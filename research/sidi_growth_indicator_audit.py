"""SIDI Growth V0: audit indicator availability, not a live screening engine.
Does not change FULL SIDI / WHITE BIRD or treat today's Yahoo data as PIT.
"""
import json,math
from collections import Counter
from pathlib import Path
import yfinance as yf

OUT=Path("results/sidi_growth/indicator_audit")
TICKERS=("AAPL","MSFT","NVDA","AMZN","JPM","XOM","NEE","KO","UNH","MU")
INFO={
 "net_debt_ebitda":("totalDebt","totalCash","ebitda"),
 "interest_coverage":("interestCoverage",),
 "fcf_4q":("freeCashflow",),
 "roic":("returnOnInvestedCapital",),
 "operating_margin":("operatingMargins",),
 "fcf_ni":("freeCashflow","netIncomeToCommon"),
 "fcf_yield":("freeCashflow","marketCap"),
 "ev_ebitda":("enterpriseToEbitda",),
 "peg":("pegRatio",),
 "current_ratio":("currentRatio",),
 "altman_z":("totalAssets","totalLiab","workingCapital","retainedEarnings","ebit","marketCap","totalRevenue"),
 "eps_revisions":("earningsGrowth",),
 "repurchases":("sharesOutstanding",),
 "insider_purchases":(),
 "52w_high_drawdown":("fiftyTwoWeekHigh","currentPrice"),
 "distance_sma200":("twoHundredDayAverage","currentPrice"),
}
# Approximate statement labels vary across issuers. Presence is diagnostic only.
STATEMENT_ALIASES={
 "ebit":["EBIT","Operating Income"],
 "interest_expense":["Interest Expense","Interest Expense Non Operating"],
 "capital_employed":["Total Assets","Stockholders Equity","Total Debt"],
 "fcf":["Free Cash Flow","Operating Cash Flow","Capital Expenditure"],
 "net_income":["Net Income","Net Income Common Stockholders"],
 "assets":["Total Assets"],
 "liabilities":["Total Liabilities Net Minority Interest"],
 "retained_earnings":["Retained Earnings"],
 "buybacks":["Repurchase Of Capital Stock","Common Stock Repurchase"],
}
def number(x):
 try:
  v=float(x)
  return v if math.isfinite(v) else None
 except (ValueError,TypeError):return None
def rows(df):
 try:return [str(x) for x in df.index] if df is not None else []
 except (TypeError,AttributeError):return []
def run():
 OUT.mkdir(parents=True,exist_ok=True)
 records=[]
 for ticker in TICKERS:
  try:
   obj=yf.Ticker(ticker);info=obj.info or {}
   statements=rows(obj.income_stmt)+rows(obj.balance_sheet)+rows(obj.cashflow)
   metrics={}
   for metric,keys in INFO.items():
    info_present=bool(keys) and all(number(info.get(k)) is not None for k in keys)
    statement_possible=any(any(alias.lower() in x.lower() for x in statements) for alias in STATEMENT_ALIASES.get(metric,[]))
    metrics[metric]={"info_keys":list(keys),"info_present":info_present,
        "statement_possible":statement_possible,"historical_PIT_verified":False}
   records.append({"ticker":ticker,"status":"ok","metrics":metrics,
       "statement_rows":len(statements)})
   print(ticker,"ok",flush=True)
  except Exception as exc:
   records.append({"ticker":ticker,"status":"error","reason":type(exc).__name__+":"+str(exc)[:160]})
   print(ticker,"error",flush=True)
 good=[r for r in records if r["status"]=="ok"]
 coverage={metric:{"info_complete":sum(r["metrics"][metric]["info_present"] for r in good),
   "total_queried":len(good),"pit_verified":0} for metric in INFO}
 summary={"source":"yfinance live snapshot: not point-in-time","ticker_sample":list(TICKERS),
   "coverage":coverage,"records":records,
   "interpretation":{
    "interest_coverage":"Check income statements; info key availability does not prove equivalent EBIT/interest definition",
    "free_cash_flow_4_quarters":"Annual or latest cash-flow value is NOT proof of 4 consecutive positive standalone quarters",
    "altman_z":"Needs historical statement components and industry-specific applicability; do not enforce for financials",
    "eps_revisions":"earningsGrowth is NOT an EPS estimate revision series; requires historical analyst estimates",
    "insider_purchases":"Requires timestamped insider transaction filings and role/transaction classification",
    "sector_ratios":"Normalize by sector with contemporaneous inputs; no current snapshot in historical backtest",
    "52w_drawdown_sma200":"Recompute from strictly historical price bars; current .info values unsuitable for PIT",
    "roic":"Needs consistent NOPAT and invested capital definitions; verify statement calculations",
    "buybacks":"Shares outstanding alone is not sufficient for historical buyback amount"
   },
   "warning":"Do not infer a valid growth score or backtest from present-day field coverage."}
 (OUT/"report.json").write_text(json.dumps(summary,indent=2,ensure_ascii=False))
 print(json.dumps({"tickers_ok":len(good),"coverage":coverage},indent=2))
if __name__=="__main__":run()
