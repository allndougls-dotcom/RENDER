"""Read-only STOCK-RADAR score extraction from Turso HTTP pipeline API.

Exports only ticker/date/score columns. Never exports arbitrary table contents.
No INSERT/UPDATE/DELETE or schema changes. Turso credentials come from env.
"""
import argparse,csv,json,os,re,urllib.request
from pathlib import Path
from urllib.parse import urlsplit

def endpoint(url):
    if not url:raise ValueError("Missing TURSO_DATABASE_URL")
    if url.startswith("libsql://"):url="https://"+url[len("libsql://"):]
    if not url.startswith("https://"):raise ValueError("Turso URL must be libsql:// or https://")
    parsed=urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise ValueError("Invalid Turso endpoint")
    return "https://"+parsed.netloc+"/v2/pipeline"

def query(api,token,sql):
    req=urllib.request.Request(api,data=json.dumps({"requests":[{"type":"execute","stmt":{"sql":sql}},{"type":"close"}]}).encode(),
        headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=45) as reply: response=json.load(reply)
    first=response["results"][0]
    if first.get("type")!="ok":raise RuntimeError("Turso SQL query failed: "+str(first.get("error",{}).get("message","unknown"))[:180])
    result=first["response"]["result"]
    cols=[c["name"] for c in result.get("cols",[])]
    rows=[[cell.get("value") if cell.get("type")!="null" else None for cell in row] for row in result.get("rows",[])]
    return [dict(zip(cols,row)) for row in rows]

def ident(s):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",s):raise ValueError("Unsafe identifier")
    return '"'+s+'"'

def choose(cols,names):
    lookup={x.lower():x for x in cols}
    return next((lookup[n] for n in names if n in lookup),None)

def export(execute,start,end,limit=5000):
    tables=execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    result=[];coverage=[]
    for entry in tables:
        name=entry["name"]
        try:table=ident(name)
        except ValueError:continue
        fields=execute("PRAGMA table_info("+table+")")
        cols=[f["name"] for f in fields]
        score=choose(cols,("combined_score","score_total","total_score","sidi_score","combinedscore"))
        ticker=choose(cols,("ticker","symbol","stock_symbol"))
        dated=choose(cols,("asof","asof_date","snapshot_date","run_date","created_at","updated_at","date","timestamp"))
        fund=choose(cols,("fund_score","fundamental_score"))
        tech=choose(cols,("tech_score","technical_score"))
        if not all((score,ticker,dated)):continue
        selected=[ticker,dated,score]+[x for x in (fund,tech) if x]
        sql=("SELECT "+", ".join(ident(x) for x in selected)+" FROM "+table+
             " WHERE substr(CAST("+ident(dated)+" AS TEXT),1,10) BETWEEN '"+start+"' AND '"+end+"'"+
             " AND CAST("+ident(score)+" AS REAL)>8 AND CAST("+ident(score)+" AS REAL)<=10"+
             " ORDER BY "+ident(dated)+" DESC LIMIT "+str(int(limit)+1))
        rows=execute(sql)
        truncated=len(rows)>limit
        coverage.append({"table":name,"matched_exported":min(len(rows),limit),"truncated":truncated,
                         "date_column":dated,"score_column":score})
        for row in rows[:limit]:
            try:s=float(row[score])
            except (ValueError,TypeError):continue
            if not 8<s<=10:continue
            result.append({"source_table":name,"ticker":row[ticker],
                           "date":str(row[dated])[:10],"combined_score":s,
                           "fund_score":row.get(fund) if fund else None,
                           "tech_score":row.get(tech) if tech else None})
    result.sort(key=lambda x:(x["date"],x["combined_score"]),reverse=True)
    return result,coverage

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--from-date",default="2026-08-01")
    p.add_argument("--to-date",default="2026-10-19")
    p.add_argument("--out",default="results/quality_swing/turso_stock_radar_scores")
    a=p.parse_args()
    if not re.fullmatch(r"\\d{4}-\\d{2}-\\d{2}",a.from_date) or not re.fullmatch(r"\\d{4}-\\d{2}-\\d{2}",a.to_date):
        p.error("Invalid date format")
    token=os.environ.get("TURSO_AUTH_TOKEN","")
    if not token:raise SystemExit("TURSO_AUTH_TOKEN missing")
    api=endpoint(os.environ.get("TURSO_DATABASE_URL",""))
    rows,coverage=export(lambda sql:query(api,token,sql),a.from_date,a.to_date)
    dest=Path(a.out);dest.mkdir(parents=True,exist_ok=True)
    fields=("source_table","ticker","date","combined_score","fund_score","tech_score")
    with (dest/"scores_above_8.csv").open("w",newline="",encoding="utf-8") as fh:
        writer=csv.DictWriter(fh,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    report={"range":[a.from_date,a.to_date],"records_above_8":len(rows),
            "records_above_8_5":sum(r["combined_score"]>8.5 for r in rows),
            "tables_checked_for_scores":coverage,
            "note":"Read-only extraction, not a historical backtest. Tables without recognized score/date/ticker fields are skipped."}
    (dest/"summary.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))
if __name__=="__main__":main()
