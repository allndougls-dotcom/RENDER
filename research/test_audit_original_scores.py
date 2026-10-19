import csv,tempfile
from pathlib import Path
from audit_original_scores import audit
with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    (root/"sp500_full_export_2026-08-04.csv").write_text(
        "ticker,combined_score,fund_score,tech_score\nAAA,8.72,8.50,9.05\nBBB,7.99,8,8\nCCC,8.20,8,8.5\n")
    (root/"sp500_full_export_2026-07-30.csv").write_text(
        "ticker,combined_score\nPRE,9.99\n")
    (root/"sp500_full_export_unknown.csv").write_text(
        "ticker,combined_score\nUNDATED,9.5\n")
    result=audit(root)
    assert result["over_8_rows"]==2, result
    assert result["over_8_5_rows"]==1
    assert result["max_observed"]==8.72
    assert all(x["date"]>="2026-08-01" for x in result["hits"])
print("PASS original score audit")
