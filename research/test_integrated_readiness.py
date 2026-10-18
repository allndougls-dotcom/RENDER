import sys,tempfile,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from integrated_readiness import audit
def test():
 with tempfile.TemporaryDirectory() as t:
  root=Path(t);(root/"sec").mkdir();(root/"tech").mkdir()
  (root/"sec"/"AAPL.json").write_text(json.dumps({"ticker":"AAPL","asof":"2024-06-28","fields":{},"ttm":{}}))
  x=audit(root/"sec",root/"tech","2024-06-28")
  assert x["companies"]==1 and x["valid_combined_scores"]==0
  assert x["rows"][0]["combined_score"] is None
  assert x["missing"]["historical_technical_gates"]==1
if __name__=="__main__":
 test();print("PASS integrated incomplete data audit")
