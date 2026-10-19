from datetime import date
from eps_pit import eps_annual,eps_annual_growth
f={"us-gaap":{"EarningsPerShareDiluted":{"units":{"USD/shares":[
{"start":"2022-01-01","end":"2022-12-31","filed":"2023-02-01","form":"10-K","val":2.0},
{"start":"2023-01-01","end":"2023-12-31","filed":"2024-02-01","form":"10-K","val":2.5},
{"start":"2023-01-01","end":"2023-12-31","filed":"2024-09-01","form":"10-K/A","val":900}
]}}}}
assert eps_annual(f,date(2024,6,28))["value"]==2.5
assert abs(eps_annual_growth(f,date(2024,6,28))["growth"]-.25)<1e-8
print("PASS SEC historical EPS")
