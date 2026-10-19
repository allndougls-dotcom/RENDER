import json,tempfile
from pathlib import Path
from exploratory_quality_ranking import rank
with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    sec=root/"sec"
    sec.mkdir()
    (root/"universe.csv").write_text("ticker,sector\nAAA,Technology\n")
    (sec/"AAA.json").write_text(json.dumps({"ticker":"AAA","asof":"2024-06-28","fields":{},"ttm":{},"eps":{},"annual_growth":{}}))
    (root/"values.json").write_text(json.dumps({"asof":"2024-06-28","rows":[{"ticker":"AAA","asof":"2024-06-28","exploratory":True,"certified":False,"pe":12,"pb":2,"market_cap":1000}]}))
    (root/"tech.json").write_text(json.dumps({"rows":[{"ticker":"AAA","asof":"2024-06-28","tech_score":9}]}))
    result=rank(sec,root/"universe.csv",root/"values.json",root/"tech.json","2024-06-28")
    assert result["ranked"]==1
    assert result["validated_combined_scores"]==0
    assert result["rows"][0]["eligible_historical_signal"] is False
    assert "debt_equity" in result["rows"][0]["neutral_imputation_fields"]
    try:
        rank(sec,root/"universe.csv",root/"values.json",root/"tech.json","2023-06-28")
    except ValueError:
        pass
    else:
        raise AssertionError("Future valuation accepted")
print("PASS exploratory ranking isolation and PIT date cutoff")
