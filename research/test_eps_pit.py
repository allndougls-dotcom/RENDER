from datetime import date
from eps_pit import eps_annual, eps_annual_growth
items=[
 {"start":"2022-01-01","end":"2022-12-31","filed":"2023-02-01","form":"10-K","val":2.0,"accn":"old"},
 {"start":"2022-01-01","end":"2022-12-31","filed":"2024-02-01","form":"10-K","val":1.0,"accn":"new"},
 {"start":"2023-01-01","end":"2023-12-31","filed":"2024-02-01","form":"10-K","val":2.5,"accn":"new"},
 {"start":"2023-01-01","end":"2023-12-31","filed":"2024-09-01","form":"10-K/A","val":900,"accn":"future"}]
f={"us-gaap":{"EarningsPerShareDiluted":{"units":{"USD/shares":items}}}}
assert eps_annual(f,date(2024,6,28))["value"]==2.5
growth=eps_annual_growth(f,date(2024,6,28))
assert growth["growth"]==1.5 and growth["accession"]=="new",growth
assert growth["comparability"]=="same_filing_diluted_eps"
# Without comparative 2022 EPS in the new filing, don't mix pre-split bases.
f2={"us-gaap":{"EarningsPerShareDiluted":{"units":{"USD/shares":[items[0],items[2]]}}}}
assert eps_annual_growth(f2,date(2024,6,28)) is None
print("PASS point-in-time same-filing diluted EPS comparability")
