import pandas as pd
import sys
sys.path.insert(0, "modules")
sys.path.insert(0, "modules/ingesta")
from sector_context import get_sector_context

df = pd.read_csv("data/master/sp500_full_export_20260808.csv")

errores = 0
for idx, row in df.iterrows():
    try:
        ctx = get_sector_context(row["ticker"], row["sector"])
    except Exception as e:
        errores += 1
        print("ERROR en", row["ticker"], "sector=", row["sector"], ":", e)
        if errores >= 5:
            break

print()
print("Total errores encontrados:", errores)
print("Hay NaN en columna sector?", df["sector"].isna().sum())
