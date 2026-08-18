import pandas as pd
import sys
sys.path.insert(0, "modules")
sys.path.insert(0, "modules/ingesta")
from ingesta.scoring import calcular_scores

df = pd.read_csv("data/master/sp500_full_export_20260808.csv")

# Reconstruir los inputs que espera calcular_scores a partir del CSV ya exportado
sp500 = df[["ticker","sector"]].copy()
df_tech = df[["ticker","tech_score","drawdown_60d","setup_hot","rsi_14","near_support","macd_improving","trend_bias"]].copy()
df_fund = df[["ticker","revenue_growth","eps_growth","roe","debt_equity","current_ratio","fcf_ni_ratio","pe","pb"]].copy()
df_fund["fcf"] = 1e9
df_fund["market_cap"] = 3e10

result = calcular_scores(sp500, df_tech, df_fund)

gev = result[result["ticker"]=="GEV"]
print()
print("GEV tras calcular_scores completo:")
print(gev[["ticker","sector","sector_etf","peer_group"]].to_string(index=False))
print()
print("Total columnas en resultado:", len(result.columns))
print("sector_etf esta en columnas?", "sector_etf" in result.columns)
