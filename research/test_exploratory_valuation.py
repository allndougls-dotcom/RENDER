import json,tempfile
from pathlib import Path
from exploratory_valuation import compute
with tempfile.TemporaryDirectory() as tmp:
    base=Path(tmp); sec=base/"sec";prices=base/"prices";sec.mkdir();prices.mkdir()
    (sec/"ABC.json").write_text(json.dumps({"ticker":"ABC","fields":{
        "shares":{"value":100,"period_end":"2024-03-31","available_on":"2024-05-01"},
        "equity":{"value":2000,"period_end":"2024-03-31","available_on":"2024-05-01"}},
        "eps":{"annual":{"value":5,"period_end":"2023-12-31","available_on":"2024-02-01"}}}))
    (prices/"ABC.csv").write_text("Date,Close\n2024-06-28,50\n")
    (prices/"ABC.splits.json").write_text(json.dumps({"history_start":"2020-01-01",
        "history_end":"2026-10-01","events":[],"split_history_verified":False}))
    r=compute(sec,prices,"2024-06-28")
    assert r["full_candidates"]==1
    assert r["rows"][0]["certified"] is False and r["exploratory_only"] is True
    record=json.loads((sec/"ABC.json").read_text())
    record["fields"]["shares"]["available_on"]="2024-07-01"
    (sec/"ABC.json").write_text(json.dumps(record))
    r=compute(sec,prices,"2024-06-28")
    assert r["full_candidates"]==0
print("PASS exploratory valuation provenance and separation")
