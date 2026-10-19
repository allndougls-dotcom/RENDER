from valuation_pit import historical_valuation
base=dict(asof="2024-06-28",price=50,price_date="2024-06-28",
 price_basis="yahoo_close_split_adjusted_no_dividends",eps=5,
 eps_period_end="2023-12-31",shares=100,shares_period_end="2024-03-31",
 equity=2000,equity_period_end="2024-03-31",
 split_history_verified=True,split_events=[])
r=historical_valuation(**base)
assert r["pe"]==10 and r["pb"]==2.5 and r["market_cap"]==5000,r
r=historical_valuation(**{**base,"split_events":[{"date":"2024-08-01","ratio":2}]})
assert r["pe"]==20 and r["market_cap"]==10000,r
r=historical_valuation(**{**base,"split_history_verified":False})
assert r["valid"] is False and r["reason"]=="split_history_not_verified"
r=historical_valuation(**{**base,"price_basis":"dividend_adjusted"})
assert r["valid"] is False
r=historical_valuation(**{**base,"split_events":[{"date":"2024-05-01","ratio":2}]})
assert r["pe"] is None and r["market_cap"]==10000 and r["pb"]==5,r
print("PASS historical valuation guardrails")
