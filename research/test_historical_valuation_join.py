import json,tempfile
from pathlib import Path
from historical_valuation_join import build

with tempfile.TemporaryDirectory() as tmp:
    base=Path(tmp); sec=base/"sec";prices=base/"prices";sec.mkdir();prices.mkdir()
    record={"ticker":"TEST","asof":"2024-06-28",
            "eps":{"annual":{"value":4.0,"period_end":"2023-12-31"}},
            "fields":{"shares":{"value":100,"period_end":"2024-03-31"},
                      "equity":{"value":1000,"period_end":"2024-03-31"}}}
    (sec/"TEST.json").write_text(json.dumps(record))
    (prices/"TEST.csv").write_text("Date,Close\n2024-06-28,80\n")
    action={"events":[],"split_history_verified":False}
    (prices/"TEST.splits.json").write_text(json.dumps(action))
    result=build(sec,prices,"2024-06-28")
    assert result["companies"]==1
    assert result["pe_coverage"]==0 and result["pb_coverage"]==0
    assert result["blocked"]["split_history_not_verified"]==1
    action["split_history_verified"]=True
    (prices/"TEST.splits.json").write_text(json.dumps(action))
    result=build(sec,prices,"2024-06-28")
    assert result["pe_coverage"]==1 and result["pb_coverage"]==1
    assert result["market_cap_coverage"]==1
print("PASS valuation join coverage and fail-closed rules")
