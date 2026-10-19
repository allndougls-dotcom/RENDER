import tempfile,json
from pathlib import Path
from quality_cohort_comparison import compare
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp)
 (p/"A.csv").write_text("Date,Open,High,Low,Close,Volume\n2024-06-28,100,100,100,100,1\n2024-07-01,100,112,98,108,1\n")
 (p/"B.csv").write_text("Date,Open,High,Low,Close,Volume\n2024-06-28,100,100,100,100,1\n2024-07-01,100,102,88,90,1\n")
 rows=[{"ticker":"A","combined_exploratory":9.1,"certified":False,"eligible_historical_signal":False},
       {"ticker":"B","combined_exploratory":7.3,"certified":False,"eligible_historical_signal":False}]
 snapshot={"asof":"2024-06-28","validated_combined_scores":0,"rows":rows}
 result=compare([snapshot],p,stop=.10,horizons=(1,),targets=(.10,))
 high=next(x for x in result["comparison"] if x["cohort"]=="GT_8_5")
 low=next(x for x in result["comparison"] if x["cohort"]=="6_5_TO_8_5")
 assert high["target_hits"]==1 and low["target_hits"]==0
 assert result["dates_count"]==1 and result["verified_win_rate"] is None
 try:compare([snapshot,snapshot],p)
 except ValueError:pass
 else:raise AssertionError("Duplicate historical asof accepted")
print("PASS dated cohort comparison with no fabricated backtest")
