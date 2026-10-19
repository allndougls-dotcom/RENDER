"""Multi-date exploratory cohort comparison from INDEPENDENT dated rankings.

No replay of today's or one-day fundamentals onto other historical dates.
The tool accepts one complete ranking snapshot per asof and compares bands;
same-day target/stop ambiguity uses the existing stop-first outcome engine.
"""
import argparse,json
from collections import defaultdict
from pathlib import Path
from exploratory_forward_outcomes import run

COHORTS=("GT_8_5","6_5_TO_8_5")

def classify(score):
    if score is None:return None
    x=float(score)
    if x>8.5:return "GT_8_5"
    if 6.5<=x<=8.5:return "6_5_TO_8_5"
    return None

def compare(rankings,prices_dir,stop=0.10,horizons=(20,40,60,90),targets=(0.10,0.15)):
    dates=set();evaluated=[];seen=set();observations=defaultdict(list)
    for ranking in rankings:
        asof=ranking.get("asof")
        if not asof or asof in dates:raise ValueError("Missing or duplicate asof")
        dates.add(asof)
        if ranking.get("validated_combined_scores",0)!=0:
            raise ValueError("Only isolated exploratory rankings accepted")
        buckets={c:[] for c in COHORTS}
        for row in ranking.get("rows",[]):
            ticker=row.get("ticker")
            if not ticker or (asof,ticker) in seen:raise ValueError("Duplicate or missing ticker")
            seen.add((asof,ticker))
            if row.get("certified") is not False or row.get("eligible_historical_signal") is not False:
                raise ValueError("Unexpected validated signal in exploratory input")
            cohort=classify(row.get("combined_exploratory"))
            if cohort:buckets[cohort].append(row)
        for cohort,rows in buckets.items():
            # Existing forward engine requires strictly > threshold; 6.5 inclusive
            # is handled by setting threshold just below 6.5 after explicit filtering.
            filtered={"asof":asof,"rows":rows}
            study=run(filtered,prices_dir,threshold=6.499999,
                      horizons=horizons,targets=targets,stop=stop)
            evaluated.append({"asof":asof,"cohort":cohort,"companies":len(rows),
                              "study_rows":len(study["results"])})
            for obs in study["results"]:
                if "tp_pct" in obs:
                    key=(cohort,obs["tp_pct"],obs["horizon_sessions"])
                    observations[key].append({**obs,"asof":asof})
    summaries=[]
    for cohort in COHORTS:
        for target in targets:
            for horizon in horizons:
                key=(cohort,int(target*100),horizon)
                group=observations[key]
                resolved=[x for x in group if x["status"] in ("target","stop_first","not_hit")]
                wins=sum(x["status"]=="target" for x in resolved)
                summaries.append({"cohort":cohort,"tp_pct":key[1],"horizon_sessions":horizon,
                                  "observations":len(group),"resolved":len(resolved),
                                  "target_hits":wins,
                                  "observed_target_hit_rate":wins/len(resolved) if resolved else None,
                                  "unresolved":len(group)-len(resolved)})
    return {"dates_count":len(dates),"asof_dates":sorted(dates),
            "stop_pct":stop,"comparison":summaries,"by_date":evaluated,
            "validated_backtest":False,"verified_win_rate":None,
            "warnings":["Only independently built dated rankings are compared",
                        "Survivorship, sector, valuation, fees and sample selection biases remain",
                        "A single date cannot establish an out-of-sample effect"]}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--ranking-dir",default="results/quality_swing/dated_rankings")
    p.add_argument("--prices",default="data/backtest_prices")
    p.add_argument("--stop",type=float,default=0.10)
    p.add_argument("--out",default="results/quality_swing/cohort_comparison.json")
    args=p.parse_args()
    if not 0<args.stop<1:p.error("Invalid stop")
    files=sorted(Path(args.ranking_dir).glob("*.json"))
    if not files:
        raise SystemExit("No dated rankings; refusing to manufacture historical snapshots")
    rankings=[json.loads(f.read_text(encoding="utf-8")) for f in files]
    result=compare(rankings,args.prices,stop=args.stop)
    out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result,indent=2))
if __name__=="__main__":main()
