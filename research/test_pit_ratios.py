"""SEC ratio reconstruction tests: missing fields and future filings must fail safely."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from pit_ratios import compute

def test():
 data={"ticker":"TEST","asof":"2024-06-28","fields":{
  "net_income":{"value":20,"available_on":"2024-05-01","period_end":"2024-03-31"},
  "equity":{"value":100,"available_on":"2024-05-01","period_end":"2024-03-31"}}}
 x=compute(data)
 assert x["ratios"]["roe_proxy"]==0.2
 assert x["ratios"]["fcf_proxy"] is None
 assert x["comparable_sidi_score"] is False
 data["fields"]["equity"]["available_on"]="2024-07-01"
 try:compute(data)
 except ValueError:pass
 else:raise AssertionError("Future disclosure accepted")
if __name__=="__main__":
 test();print("PASS ratio PIT tests")
