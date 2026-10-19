"""Create an actionable document-review queue, never attest automatically."""
import argparse,csv,json
from pathlib import Path

def queue(universe,prices,candidates,asof):
    lookup={r["ticker"]:r for r in csv.DictReader(open(universe,encoding="utf-8-sig"))}
    source=json.loads(Path(candidates).read_text(encoding="utf-8"))
    rows=[]
    for entry in source.get("rows",[]):
        ticker=entry["ticker"]; cik=lookup.get(ticker,{}).get("cik","")
        event_list=entry.get("yahoo_events") or []
        candidate_list=entry.get("sec_candidates") or []
        for event in event_list:
            matches=[c for c in candidate_list if abs(float(c["ratio"])-float(event["ratio"]))<1e-8]
            urls=[]
            for c in matches:
                accn=c.get("accession")
                if cik and accn:
                    try:
                        path=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-','')}/"
                        if path not in urls:urls.append(path)
                    except (ValueError,AttributeError):pass
            rows.append({"ticker":ticker,"yahoo_effective_date":event["date"],
                "split_ratio":event["ratio"],"relative_to_asof":"future" if event["date"]>asof else "past",
                "sec_candidate_count":len(matches),"sec_filing_directories":urls,
                "review_status":"unverified","review_task":"Confirm effective date and ratio in original filing or issuer IR; verify no omitted events and full history"})
        if not event_list:
            rows.append({"ticker":ticker,"yahoo_effective_date":None,"split_ratio":None,
                "relative_to_asof":None,"sec_candidate_count":0,"sec_filing_directories":[],
                "review_status":"unverified",
                "review_task":"Independently establish complete absence of splits over required span"})
    return {"asof":asof,"rows":rows,"events_to_review":sum(r["split_ratio"] is not None for r in rows),
        "verified":0,"note":"Links are leads, not evidence verified; no auto-attestations generated."}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--universe",default="results/quality_swing/sec_universe.csv")
    p.add_argument("--prices",default="results/quality_swing/valuation_prices")
    p.add_argument("--candidates",default="results/quality_swing/sec_split_candidates.json")
    p.add_argument("--asof",required=True)
    p.add_argument("--out",default="results/quality_swing/split_review_queue.json")
    a=p.parse_args()
    report=queue(a.universe,a.prices,a.candidates,a.asof)
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="rows"},indent=2))
if __name__=="__main__":main()
