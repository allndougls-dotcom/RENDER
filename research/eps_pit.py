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
    """Compare diluted annual EPS from the SAME filed 10-K accession.

    This avoids mixing pre-/post-split EPS bases reported in different filings.
    If comparable prior-year diluted EPS is not included in the latest available
    annual filing, return missing instead of combining incompatible numbers.
    """
    by_filing={}
    for item in facts.get("us-gaap",{}).get("EarningsPerShareDiluted",{}).get("units",{}).get("USD/shares",[]):
        try:
            start=date.fromisoformat(item["start"])
            end=date.fromisoformat(item["end"])
            filed=date.fromisoformat(item["filed"])
            value=float(item["val"])
            accession=item["accn"]
        except (ValueError,TypeError,KeyError):continue
        if not isfinite(value) or not 330<=(end-start).days<=380:continue
        if filed>asof or end>asof or item.get("form") not in ("10-K","10-K/A"):continue
        by_filing.setdefault(accession,[]).append((start,end,filed,value))
    if not by_filing:return None
    all_latest=[(end,filed,accn,start,val)
                for accn,observations in by_filing.items()
                for start,end,filed,val in observations]
    latest_end,latest_filed,latest_accn,latest_start,latest_value=max(
        all_latest,key=lambda x:(x[0],x[1]))
    peers=[(s,e,f,v) for s,e,f,v in by_filing[latest_accn]
           if 350<=(latest_start-s).days<=380
           and 350<=(latest_end-e).days<=380]
    if not peers:return None
    old_start,old_end,old_filed,old_value=max(peers,key=lambda x:x[1])
    if old_value==0:return None
    return {"growth":(latest_value-old_value)/abs(old_value),
            "new_eps":latest_value,"old_eps":old_value,
            "new_period_end":str(latest_end),"old_period_end":str(old_end),
            "available_on":str(max(latest_filed,old_filed)),
            "tag":"EarningsPerShareDiluted","accession":latest_accn,
            "comparability":"same_filing_diluted_eps"}
