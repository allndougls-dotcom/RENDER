"""Recover original STOCK-RADAR scores from historical Git CSV blobs.

Read-only: git log/ls-tree/show. No reconstruction of historical scores.
"""
import csv,io,json,re,subprocess
from collections import defaultdict
from pathlib import Path

def git(*args):
    return subprocess.check_output(["git",*args],stderr=subprocess.DEVNULL)
def col(fields,candidates):
    lookup={x.strip().lower():x for x in fields if x}
    return next((lookup[x] for x in candidates if x in lookup),None)
def main():
    out=Path("results/quality_swing/original_ingesta_history")
    out.mkdir(parents=True,exist_ok=True)
    commits=git("log","--all","--format=%H","--","data/master").decode().splitlines()
    seen=set();scores=[];inventory=[]
    for sha in commits:
        try:
            paths=git("ls-tree","-r","--name-only",sha,"--","data/master").decode().splitlines()
        except subprocess.CalledProcessError:continue
        for path in paths:
            if not re.fullmatch(r"data/master/sp500_full_export_[0-9]{8}[.]csv",path):
                continue
            blob=git("rev-parse",sha+":"+path).decode().strip()
            if blob in seen:continue
            seen.add(blob)
            dt=re.search(r"([0-9]{8})[.]csv$",path).group(1)
            date=dt[:4]+"-"+dt[4:6]+"-"+dt[6:8]
            if date<"2026-08-01":continue
            try:raw=git("show",sha+":"+path).decode("utf-8-sig")
            except (subprocess.CalledProcessError,UnicodeDecodeError):
                inventory.append({"date":date,"path":path,"status":"unreadable","commit":sha[:12]});continue
            reader=csv.DictReader(io.StringIO(raw))
            headers=reader.fieldnames or []
            ticker=col(headers,("ticker","symbol","stock_symbol"))
            combined=col(headers,("combined_score","score_total","total_score","sidi_score","score_final","final_score","score"))
            fund=col(headers,("fund_score","fundamental_score","fundamentals_score"))
            tech=col(headers,("tech_score","technical_score","technicals_score"))
            n=0;above=0
            for row in reader:
                n+=1
                if not ticker or not combined:continue
                try:score=float(row[combined])
                except (TypeError,ValueError):continue
                if score<=8 or score>10:continue
                scores.append({"date":date,"ticker":row.get(ticker),"combined_score":score,
                    "fund_score":row.get(fund) if fund else "","tech_score":row.get(tech) if tech else "",
                    "source_csv":path,"commit":sha[:12]})
                above+=1
            inventory.append({"date":date,"path":path,"commit":sha[:12],"status":"parsed" if ticker and combined else "score_column_not_found",
                "rows":n,"above_8":above,"columns_detected":headers if not combined else []})
    scores.sort(key=lambda r:(r["date"],r["combined_score"]),reverse=True)
    with (out/"historical_scores_above_8.csv").open("w",newline="",encoding="utf-8") as fh:
        cols=("date","ticker","combined_score","fund_score","tech_score","source_csv","commit")
        writer=csv.DictWriter(fh,fieldnames=cols);writer.writeheader();writer.writerows(scores)
    summary={"unique_csv_blobs":len(inventory),"rows_over_8":len(scores),
        "rows_over_8_5":sum(x["combined_score"]>8.5 for x in scores),
        "unique_tickers_over_8":len({x["ticker"] for x in scores}),
        "snapshots":inventory,"note":"Original Git blobs. Duplicate dates/commits can occur. Missing snapshots are not evidence of no signals."}
    (out/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print("Historical snapshots",len(inventory),"scores >8",len(scores),"scores >8.5",summary["rows_over_8_5"])
if __name__=="__main__":main()
