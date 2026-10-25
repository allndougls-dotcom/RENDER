"""Fetch a small, reproducible SEC PIT calibration panel (9 ticker-date pairs)."""
import argparse,json,os,time
from datetime import date
from pathlib import Path
from sec_pit_fundamentals import snapshot
CASES=(("AAPL",320193),("MSFT",789019),("NVDA",1045810))
DATES=("2023-06-30","2024-06-28","2025-06-30")
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--out",default="results/sidi_growth/sec_calibration")
    a=p.parse_args()
    agent=os.environ.get("SEC_USER_AGENT","")
    if "@" not in agent or len(agent)<10:
        raise SystemExit("SEC_USER_AGENT GitHub secret required: a real contact email, no placeholder.")
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    results=[]
    for day in DATES:
        for ticker,cik in CASES:
            try:
                data=snapshot(cik,date.fromisoformat(day),agent)
                data["ticker"]=ticker
                (out/f"{ticker}_{day}.json").write_text(json.dumps(data,indent=2),encoding="utf-8")
                results.append({"ticker":ticker,"asof":day,"ok":True,"missing":len(data.get("missing",[]))})
            except Exception as exc:
                results.append({"ticker":ticker,"asof":day,"ok":False,"error":str(exc)[:250]})
            time.sleep(0.2)
    (out/"panel_status.json").write_text(json.dumps(results,indent=2))
    print(json.dumps(results,indent=2))
    if not any(x["ok"] for x in results):raise SystemExit("SEC panel failed: no observations.")
if __name__=="__main__":main()
