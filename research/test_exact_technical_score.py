"""Regression: adding future prices cannot change historical production tech_score."""
import sys,tempfile
from pathlib import Path
import numpy as np,pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parent))
from exact_technical_score import calculate
def test():
 with tempfile.TemporaryDirectory() as folder:
  p=Path(folder)/"TEST.csv"
  d=pd.DataFrame({"Date":pd.date_range("2022-01-03",periods=280,freq="B"),
    "Open":100+np.arange(280)*.05,"High":101+np.arange(280)*.05,
    "Low":99+np.arange(280)*.05,"Close":100+np.arange(280)*.05,
    "Volume":[100000+i%20*100 for i in range(280)]})
  d.to_csv(p,index=False)
  day=str(d.iloc[249]["Date"].date())
  a=calculate(p,day)
  d.loc[250:,"Close"]+=5000
  d.to_csv(p,index=False)
  b=calculate(p,day)
  assert a and b and a["tech_score"]==b["tech_score"],(a,b)
if __name__=="__main__":test();print("PASS future price invariance")
