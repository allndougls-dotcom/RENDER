import csv,json,tempfile
from pathlib import Path
from split_review_queue import queue
with tempfile.TemporaryDirectory() as d:
    d=Path(d)
    (d/"universe.csv").write_text("ticker,cik\nTEST,320193\n")
    (d/"candidates.json").write_text(json.dumps({"rows":[{"ticker":"TEST",
      "yahoo_events":[{"date":"2024-08-01","ratio":2}],
      "sec_candidates":[{"ratio":2,"accession":"0000320193-24-000123"}]}]}))
    result=queue(d/"universe.csv",d,d/"candidates.json","2024-06-28")
    assert result["verified"]==0
    assert result["rows"][0]["relative_to_asof"]=="future"
    assert result["rows"][0]["sec_filing_directories"][0].endswith("/000032019324000123/")
print("PASS split document review queue")
