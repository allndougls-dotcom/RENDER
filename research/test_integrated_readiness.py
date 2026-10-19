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
  record={"ticker":"AAPL","asof":"2024-06-28","fields":{},"ttm":{},
          "eps":{"annual_growth":{"growth":0.25,"tag":"EarningsPerShareDiluted",
                 "available_on":"2024-02-01","new_period_end":"2023-12-31",
                 "old_period_end":"2022-12-31"}}}
  (root/"sec"/"AAPL.json").write_text(json.dumps(record))
  x=audit(root/"sec",root/"tech","2024-06-28")
  assert x["rows"][0]["checks"]["validated_eps_growth"]
  assert x["rows"][0]["eps_growth_annual_pit"]==0.25
  assert x["valid_combined_scores"]==0 and not x["backtest_ready"]
  record["eps"]["annual_growth"]["available_on"]="2024-09-01"
  (root/"sec"/"AAPL.json").write_text(json.dumps(record))
  x=audit(root/"sec",root/"tech","2024-06-28")
  assert not x["rows"][0]["checks"]["validated_eps_growth"]
def test_valuation_bridge():
 with tempfile.TemporaryDirectory() as t:
  root=Path(t);(root/"sec").mkdir();(root/"tech").mkdir()
  (root/"sec"/"X.json").write_text(json.dumps({"ticker":"X","asof":"2024-06-28","fields":{},"ttm":{}}))
  v=root/"values.json"
  v.write_text(json.dumps({"asof":"2024-06-28","rows":[{"ticker":"X","asof":"2024-06-28","valuation":{"valid":False,"pe":10,"pb":3,"market_cap":1000}}]}))
  report=audit(root/"sec",root/"tech","2024-06-28",valuation_file=v)
  assert not report["rows"][0]["checks"]["historical_pe_pb_market_cap"]
  v.write_text(json.dumps({"asof":"2024-06-28","rows":[{"ticker":"X","asof":"2024-06-28","valuation":{"valid":True,"pe":10,"pb":3,"market_cap":1000}}]}))
  report=audit(root/"sec",root/"tech","2024-06-28",valuation_file=v)
  assert report["rows"][0]["checks"]["historical_pe_pb_market_cap"]
  assert report["valid_combined_scores"]==0
  v.write_text(json.dumps({"asof":"2023-06-28","rows":[]}))
  try:audit(root/"sec",root/"tech","2024-06-28",valuation_file=v)
  except ValueError:pass
  else:raise AssertionError("Cross-date valuation was accepted")
if __name__=="__main__":
 test();test_valuation_bridge();print("PASS integrated valuation provenance audit")
