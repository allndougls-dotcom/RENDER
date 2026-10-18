from datetime import date
from sec_ttm import observations,latest_ttm
def test():
    facts={"us-gaap":{"NetIncomeLoss":{"units":{"USD":[
      {"start":"2023-01-01","end":"2023-12-31","filed":"2024-02-10","form":"10-K","val":100},
      {"start":"2023-01-01","end":"2023-12-31","filed":"2024-09-01","form":"10-K/A","val":900}
    ]}}}}
    obs=observations(facts,["NetIncomeLoss"],date(2024,6,28))
    result=latest_ttm(obs)
    assert result["value"]==100 and result["method"]=="ANNUAL",result
    assert all(x["filed"]<="2024-06-28" for x in result["sources"])
if __name__=="__main__":
 test();print("PASS TTM point-in-time audit")
