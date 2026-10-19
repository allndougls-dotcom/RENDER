import csv,json,tempfile
from pathlib import Path
from exploratory_sector_medians import summarize

with tempfile.TemporaryDirectory() as root:
    universe=Path(root)/"universe.csv"
    universe.write_text("ticker,sector\nAAA,Technology\nBBB,Technology\nCCC,Technology\nDDD,Health Care\n",encoding="utf-8")
    vals={"asof":"2024-06-28","rows":[
        {"ticker":"AAA","exploratory":True,"certified":False,"pe":10,"pb":2,"market_cap":100},
        {"ticker":"BBB","exploratory":True,"certified":False,"pe":20,"pb":4,"market_cap":200},
        {"ticker":"CCC","exploratory":True,"certified":False,"pe":30,"pb":6,"market_cap":300},
        {"ticker":"DDD","exploratory":True,"certified":False,"pe":100,"pb":10,"market_cap":500}]}
    r=summarize(vals,universe,min_peers=3)
    assert r["sector_medians"]["Technology"]["pe"]["median"]==20
    assert r["sector_medians"]["Technology"]["pb"]["n"]==3
    assert r["sector_medians"]["Health Care"]["pe"]["median"] is None
    assert r["may_be_used_for_sidi_score"] is False and not r["historical_membership"]
    vals["rows"][2]["certified"]=True
    r=summarize(vals,universe,min_peers=3)
    assert r["sector_medians"]["Technology"]["pe"]["median"] is None
print("PASS exploratory sector medians; incomplete cohorts remain null")
