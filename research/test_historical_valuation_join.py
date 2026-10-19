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
    action={"events":[],"split_history_verified":False,"history_end":"2026-01-01"}
    (prices/"TEST.splits.json").write_text(json.dumps(action))
    result=build(sec,prices,"2024-06-28")
    assert result["companies"]==1
    assert result["pe_coverage"]==0 and result["pb_coverage"]==0
    assert result["blocked"]["missing_independent_attestation"]==1
    attest=base/"attest";attest.mkdir()
    (attest/"TEST.json").write_text(json.dumps({
        "review_status":"confirmed","reviewer":"independent analyst",
        "independent_source_url":"https://example.org/reviewed-corporate-actions",
        "coverage_start":"2020-01-01","coverage_end":"2026-01-01",
        "events":[]}))
    result=build(sec,prices,"2024-06-28",attest)
    assert result["pe_coverage"]==1 and result["pb_coverage"]==1
    assert result["market_cap_coverage"]==1
print("PASS valuation join coverage and fail-closed rules")
