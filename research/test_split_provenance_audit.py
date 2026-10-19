import json,tempfile
from pathlib import Path
from split_provenance_audit import audit
with tempfile.TemporaryDirectory() as d:
    sec=Path(d)/"sec";px=Path(d)/"prices";sec.mkdir();px.mkdir()
    (sec/"X.json").write_text(json.dumps({"ticker":"X","eps":{"annual":{"period_end":"2023-12-31"}},"fields":{}}))
    def save(start):
        (px/"X.splits.json").write_text(json.dumps({"history_start":start,"history_end":"2026-01-01","events":[]}))
    save("2024-01-01")
    assert audit(sec,px,"2024-06-28")["blockers"]["action_history_starts_after_filing_period"]==1
    save("2020-01-01")
    result=audit(sec,px,"2024-06-28")
    assert result["independently_verified"]==0
    assert result["blockers"]["needs_independent_corporate_action_confirmation"]==1
print("PASS split provenance audit")