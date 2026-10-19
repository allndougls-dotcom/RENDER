import json,tempfile
from pathlib import Path
from exploratory_forward_outcomes import run
with tempfile.TemporaryDirectory() as tmp:
    p=Path(tmp)
    (p/"ABC.csv").write_text("Date,Open,High,Low,Close,Volume\n2024-06-28,10,11,9,10,100\n2024-07-01,100,111,89,105,100\n2024-07-02,105,112,99,111,100\n")
    rank={"asof":"2024-06-28","rows":[{"ticker":"ABC","combined_exploratory":9.1}]}
    outcome=run(rank,p,horizons=(1,2),targets=(.10,.15),stop=.10)
    a=[r for r in outcome["results"] if r["tp_pct"]==10 and r["horizon_sessions"]==1][0]
    assert a["entry_date"]=="2024-07-01"
    assert a["status"]=="stop_first"
    no_stop=run(rank,p,horizons=(1,2,5),targets=(.10,.15))
    hit=[r for r in no_stop["results"] if r["tp_pct"]==10 and r["horizon_sessions"]==1][0]
    assert hit["status"]=="target"
    censored=[r for r in no_stop["results"] if r["tp_pct"]==15 and r["horizon_sessions"]==5][0]
    assert censored["status"]=="censored"
    assert no_stop["verified_win_rate"] is None and not no_stop["validated_backtest"]
print("PASS exploratory next-open, target, stop-first and censoring")
