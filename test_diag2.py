import pandas as pd
import sys
sys.path.insert(0, "modules")
sys.path.insert(0, "modules/ingesta")
from sector_context import get_sector_context

df = pd.read_csv("data/master/sp500_full_export_20260808.csv")

def _sector_ctx(row):
    ctx = get_sector_context(row.get("ticker", ""), row.get("sector", ""))
    return pd.Series({
        "sector_etf":               ctx["sector_etf"],
        "subsector":                ctx["subsector"],
        "peer_group":               "|".join(ctx["peer_group"]) if ctx["peer_group"] else "",
        "critical_macro_variables": "|".join(ctx["critical_macro_variables"]) if ctx["critical_macro_variables"] else "",
    })

sector_ctx_df = df.apply(_sector_ctx, axis=1)
print(sector_ctx_df[sector_ctx_df["sector_etf"] != ""].head(10))
print()
print("Total con sector_etf no vacio:", (sector_ctx_df["sector_etf"] != "").sum(), "de", len(df))
print()
gev_idx = df[df["ticker"]=="GEV"].index[0]
print("Fila GEV en sector_ctx_df:")
print(sector_ctx_df.loc[gev_idx])
