"""Historical income growth from SEC disclosed annual periods only.
TTM growth requires comparable disclosure windows at both dates.
No EPS inference from current shares or net income.
"""
from datetime import date
from sec_ttm import observations

def annual_growth(facts,tags,asof):
    obs=observations(facts,tags,asof)
    annual=[r for r in obs if 330<=r["duration"]<=380]
    if not annual:return None
    last=max(annual,key=lambda r:r["end"])
    comparable=[r for r in annual if 350<=(last["start"]-r["start"]).days<=380 and
                350<=(last["end"]-r["end"]).days<=380]
    if not comparable:return None
    prior=max(comparable,key=lambda r:r["end"])
    if prior["value"]==0:return None
    return {"growth":(last["value"]-prior["value"])/abs(prior["value"]),
            "recent_period_end":last["end"].isoformat(),
            "previous_period_end":prior["end"].isoformat(),
            "available_by":max(last["filed"],prior["filed"]).isoformat(),
            "metric":"annual_reported_growth_not_ttm"}
