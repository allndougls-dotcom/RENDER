import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from score_readiness import audit
def test():
 x=audit([{"ticker":"AAPL","fields":{"revenue":{"value":99}}}])
 assert x["ready"]==0 and x["not_ready"]==1
 assert x["quality_gt_8_5_count"] is None
 assert x["valid_backtest_signals"]==0
if __name__=="__main__":
 test();print("PASS score readiness excludes incomplete snapshots")
