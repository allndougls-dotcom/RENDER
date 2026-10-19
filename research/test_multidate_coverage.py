import json,tempfile
from pathlib import Path
from multidate_coverage import inspect
with tempfile.TemporaryDirectory() as t:
 p=Path(t)
 for day,s in [("2023-06-30",9.1),("2024-06-28",7.2),("2025-06-30",5.5)]:
  (p/(day+".json")).write_text(json.dumps({"asof":day,"rows":[{"ticker":"ABC","combined_exploratory":s,"eligible_historical_signal":False,"neutral_imputation_fields":["debt_equity"]}]}))
 r=inspect(p)
 assert r["dates"]==3 and r["gt_8_5_total"]==1 and r["between_6_5_8_5_total"]==1
 assert r["validated_win_rate"] is None
print("PASS date-level cohort coverage")
