"""Read-only audit of SIDI's actual saved master snapshots (no score recomputation)."""
import argparse,csv,json,re
from datetime import date
from pathlib import Path

def audit(root,start="2026-08-01"):
    hits=[];scanned=[];skipped=[]
    for f in sorted(Path(root).glob("sp500_full_export_*.csv")):
        suffix=f.stem.removeprefix("sp500_full_export_")
        match=re.search(r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)",suffix)
        filename_day=None
        if match:
            try:filename_day=date(*map(int,match.groups())).isoformat()
            except ValueError:pass
        with f.open(encoding="utf-8-sig",newline="") as handle:
            reader=csv.DictReader(handle)
            cols=reader.fieldnames or []
            score_col=next((c for c in cols if c.strip().lower()=="combined_score"),None)
            ticker_col=next((c for c in cols if c.strip().lower() in ("ticker","symbol")),None)
            dt_col=next((c for c in cols if c.strip().lower() in ("asof","date","snapshot_date","scan_date")),None)
            if not score_col or not ticker_col:
                skipped.append({"file":f.name,"reason":"ticker_or_combined_score_missing"});continue
            total=0;eligible=0;best=None;file_dates=set()
            for row in reader:
                total+=1
                raw=(row.get(dt_col) or "")[:10] if dt_col else ""
                day=raw if re.fullmatch(r"20\d{2}-\d{2}-\d{2}",raw) else filename_day
                if not day:continue
                file_dates.add(day)
                if day<start:continue
                try:score=float((row.get(score_col) or "").replace(",","."))
                except (TypeError,ValueError):continue
                if not (0<=score<=10):continue
                eligible+=1;best=score if best is None else max(best,score)
                if score>8:
                    def numeric(name):
                        try:return float((row.get(name) or "").replace(",","."))
                        except (ValueError,TypeError):return None
                    hits.append({"date":day,"ticker":(row.get(ticker_col) or "").strip().upper(),
                                 "combined_score":score,"fund_score":numeric("fund_score"),
                                 "tech_score":numeric("tech_score"),"source_file":f.name,
                                 "above_8_5":score>8.5})
            scanned.append({"file":f.name,"rows":total,"eligible_from_start":eligible,
                            "max_from_start":best,"dates":sorted(file_dates)})
    hits.sort(key=lambda r:(r["date"],-r["combined_score"],r["ticker"]))
    return {"start":start,"snapshots_scanned":len(scanned),"snapshots_skipped":skipped,
            "files":scanned,"over_8_rows":len(hits),
            "over_8_5_rows":sum(r["above_8_5"] for r in hits),
            "unique_tickers_over_8":len({r["ticker"] for r in hits}),
            "max_observed":max((r["combined_score"] for r in hits),default=None),
            "hits":hits,
            "caution":"Counts are file-observations (may include repeated snapshots). Only file or explicit row dates accepted."}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--root",default="data/master")
    p.add_argument("--start",default="2026-08-01")
    p.add_argument("--out",default="results/quality_swing/original_score_audit")
    a=p.parse_args()
    date.fromisoformat(a.start)
    result=audit(a.root,a.start)
    folder=Path(a.out);folder.mkdir(parents=True,exist_ok=True)
    (folder/"summary.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    with (folder/"scores_over_8.csv").open("w",newline="",encoding="utf-8") as f:
        names=["date","ticker","combined_score","fund_score","tech_score","source_file","above_8_5"]
        writer=csv.DictWriter(f,fieldnames=names);writer.writeheader();writer.writerows(result["hits"])
    print(json.dumps({k:v for k,v in result.items() if k not in ("hits","files")},indent=2))
    print("TOP 20",json.dumps(sorted(result["hits"],key=lambda x:x["combined_score"],reverse=True)[:20],indent=2))
if __name__=="__main__":main()
