"""Conservative valuation arithmetic for historical QUALITY SWING.
All inputs need explicit as-of/share bases. Unknown split history => no valuation.
No live SIDI scores are produced here.
"""
from datetime import date
from math import isfinite

def historical_valuation(*, asof, price, price_basis, price_date, eps=None,
                         eps_period_end=None, shares=None, shares_period_end=None,
                         equity=None, equity_period_end=None, split_events=None,
                         split_history_verified=False):
    """Price must be Yahoo split-adjusted close, NOT dividend adjusted.
    Yahoo retrospectively adjusts closes for splits *after* asof. To recover
    asof share basis, multiply by all post-asof split ratios. Shares are moved
    from SEC balance date to asof via intervening split events.
    """
    if isinstance(asof,str):asof=date.fromisoformat(asof)
    def dt(x):return date.fromisoformat(x) if isinstance(x,str) else x
    if price_basis!="yahoo_close_split_adjusted_no_dividends":
        return {"valid":False,"reason":"unverified_price_adjustment_basis"}
    if not split_history_verified or split_events is None:
        return {"valid":False,"reason":"split_history_not_verified"}
    if dt(price_date)!=asof:return {"valid":False,"reason":"price_date_mismatch"}
    try:
        p=float(price)
        if p<=0 or not isfinite(p):raise ValueError()
        events=sorted([(dt(x["date"]),float(x["ratio"])) for x in split_events])
        if any(r<=0 or not isfinite(r) for _,r in events):raise ValueError()
        # A future split makes the downloaded historical close smaller.
        future_factor=1.
        for d,ratio in events:
            if d>asof:future_factor*=ratio
        p_asof=p*future_factor
        answer={"valid":True,"date":str(asof),"price_asof_share_basis":p_asof,
                "basis":"split-only Yahoo Close; post-asof splits reversed",
                "pe":None,"pb":None,"market_cap":None,"warnings":[]}
        if eps is not None:
            e=float(eps)
            if dt(eps_period_end)>asof:raise ValueError()
            # SEC EPS reported for a financial year could be retrospectively
            # restated for splits after period end but before publication.
            # A split between EPS period end and asof makes PE unsafe.
            if any(dt(eps_period_end)<d<=asof for d,_ in events):
                answer["warnings"].append("eps_split_basis_requires_filing_review")
            elif e>0 and isfinite(e):
                answer["pe"]=p_asof/e
        if shares is not None:
            s=float(shares)
            if dt(shares_period_end)>asof:raise ValueError()
            if s>0 and isfinite(s):
                for d,ratio in events:
                    if dt(shares_period_end)<d<=asof:s*=ratio
                answer["market_cap"]=p_asof*s
                if equity is not None and dt(equity_period_end)<=asof:
                    eq=float(equity)
                    if eq>0 and isfinite(eq):
                        answer["pb"]=answer["market_cap"]/eq
        return answer
    except (ValueError,TypeError,KeyError,AttributeError):
        return {"valid":False,"reason":"invalid_or_missing_inputs"}
