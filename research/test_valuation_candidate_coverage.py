import json,tempfile
from pathlib import Path
from valuation_candidate_coverage import report
with tempfile.TemporaryDirectory() as t:
    base=Path(t);sec=base/"sec";prices=base/"prices";sec.mkdir();prices.mkdir()
    (sec/"AAA.json").write_text(json.dumps({"ticker":"AAA",
        "eps":{"annual":{"value":4,"period_end":"2023-12-31","available_on":"2024-02-01"}},
        "fields":{"shares":{"value":100,"period_end":"2024-03-31","available_on":"2024-05-01"},
                  "equity":{"value":1000,"period_end":"2024-03-31","available_on":"2024-05-01"}}}))
    (prices/"AAA.csv").write_text("Date,Close\n2024-06-28,50\n")
    (prices/"AAA.splits.json").write_text(json.dumps({"events":[],"history_start":"2020-01-01","history_end":"2026-01-01"}))
    r=report(sec,prices,"2024-06-28")
    assert r["input_complete"]==1 and r["rows"][0]["valuation_verified"] is False
    data=json.loads((sec/"AAA.json").read_text())
    data["fields"]["shares"]["available_on"]="2024-08-01"
    (sec/"AAA.json").write_text(json.dumps(data))
    r=report(sec,prices,"2024-06-28")
    assert r["input_complete"]==0 and r["missing_by_component"]["shares_reported"]==1
print("PASS valuation input coverage, no false verification")