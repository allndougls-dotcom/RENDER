"""Calculate exact production SIDI tech_score on data available by as-of date.
Uses the production scoring implementation, no alternative weights.
Historical score is technical ONLY and does not imply a valid combined score.
"""
import argparse,json,sys
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"modules"/"ingesta"))
from tecnico import calcular_tecnicos

def calculate(path,asof):
    df=pd.read_csv(path,parse_dates=["Date"])
    df=df.loc[df["Date"]<=pd.Timestamp(asof)].sort_values("Date").drop_duplicates("Date",keep="last")
    if len(df)<221: return None
    df=df.set_index("Date")
    # Original production method expects daily OHLCV indexed by date.
    result=calcular_tecnicos({path.stem:df})
    if result.empty:return None
    row=result.iloc[0]
    if str(row["price_date"])>asof:raise AssertionError("Future technical observation")
    score=float(row["tech_score"])
    if not (0<=score<=10):raise ValueError("Invalid score")
    return {"ticker":path.stem,"asof":asof,"price_date":str(row["price_date"]),
            "tech_score":score,"score_source":"modules/ingesta/tecnico.py",
            "observations":len(df),"combined_score":None}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--prices",default="data/backtest_prices")
    p.add_argument("--asof",required=True)
    p.add_argument("--out",default="results/quality_swing/exact_technical_scores.json")
    args=p.parse_args()
    results=[];failures=[]
    for path in sorted(Path(args.prices).glob("*.csv")):
        try:
            row=calculate(path,args.asof)
            if row is not None:results.append(row)
            else:failures.append({"ticker":path.stem,"reason":"fewer than 221 historical sessions or production calculation empty"})
        except Exception as exc:failures.append({"ticker":path.stem,"reason":str(exc)[:180]})
    coverage={"asof":args.asof,"technical_scores":len(results),"failed":failures,
              "rows":results,"combined_scores":0,"backtest_ready":False}
    target=Path(args.out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(coverage,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in coverage.items() if k!="rows"},indent=2))
if __name__=="__main__":main()
