"""Build dated, auditable SEC CompanyFacts observations (research only).

Inputs:
  --universe-csv: ticker,cik,sector (historical membership must be separately verified)
  --asof: YYYY-MM-DD
  --out: directory
Set SEC_USER_AGENT to a real contact (organization email).
Never labels observations as a SIDI score: all earnings and valuation components
must be verified prior to scoring.
"""
import argparse,csv,json,os,time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from sec_pit_fundamentals import snapshot

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--universe-csv",required=True)
    p.add_argument("--asof",type=date.fromisoformat,required=True)
    p.add_argument("--out",default="results/quality_swing/sec_pit")
    p.add_argument("--limit",type=int,default=20)
    p.add_argument("--pause",type=float,default=0.2)
    args=p.parse_args()
    ua=os.environ.get("SEC_USER_AGENT","")
    if "@" not in ua or len(ua)<10:p.error("SEC_USER_AGENT must contain real contact email; configure GitHub Actions secret.")
    if args.pause<0.11:p.error("--pause must be at least 0.11 seconds")
    if args.limit<1:p.error("--limit must be positive")
    target=Path(args.out);target.mkdir(parents=True,exist_ok=True)
    rows=list(csv.DictReader(Path(args.universe_csv).open(encoding="utf-8-sig",newline="")))
    report={"asof":str(args.asof),"source":"SEC companyfacts","attempted":0,"success":0,
            "failed":[],"missing_by_metric":{},"score_reconstructed":False,
            "bias_notice":"Universe membership and sector peers must be independently point-in-time verified."}
    for row in rows[:args.limit]:
        ticker=(row.get("ticker") or "").upper().strip()
        cik=(row.get("cik") or "").strip()
        if not ticker or not cik or not cik.isdigit():
            report["failed"].append({"ticker":ticker,"reason":"ticker/cik missing"});continue
        report["attempted"]+=1
        try:
            data=snapshot(int(cik),args.asof,ua)
            data["ticker"]=ticker
            data["sector_asof_unverified"]=row.get("sector","")
            # The returned facts contain filing timestamps, not current scores.
            (target/f"{ticker}.json").write_text(json.dumps(data,indent=2),encoding="utf-8")
            report["success"]+=1
            for metric in data["missing"]:
                report["missing_by_metric"][metric]=report["missing_by_metric"].get(metric,0)+1
        except (HTTPError,URLError,ValueError,TimeoutError,OSError) as exc:
            report["failed"].append({"ticker":ticker,"reason":str(exc)[:180]})
        time.sleep(args.pause)
    (target/"coverage.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))
    if report["success"]==0:raise SystemExit("No SEC observations obtained — do not calculate WR.")

if __name__=="__main__":main()
