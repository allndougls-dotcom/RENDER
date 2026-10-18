import sys
from pathlib import Path
from datetime import date
sys.path.insert(0,str(Path(__file__).resolve().parent))
from annual_growth_pit import annual_growth
def test():
 f={"us-gaap":{"Revenues":{"units":{"USD":[
 {"start":"2022-01-01","end":"2022-12-31","filed":"2023-02-10","form":"10-K","val":100},
 {"start":"2023-01-01","end":"2023-12-31","filed":"2024-02-10","form":"10-K","val":120},
 {"start":"2023-01-01","end":"2023-12-31","filed":"2024-09-10","form":"10-K/A","val":900}
 ]}}}}
 r=annual_growth(f,["Revenues"],date(2024,6,28))
 assert r and abs(r["growth"]-0.2)<1e-9,r
if __name__=="__main__":test();print("PASS PIT growth")
