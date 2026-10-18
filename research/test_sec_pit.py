"""Offline regression checks for SEC PIT field selection."""
from datetime import date
from sec_pit_fundamentals import pick

facts={"us-gaap":{
 "Revenues":{"units":{"USD":[
  {"start":"2023-01-01","end":"2023-12-31","filed":"2024-02-15","form":"10-K","val":100,"accn":"old"},
  {"start":"2023-01-01","end":"2023-12-31","filed":"2024-09-15","form":"10-K/A","val":999,"accn":"future"},
 ]}},
 "RevenueFromContractWithCustomerExcludingAssessedTax":{"units":{"USD":[
  {"start":"2022-01-01","end":"2022-12-31","filed":"2023-02-15","form":"10-K","val":77,"accn":"older"},
 ]}}
}}
def test_no_restatement_leak():
 row=pick(facts,["RevenueFromContractWithCustomerExcludingAssessedTax","Revenues"],date(2024,6,28),"flow")
 assert row["value"]==100 and row["accession"]=="old",row
 assert row["available_on"]<="2024-06-28"
def test_no_past_instant_for_flow():
 row=pick(facts,["Revenues"],date(2023,1,1),"flow")
 assert row is None
if __name__=="__main__":
 test_no_restatement_leak()
 test_no_past_instant_for_flow()
 print("SEC PIT regression tests passed")
