from ttm_metrics import compute
base={"ticker":"ABC","asof":"2024-06-28",
      "ttm":{"net_income":{"value":50,"period_end":"2024-03-31"}},
      "fields":{}}
def fact(v,end="2024-03-31",filed="2024-05-01"):
 return {"value":v,"period_end":end,"available_on":filed}
base["fields"]={"equity":fact(300),"equity_prior":fact(200,"2023-03-31","2023-05-01"),
 "long_term_debt_noncurrent":fact(60),"long_term_debt_current":fact(20),
 "short_term_borrowings":fact(10)}
v=compute(base)["metrics"]
assert abs(v["roe_ttm_avg_equity_exploratory"]-.20)<1e-10
assert abs(v["debt_equity_interest_bearing_exploratory_pct"]-30)<1e-10
base["fields"]["long_term_debt_current"]=None
assert compute(base)["metrics"]["debt_equity_interest_bearing_exploratory_pct"] is None
base["fields"]["long_term_debt_current"]=fact(20)
base["fields"]["short_term_borrowings"]["period_end"]="2023-12-31"
assert compute(base)["metrics"]["debt_equity_interest_bearing_exploratory_pct"] is None
base["fields"]["short_term_borrowings"]=fact(10,"2024-03-31","2024-08-01")
try:compute(base)
except ValueError:pass
else:raise AssertionError("Future disclosure not rejected")
print("PASS exploratory ROE and financial debt strict dates")
