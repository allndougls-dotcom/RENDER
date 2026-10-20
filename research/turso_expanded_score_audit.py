"""Read-only Turso score discovery across recognized scalar and JSON payload columns.

Never exports arbitrary JSON blobs or non-score fields. Table and column names are
reported for diagnostics; candidate JSON paths are explicitly allowlisted.
"""
import argparse,csv,json,math,re
from pathlib import Path
from turso_stock_radar_scores import endpoint,query,ident,choose

SCORE=("combined_score","score_total","total_score","sidi_score","combinedscore")
TICKER=("ticker","symbol","stock_symbol")
DATE=("asof","asof_date","snapshot_date","signal_date","run_date","created_at","updated_at","date","timestamp")
FUND=("fund_score","fundamental_score")
TECH=("tech_score","technical_score")
JSON_COLUMNS=("payload","data","snapshot","signal_payload","analysis_json","details_json","raw_payload")
JSON_PATHS=("$.combined_score","$.scores.combined_score","$.score.combined_score",
            "$.analysis.combined_score","$.sidi.combined_score",
            "$.fund_score","$.tech_score","$.ticker","$.symbol",
            "$.asof","$.date","$.signal_date")
def score_value(value):
    try:
        n=float(value)
        return n if math.isfinite(n) and 8<n<=10 else None
    except (TypeError,ValueError):return None
def extract(execute,start,end,limit=5000):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",start+"" ) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",end+""):raise ValueError("date")
    tables=execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    results=[];audit=[]
    for entry in tables:
        name=entry.get("name","")
        if not isinstance(name,str):continue
        try:table=ident(name)
        except ValueError:continue
        columns=[x["name"] for x in execute("PRAGMA table_info("+table+")")]
        score=choose(columns,SCORE);ticker=choose(columns,TICKER);dated=choose(columns,DATE)
        fund=choose(columns,FUND);tech=choose(columns,TECH)
        payload=choose(columns,JSON_COLUMNS)
        # No broad selects: only score-bearing tables or known JSON payload columns,
        # and table must provide a date/ticker or discover them in allowlisted JSON paths.
        if not score and not payload:continue
        j=lambda path: "(CASE WHEN json_valid("+ident(payload)+") THEN json_extract("+ident(payload)+",'"+path+"') END)" if payload else "NULL"
        json_score="COALESCE("+",".join(j(p) for p in JSON_PATHS if p.endswith("combined_score"))+")" if payload else "NULL"
        expr_score=ident(score) if score else json_score
        expr_ticker=ident(ticker) if ticker else ("COALESCE("+j("$.ticker")+","+j("$.symbol")+")" if payload else "NULL")
        expr_date=ident(dated) if dated else ("COALESCE("+j("$.asof")+","+j("$.signal_date")+","+j("$.date")+")" if payload else "NULL")
        if expr_ticker=="NULL" or expr_date=="NULL":continue
        # Per-table try/catch: malformed JSON must not prevent another valid table.
        query_sql=("SELECT CAST("+expr_ticker+" AS TEXT) AS ticker, CAST("+expr_date+" AS TEXT) AS dt, "+
                   "CAST("+expr_score+" AS REAL) AS score, "+
                   (("CAST("+ident(fund)+" AS REAL)") if fund else (j("$.fund_score") if payload else "NULL"))+" AS fund, "+
                   (("CAST("+ident(tech)+" AS REAL)") if tech else (j("$.tech_score") if payload else "NULL"))+" AS tech "+
                   "FROM "+table+" WHERE substr(CAST("+expr_date+" AS TEXT),1,10) BETWEEN '"+start+"' AND '"+end+"'"+
                   " AND CAST("+expr_score+" AS REAL)>8 AND CAST("+expr_score+" AS REAL)<=10 "+
                   " LIMIT "+str(int(limit)+1))
        try:
            rows=execute(query_sql)
        except Exception as exc:
            audit.append({"table":name,"status":"query_failed","reason":str(exc)[:90]})
            continue
        accepted=0
        for row in rows[:limit]:
            score_n=score_value(row.get("score"))
            tick=row.get("ticker")
            dt=str(row.get("dt") or "")[:10]
            if score_n is None or not tick or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",dt):continue
            results.append({"table":name,"ticker":str(tick)[:30],"date":dt,
                            "combined_score":score_n,"fund_score":row.get("fund"),
                            "tech_score":row.get("tech"),"source":("scalar" if score else "json")})
            accepted+=1
        audit.append({"table":name,"status":"ok","score_source":"scalar" if score else "json",
                      "date_source":dated or "json","matched":accepted,"truncated":len(rows)>limit})
    results.sort(key=lambda r:(r["date"],r["combined_score"]),reverse=True)
    return results,audit

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--from-date",default="2026-08-01")
    p.add_argument("--to-date",default="2026-10-20")
    p.add_argument("--out",default="results/quality_swing/turso_expanded_audit")
    a=p.parse_args()
    from datetime import date
    date.fromisoformat(a.from_date);date.fromisoformat(a.to_date)
    import os
    token=os.environ.get("TURSO_AUTH_TOKEN")
    if not token:raise SystemExit("Missing TURSO_AUTH_TOKEN")
    api=endpoint(os.environ.get("TURSO_DATABASE_URL",""))
    rows,audit=extract(lambda sql:query(api,token,sql),a.from_date,a.to_date)
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    names=("table","ticker","date","combined_score","fund_score","tech_score","source")
    with (out/"scores_above_8_expanded.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=names);w.writeheader();w.writerows(rows)
    summary={"period":[a.from_date,a.to_date],"records_above_8":len(rows),
        "records_above_8_5":sum(r["combined_score"]>8.5 for r in rows),
        "scanned_tables":audit,"scope":"read_only","note":"Multiple tables may contain duplicate records; not deduplicated or certified."}
    (out/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps(summary,indent=2))
if __name__=="__main__":main()
