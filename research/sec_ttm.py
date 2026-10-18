"""Point-in-time trailing twelve-month SEC flow series, not current-data proxies.
Use annual duration 330-380 days, or four *standalone* quarters 70-110 days.
YTD cumulative SEC facts are NOT summed as quarters.
"""
from datetime import date, timedelta
from math import isfinite

def observations(facts,tags,asof):
    records=[]
    for priority,tag in enumerate(tags):
        for fact in facts.get("us-gaap",{}).get(tag,{}).get("units",{}).get("USD",[]):
            try:
                filed=date.fromisoformat(fact["filed"])
                start=date.fromisoformat(fact["start"])
                end=date.fromisoformat(fact["end"])
                val=float(fact["val"])
            except (KeyError,ValueError,TypeError):continue
            if not isfinite(val) or filed>asof or end>asof:continue
            if fact.get("form") not in ("10-Q","10-K","10-Q/A","10-K/A"):continue
            days=(end-start).days
            if not 70<=days<=380:continue
            records.append({"start":start,"end":end,"filed":filed,"value":val,
                           "duration":days,"tag":tag,"priority":priority,"accession":fact.get("accn")})
    # latest filing no later than asof for each economic period, prefer defined tag order.
    unique={}
    for r in records:
        k=(r["start"],r["end"])
        old=unique.get(k)
        if old is None or (r["filed"],-r["priority"])>(old["filed"],-old["priority"]):unique[k]=r
    return sorted(unique.values(),key=lambda r:r["end"])

def latest_ttm(records):
    annual=[r for r in records if 330<=r["duration"]<=380]
    quarters=[r for r in records if 70<=r["duration"]<=110]
    options=[]
    for r in annual: options.append((r["end"],r["value"],[r],"ANNUAL"))
    for last in quarters:
        chosen=[last]
        for _ in range(3):
            # Fiscal quarters must be contiguous; never invent a missing quarter.
            earlier=[x for x in quarters if x["end"]+timedelta(days=1)==chosen[-1]["start"]]
            if not earlier:break
            chosen.append(max(earlier,key=lambda x:x["end"]))
        if len(chosen)==4 and 330<=(last["end"]-chosen[-1]["start"]).days<=380:
            options.append((last["end"],sum(x["value"] for x in chosen),chosen,"FOUR_QUARTERS"))
    if not options:return None
    end,value,source,method=max(options,key=lambda x:(x[0],x[3]=="FOUR_QUARTERS"))
    return {"value":value,"period_end":end.isoformat(),"method":method,
            "sources":[{"tag":r["tag"],"filed":r["filed"].isoformat(),"accession":r["accession"],
                        "start":r["start"].isoformat(),"end":r["end"].isoformat()} for r in source]}
