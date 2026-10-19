"""Extrae fundamentales point-in-time de SEC CompanyFacts.
Solo datos con fecha filed <= sesión de señal. NO calcula todavía el score SIDI.
El score requiere además valoración histórica, técnicos y mediana sectorial.
Uso: python research/sec_pit_fundamentals.py --cik 320193 --asof 2024-06-28 --out /tmp/pit.json
Se requiere --user-agent con contacto real SEC, o variable SEC_USER_AGENT.
"""
import argparse,json,os,urllib.request
from datetime import date
from sec_ttm import observations, latest_ttm
from annual_growth_pit import annual_growth
from eps_pit import eps_annual, eps_annual_growth

TAGS = {
 "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax","RevenueFromContractWithCustomerIncludingAssessedTax","Revenues","SalesRevenueNet","SalesRevenueGoodsNet"],
 "net_income": ["NetIncomeLoss","ProfitLoss"],
 "assets": ["Assets"],
 "liabilities": ["Liabilities"],
 "equity": ["StockholdersEquity","StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
 "current_assets": ["AssetsCurrent"],
 "current_liabilities": ["LiabilitiesCurrent"],
 "operating_cashflow": ["NetCashProvidedByUsedInOperatingActivities"],
 "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
 "shares": ["CommonStockSharesOutstanding"],
 "long_term_debt_noncurrent": ["LongTermDebtNoncurrent"],
 "long_term_debt_current": ["LongTermDebtCurrent"],
 "short_term_borrowings": ["ShortTermBorrowings"],
}
def get(url,agent):
    req=urllib.request.Request(url,headers={"User-Agent":agent,"Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=40) as r:return json.load(r)
def pick(facts, names, asof, kind):
    """Latest available disclosure across equivalent tags, never latest revised today."""
    valid=[]
    for priority,tag in enumerate(names):
        units=facts.get("us-gaap",{}).get(tag,{}).get("units",{})
        rows=units.get("shares" if kind=="shares" else "USD",[])
        for item in rows:
            try:
                filed=date.fromisoformat(item["filed"])
                end=date.fromisoformat(item["end"])
                start=date.fromisoformat(item["start"]) if "start" in item else None
                value=float(item["val"])
            except (KeyError,ValueError,TypeError):continue
            if filed>asof or end>asof:continue
            if item.get("form") not in ("10-K","10-Q","10-K/A","10-Q/A"):continue
            if kind=="flow":
                if start is None:continue
                duration=(end-start).days
                if not (75<=duration<=380):continue
            elif start is not None:continue
            valid.append((end,filed,-priority,item.get("accn",""),value,start,tag))
    if not valid:return None
    end,filed,_,accn,value,start,tag=max(valid,key=lambda x:(x[0],x[1],x[2]))
    return dict(value=value,period_end=end.isoformat(),period_start=start.isoformat() if start else None,
                available_on=filed.isoformat(),accession=accn,tag=tag)

def snapshot(cik,asof,agent):
    facts=get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",agent)
    fields={name:pick(facts["facts"],tags,asof,"flow" if name in
          {"revenue","net_income","operating_cashflow","capex"} else "shares" if name=="shares" else "balance")
          for name,tags in TAGS.items()}
    # Prior equity is selected as it was known at asof, with a prior balance date.
    current_equity=fields.get("equity")
    prior_equity=None
    if current_equity:
        earlier=date.fromisoformat(current_equity["period_end"])
        prior_asof=min(asof,earlier)
        prior_equity=pick(facts["facts"],TAGS["equity"],prior_asof,"balance")
        if prior_equity and prior_equity["period_end"]>=current_equity["period_end"]:
            prior_equity=None
    fields["equity_prior"]=prior_equity
    ttm_tags={"revenue":TAGS["revenue"],"net_income":TAGS["net_income"],
              "operating_cashflow":TAGS["operating_cashflow"],"capex":TAGS["capex"]}
    ttm={name:latest_ttm(observations(facts["facts"],tags,asof)) for name,tags in ttm_tags.items()}
    growth={"revenue_annual":annual_growth(facts["facts"],TAGS["revenue"],asof),
            "net_income_annual":annual_growth(facts["facts"],TAGS["net_income"],asof)}
    eps={"annual":eps_annual(facts["facts"],asof),"annual_growth":eps_annual_growth(facts["facts"],asof)}
    return {"cik":int(cik),"entity":facts.get("entityName"),"asof":asof.isoformat(),
            "ttm":ttm,"annual_growth":growth,"eps":eps,
            "point_in_time":True,"source":"SEC companyfacts","fields":fields,
            "missing":[name for name,val in fields.items() if val is None],
            "warning":"Not a SIDI score. Period length, TTM, sector medians, valuation and historical price must be reconstructed separately."}
if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--cik",type=int,required=True)
    p.add_argument("--asof",type=date.fromisoformat,required=True)
    p.add_argument("--user-agent",default=os.environ.get("SEC_USER_AGENT",""))
    p.add_argument("--out",required=True)
    args=p.parse_args()
    if "@" not in args.user_agent: p.error("SEC user-agent needs a real contact email")
    data=snapshot(args.cik,args.asof,args.user_agent)
    from pathlib import Path
    Path(args.out).parent.mkdir(parents=True,exist_ok=True)
    Path(args.out).write_text(json.dumps(data,indent=2),encoding="utf-8")
    print("Saved point-in-time SEC fields, missing:",data["missing"])
