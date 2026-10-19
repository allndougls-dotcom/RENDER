"""Research-only SEC XBRL corroboration of Yahoo split events.

SEC CompanyFacts may expose StockholdersEquityNoteStockSplitConversionRatio1
with period ends that are NOT necessarily ex-dates. A ratio match is only
corroboration; it must never mark the events fully verified automatically.
"""
import argparse,csv,json,os,time,urllib.request
from pathlib import Path

TAG="StockholdersEquityNoteStockSplitConversionRatio1"

def sec_candidates(companyfacts,asof):
    result=[]
    facts=companyfacts.get("facts",{}).get("us-gaap",{}).get(TAG,{}).get("units",{})
    for unit,observations in facts.items():
        for x in observations:
            if x.get("filed","9999")>asof or x.get("end","9999")>asof:continue
            try:ratio=float(x["val"])
            except (ValueError,TypeError,KeyError):continue
            if ratio<=0 or ratio==1:continue
            result.append({"ratio":ratio,"period_end":x.get("end"),
                           "filed":x["filed"],"accession":x.get("accn"),"unit":unit})
    return result

def compare_candidates(yahoo_events,sec_events,asof):
    """Ratio-only comparison: SEC end dates are not ex-dates."""
    matches=[];unmatched=[]
    for event in yahoo_events:
        ratio=float(event["ratio"])
        dated=event["date"]
        candidates=[x for x in sec_events
                    if abs(float(x["ratio"])-ratio)<1e-8
                    and x.get("filed","9999")<=asof]
        row={"yahoo_ex_date":dated,"ratio":ratio,
             "sec_accessions":sorted(set(x["accession"] for x in candidates if x.get("accession")))}
        (matches if candidates else unmatched).append(row)
    return {"ratio_matches_only":matches,"unmatched_yahoo_events":unmatched,
            "matched_event_count":len(matches),
            "fully_verified":False,
            "caveat":"Ratio coincidences do not verify ex-dates, completeness, or post-asof events."}

def report(universe,price_dir,asof,agent,limit):
    findings=[];rows=list(csv.DictReader(open(universe,encoding="utf-8-sig")))
    for row in rows[:limit]:
        ticker=row["ticker"];cik=row.get("cik","");path=Path(price_dir)/(ticker+".splits.json")
        item={"ticker":ticker,"sec_candidates":[],"yahoo_events":[],"corroborated":False}
        if not path.exists():
            item["status"]="missing_yahoo_events";findings.append(item);continue
        item["yahoo_events"]=json.loads(path.read_text()).get("events",[])
        try:
            url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json"
            request=urllib.request.Request(url,headers={"User-Agent":agent,"Accept":"application/json"})
            with urllib.request.urlopen(request,timeout=35) as response:
                data=json.load(response)
            item["sec_candidates"]=sec_candidates(data,asof)
            item["comparison"]=compare_candidates(item["yahoo_events"],item["sec_candidates"],asof)
            item["status"]="sec_candidate_review_needed" if item["sec_candidates"] else "no_sec_candidate"
        except Exception as exc:
            item["status"]="sec_lookup_failed";item["error"]=str(exc)[:120]
        # SEC reporting end dates do not prove event effective dates.
        # Even a matching ratio may refer to a different corporate action.
        findings.append(item);time.sleep(.12)
    return {"asof":asof,"count":len(findings),"independently_verified":0,
            "rows":findings,"warning":"SEC XBRL candidates require filing-level ex-date confirmation; Yahoo split_history_verified remains false."}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--universe",default="results/quality_swing/sec_universe.csv")
    p.add_argument("--prices",default="results/quality_swing/valuation_prices")
    p.add_argument("--asof",required=True)
    p.add_argument("--limit",type=int,default=50)
    p.add_argument("--out",default="results/quality_swing/sec_split_candidates.json")
    a=p.parse_args()
    agent=os.environ.get("SEC_USER_AGENT","")
    if "@" not in agent:p.error("SEC_USER_AGENT required")
    result=report(a.universe,a.prices,a.asof,agent,a.limit)
    target=Path(a.out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!="rows"},indent=2))
if __name__=="__main__":main()
