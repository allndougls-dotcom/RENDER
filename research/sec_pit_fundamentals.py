"""Extrae fundamentales point-in-time de SEC CompanyFacts.
Solo datos con fecha filed <= sesión de señal. NO calcula todavía el score SIDI.
El score requiere además valoración histórica, técnicos y mediana sectorial.
Uso: python research/sec_pit_fundamentals.py --cik 320193 --asof 2024-06-28 --out /tmp/pit.json
Se requiere --user-agent con contacto real SEC, o variable SEC_USER_AGENT.
"""
import argparse,json,os,urllib.request
from datetime import date

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
}
def get(url,agent):
    req=urllib.request.Request(url,headers={"User-Agent":agent,"Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=40) as r:return json.load(r)
def pick(facts, names, asof, kind):
    for tag in names:
        units=facts.get("us-gaap",{}).get(tag,{}).get("units",{})
        rows=units.get("shares" if kind=="shares" else "USD",[])
        valid=[]
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
            else:
                if start is not None:continue
            valid.append((end,filed,item.get("accn",""),value,start,tag))
        if valid:
            end,filed,accn,value,start,selected_tag=max(valid,key=lambda x:(x[0],x[1]))
            return dict(value=value,period_end=end.isoformat(),period_start=start.isoformat() if start else None,
                        available_on=filed.isoformat(),accession=accn,tag=selected_tag)
    return None

def snapshot(cik,asof,agent):
    facts=get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",agent)
    fields={name:pick(facts["facts"],tags,asof,"flow" if name in
          {"revenue","net_income","operating_cashflow","capex"} else "shares" if name=="shares" else "balance")
          for name,tags in TAGS.items()}
    return {"cik":int(cik),"entity":facts.get("entityName"),"asof":asof.isoformat(),
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
