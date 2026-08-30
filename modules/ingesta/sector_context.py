"""
Mapeo estático sector GICS -> ETF sectorial + grupo de pares representativos.
No requiere API externa. Se usa para dar contexto relativo al analista de noticias
(ej: "NEM cae -15%, pero el ETF GDX solo cae -5% -> caida especifica de la empresa").

Referencia: los 11 sectores GICS que ya usa tu scoring.py/sp500 (columna 'sector').
"""

# sector (tal y como aparece en tu columna 'sector' del S&P500) -> ETF representativo
SECTOR_ETF_MAP = {
    "Materials":              "XLB",
    "Communication Services": "XLC",
    "Energy":                 "XLE",
    "Financials":             "XLF",
    "Industrials":             "XLI",
    "Technology":              "XLK",
    "Information Technology":  "XLK",   # alias, algunas fuentes usan este nombre
    "Consumer Staples":        "XLP",
    "Real Estate":             "XLRE",
    "Utilities":               "XLU",
    "Health Care":             "XLV",
    "Healthcare":              "XLV",   # alias
    "Consumer Discretionary":  "XLY",
}

# Subsector opcional para mineras de oro / plata — el caso NEM del documento.
# Se detecta por ticker porque yFinance/SP500 no siempre trae "industry" limpio.
GOLD_MINERS = {"NEM", "GOLD", "AEM", "KGC", "AU", "FNV", "WPM", "PAAS", "HL"}
SILVER_MINERS = {"PAAS", "HL", "CDE", "AG", "FSM"}

# sector -> lista de 3-4 tickers "pares" representativos del propio S&P500
# (para poder comparar rendimiento relativo sin llamar a ninguna API extra)
SECTOR_PEERS_MAP = {
    "Materials":              ["LIN", "APD", "ECL", "NEM"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS"],
    "Energy":                 ["XOM", "CVX", "COP", "SLB"],
    "Financials":             ["JPM", "BAC", "WFC", "GS"],
    "Industrials":             ["CAT", "UNP", "HON", "RTX"],
    "Technology":              ["AAPL", "MSFT", "NVDA", "AVGO"],
    "Information Technology":  ["AAPL", "MSFT", "NVDA", "AVGO"],
    "Consumer Staples":        ["PG", "KO", "PEP", "WMT"],
    "Real Estate":             ["PLD", "AMT", "EQIX", "SPG"],
    "Utilities":               ["NEE", "DUK", "SO", "D"],
    "Health Care":             ["UNH", "JNJ", "LLY", "ABBV"],
    "Healthcare":              ["UNH", "JNJ", "LLY", "ABBV"],
    "Consumer Discretionary":  ["AMZN", "TSLA", "HD", "MCD"],
}

# Variables macro criticas por sector — para que el analista de noticias sepa
# QUÉ buscar ademas de las noticias de la propia empresa (ej: oro para mineras)
SECTOR_MACRO_VARS = {
    "Materials":              ["commodity_prices", "dxy", "china_demand", "energy_costs"],
    "Energy":                 ["oil_price", "gas_price", "opec_supply", "inventories"],
    "Financials":             ["interest_rates", "yield_curve", "credit_spreads", "loan_growth"],
    "Real Estate":            ["interest_rates", "cap_rates", "occupancy_trends"],
    "Utilities":              ["interest_rates", "regulatory_environment"],
    "Technology":             ["interest_rates", "ai_capex_cycle", "semiconductor_cycle"],
    "Information Technology": ["interest_rates", "ai_capex_cycle", "semiconductor_cycle"],
    "Consumer Discretionary": ["consumer_spending", "employment_data", "interest_rates"],
    "Consumer Staples":       ["inflation", "input_costs", "fx_rates"],
    "Health Care":            ["fda_calendar", "drug_pricing_policy", "patent_cliffs"],
    "Healthcare":             ["fda_calendar", "drug_pricing_policy", "patent_cliffs"],
    "Communication Services": ["ad_spending_cycle", "streaming_competition", "regulation"],
    "Industrials":            ["pmi_manufacturing", "supply_chain", "capex_cycle"],
}


def get_sector_context(ticker: str, sector: str) -> dict:
    """
    Devuelve el contexto sectorial para un ticker: ETF representativo,
    peers (excluyendo el propio ticker), variables macro clave y si es
    una minera de oro/plata (caso especial mencionado en el documento).
    """
    sector = sector or "Unknown"
    etf   = SECTOR_ETF_MAP.get(sector, "SPY")
    peers = [p for p in SECTOR_PEERS_MAP.get(sector, []) if p != ticker][:3]
    macro = SECTOR_MACRO_VARS.get(sector, ["interest_rates", "market_regime"])

    subsector = None
    if ticker in GOLD_MINERS:
        subsector = "Gold Miner"
        if "gold_price" not in macro:
            macro = ["gold_price", "real_yields"] + macro
    elif ticker in SILVER_MINERS:
        subsector = "Silver Miner"
        if "silver_price" not in macro:
            macro = ["silver_price", "real_yields"] + macro

    return {
        "sector_etf":              etf,
        "subsector":               subsector,
        "peer_group":              peers,
        "critical_macro_variables": macro,
    }
