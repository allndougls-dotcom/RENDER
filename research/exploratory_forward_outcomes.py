"""Exploratory forward outcome study, NOT validated backtest or trading signals.

Entry at NEXT session adjusted Open (after the scoring close). Evaluate adjusted
daily High for target touches and adjusted Low for exploratory stop. Same-day
stop+target is ambiguous: conservatively count stop first.
No stop configured => censored unless TP touched by horizon.
"""
import argparse,csv,json
from pathlib import Path

def run(ranking,prices_dir,threshold=8.5,horizons=(20,40,60,90),targets=(0.10,0.15),stop=None):
    asof=ranking["asof"];results=[]
    for signal in ranking.get("rows",[]):
        if signal.get("combined_exploratory") is None or signal["combined_exploratory"]<=threshold:continue
        ticker=signal["ticker"];p=Path(prices_dir)/(ticker+".csv")
        if not p.exists():
            results.append({"ticker":ticker,"status":"missing_prices"});continue
        with p.open(newline="") as fh:
            bars=[r for r in csv.DictReader(fh) if r.get("Date")]
        future=[b for b in bars if b["Date"][:10]>asof]
        if not future:
            results.append({"ticker":ticker,"status":"no_future_sessions"});continue
        try:entry=float(future[0]["Open"])
        except (ValueError,KeyError,TypeError):
            results.append({"ticker":ticker,"status":"invalid_entry"});continue
        if entry<=0:
            results.append({"ticker":ticker,"status":"invalid_entry"});continue
        for target in targets:
            for horizon in horizons:
                segment=future[:horizon]
                hit=None;exit_day=None
                for i,bar in enumerate(segment,1):
                    try:high=float(bar["High"]);low=float(bar["Low"])
                    except (ValueError,KeyError,TypeError):
                        hit="invalid_bars";break
                    if stop is not None and low<=entry*(1-stop):
                        hit="stop_first";exit_day=i;break
                    if high>=entry*(1+target):
                        hit="target";exit_day=i;break
                if hit is None:hit="not_hit" if len(segment)>=horizon else "censored"
                results.append({"ticker":ticker,"status":hit,"tp_pct":int(target*100),
                    "horizon_sessions":horizon,"entry_date":future[0]["Date"][:10],
                    "entry_adjusted_open":entry,"exit_session":exit_day,
                    "fully_observed":len(segment)>=horizon or hit in ("target","stop_first")})
    grouped=[]
    for tp in targets:
        for horizon in horizons:
            group=[x for x in results if x.get("tp_pct")==int(tp*100) and x.get("horizon_sessions")==horizon]
            complete=[x for x in group if x["status"] in ("target","not_hit","stop_first")]
            wins=sum(x["status"]=="target" for x in complete)
            grouped.append({"tp_pct":int(tp*100),"horizon_sessions":horizon,
                "eligible":len(group),"resolved":len(complete),"targets_hit":wins,
                "observed_hit_rate":wins/len(complete) if complete else None,
                "censored":sum(x["status"]=="censored" for x in group)})
    return {"asof":asof,"threshold":threshold,"study_type":"EXPLORATORY_ONLY",
        "entry":"next_session_adjusted_open","same_day_priority":"stop_before_target",
        "stop_pct":stop,"results":results,"groups":grouped,
        "validated_backtest":False,"verified_win_rate":None,
        "warning":"Single date; current survivor universe; unverified historical valuation; no fees/slippage. Observed target hits are not a validated WR."}

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--ranking",default="results/quality_swing/exploratory_ranking.json")
    p.add_argument("--prices",default="data/backtest_prices")
    p.add_argument("--out",default="results/quality_swing/exploratory_forward_outcomes.json")
    a=p.parse_args()
    data=run(json.loads(Path(a.ranking).read_text()),a.prices)
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(data,indent=2))
    print(json.dumps({k:v for k,v in data.items() if k!="results"},indent=2))
