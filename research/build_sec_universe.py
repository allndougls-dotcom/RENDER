"""Build SEC CIK coverage universe from SIDI master + SEC company_tickers.json.
CURRENT universe ONLY: membership at historical asof is NOT established.
"""
import argparse,csv,json,urllib.request
from pathlib import Path

def main():
 p=argparse.ArgumentParser()
 p.add_argument("--master",default="data/master")
 p.add_argument("--out",default="results/quality_swing/sec_universe.csv")
 p.add_argument("--user-agent",required=True)
 a=p.parse_args()
 if "@" not in a.user_agent: p.error("Use real SEC contact email.")
 latest=sorted(Path(a.master).glob("sp500_full_export_*.csv"))
 if not latest: raise SystemExit("No master CSV available")
 request=urllib.request.Request("https://www.sec.gov/files/company_tickers.json",headers={"User-Agent":a.user_agent,"Accept":"application/json"})
 with urllib.request.urlopen(request,timeout=40) as f: listing=json.load(f)
 lookup={str(item["ticker"]).upper():int(item["cik_str"]) for item in listing.values()}
 rows=[];missing=[]
 with latest[-1].open(encoding="utf-8-sig",newline="") as f:
  for row in csv.DictReader(f):
   ticker=(row.get("ticker") or "").upper().strip()
   cik=lookup.get(ticker)
   if cik is None:
    missing.append(ticker)
    continue
   rows.append({"ticker":ticker,"cik":cik,"sector":row.get("sector","")})
 target=Path(a.out);target.parent.mkdir(parents=True,exist_ok=True)
 with target.open("w",encoding="utf-8",newline="") as f:
  writer=csv.DictWriter(f,fieldnames=["ticker","cik","sector"]);writer.writeheader();writer.writerows(rows)
 report={"source_master":latest[-1].name,"universe_point_in_time":False,"resolved":len(rows),"unresolved":missing,
         "warning":"Current constituents. Not historical S&P500 membership."}
 (target.parent/"sec_universe_coverage.json").write_text(json.dumps(report,indent=2))
 print(json.dumps(report,indent=2))
if __name__=="__main__":main()
