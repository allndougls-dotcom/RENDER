"""Sector medians for exploratory valuations: descriptive statistics only.

Universe sectors come from CURRENT master, not historical membership/sector
assignments; sector medians are therefore NOT usable for historical SIDI scores.
"""
import argparse,csv,json,statistics
from collections import defaultdict
from pathlib import Path
from math import isfinite

FIELDS=("pe","pb","market_cap")
def summarize(valuations,universe,min_peers=3):
    sectors={}
    for row in csv.DictReader(open(universe,encoding="utf-8-sig",newline="")):
        ticker=(row.get("ticker") or "").strip().upper()
        sectors[ticker]=(row.get("sector") or "").strip()
    buckets=defaultdict(lambda:defaultdict(list))
    for row in valuations["rows"]:
        sector=sectors.get(row["ticker"])
        if not sector or not row.get("exploratory") or row.get("certified") is not False:continue
        for field in FIELDS:
            value=row.get(field)
            if value is None:continue
            try:v=float(value)
            except (TypeError,ValueError):continue
            if isfinite(v) and v>0:buckets[sector][field].append(v)
    metrics={}
    for sector,fields in sorted(buckets.items()):
        metrics[sector]={key:{"n":len(fields.get(key,[])),
                              "median":statistics.median(fields[key]) if len(fields.get(key,[]))>=min_peers else None}
                         for key in FIELDS}
    return {"asof":valuations.get("asof"),"min_peers":min_peers,
            "sector_classification_historical":False,"historical_membership":False,
            "certified":False,"may_be_used_for_sidi_score":False,
            "sector_medians":metrics,
            "warning":"CURRENT sectors/universe; unverified Yahoo splits. Diagnostic only, not point-in-time sector benchmark."}
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--valuations",default="results/quality_swing/exploratory_valuations.json")
    p.add_argument("--universe",default="results/quality_swing/sec_universe.csv")
    p.add_argument("--out",default="results/quality_swing/exploratory_sector_medians.json")
    p.add_argument("--min-peers",type=int,default=3)
    a=p.parse_args()
    result=summarize(json.loads(Path(a.valuations).read_text()),a.universe,a.min_peers)
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!="sector_medians"},indent=2))
    print("SECTOR MEDIANS",json.dumps(result["sector_medians"],indent=2))
if __name__=="__main__":main()
