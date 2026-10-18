"""Synthetic no-lookahead tests for PIT technical gate reconstruction."""
import sys
import tempfile
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.technical_pit import reconstruct

def test_no_lookahead():
    x=np.arange(160,dtype=float)
    close=160-np.minimum(x,100)*0.5+np.maximum(x-100,0)*0.2
    d=pd.DataFrame({"Date":pd.date_range("2023-01-01",periods=160,freq="B"),
                    "Open":close,"High":close+1,"Low":close-1,"Close":close,
                    "Volume":np.full(160,100000)})
    with tempfile.TemporaryDirectory() as tmp:
        a=Path(tmp)/"A.csv";b=Path(tmp)/"B.csv"
        d.to_csv(a,index=False)
        changed=d.copy();changed.loc[120:,"Close"]=changed.loc[120:,"Close"]*3
        changed.to_csv(b,index=False)
        original=reconstruct(a);modified=reconstruct(b)
        cols=["rsi_14","drawdown_60d","sidi_pass_dd12","sidi_pass_rsi40",
              "sidi_pass_macd","sidi_pass_volume","setup_hot"]
        pd.testing.assert_frame_equal(original.iloc[:120][cols],modified.iloc[:120][cols])
        assert not original["drawdown_60d"].iloc[:59].notna().any()

if __name__=="__main__":
    test_no_lookahead()
    print("PASS technical point-in-time invariance")
