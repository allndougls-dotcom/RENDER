"""Historical SEC diluted EPS and per-share book value, strictly point-in-time.

EPS uses SEC USD/shares (not raw net income / today's shares).
A price-derived PE/PB is deliberately unavailable until raw unadjusted historical
price and matching split basis are validated. Adjusted Yahoo closes are unsafe
for direct historical PE calculation.
"""
from datetime import date
from math import isfinite

EPS_TAGS=("EarningsPerShareDiluted","EarningsPerShareBasic")
def eps_annual(facts,asof):
    candidates=[]
    for priority,tag in enumerate(EPS_TAGS):
        for unit in facts.get("us-gaap",{}).get(tag,{}).get("units",{}).get("USD/shares",[]):
            try:
                start=date.fromisoformat(unit["start"]);end=date.fromisoformat(unit["end"])
                filed=date.fromisoformat(unit["filed"]); val=float(unit["val"])
            except (KeyError,ValueError,TypeError):continue
            if not isfinite(val) or end>asof or filed>asof:continue
            if unit.get("form") not in ("10-K","10-K/A"):continue
            if not 330<=(end-start).days<=380:continue
            candidates.append((end,filed,-priority,tag,val,start,unit.get("accn","")))
    if not candidates:return None
    end,filed,_,tag,val,start,accn=max(candidates,key=lambda r:(r[0],r[1],r[2]))
    return {"value":val,"tag":tag,"period_start":str(start),"period_end":str(end),
            "available_on":str(filed),"accession":accn,
            "basis":"USD/shares reported; no split adjustment inferred"}

def eps_annual_growth(facts,asof):
    periods={}
    for tag in EPS_TAGS:
        for x in facts.get("us-gaap",{}).get(tag,{}).get("units",{}).get("USD/shares",[]):
            try:
                s=date.fromisoformat(x["start"]);e=date.fromisoformat(x["end"])
                f=date.fromisoformat(x["filed"]);v=float(x["val"])
            except (ValueError,KeyError,TypeError):continue
            if f>asof or e>asof or x.get("form") not in ("10-K","10-K/A"):continue
            if not 330<=(e-s).days<=380 or not isfinite(v):continue
            key=(s,e)
            old=periods.get(key)
            candidate=(f,1 if tag==EPS_TAGS[0] else 0,v,tag)
            if old is None or candidate[:2]>old[:2]:periods[key]=candidate
    ordered=sorted(periods.items(),key=lambda r:r[0][1],reverse=True)
    if len(ordered)<2:return None
    (s1,e1),(f1,_,v1,t1)=ordered[0]
    preceding=[((s,e),info) for (s,e),info in ordered[1:]
               if 350<=(s1-s).days<=380 and 350<=(e1-e).days<=380]
    if not preceding:return None
    (s0,e0),(f0,_,v0,t0)=preceding[0]
    if v0==0 or t0!=t1:return None
    return {"growth":(v1-v0)/abs(v0),"new_eps":v1,"old_eps":v0,
            "new_period_end":str(e1),"old_period_end":str(e0),
            "available_on":str(max(f1,f0)),"tag":t1}
