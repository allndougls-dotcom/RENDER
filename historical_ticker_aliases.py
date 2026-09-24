"""Conservative ticker continuity map for historical S&P 500 backtests.

Only same-company name/ticker changes are mapped here. Mergers/acquisitions into
another issuer are deliberately NOT mapped to the acquirer (e.g. ATVI->MSFT,
XLNX->AMD) because that would splice different securities and introduce bias.

Each mapping below is supported by issuer/SEC documentation. The canonical
symbol is the later ticker for the SAME listed company/security lineage.
"""

# Historical symbol -> later symbol for same company/security lineage.
SAME_COMPANY_ALIASES = {
    # Meta Platforms: FB -> META, effective 2022-06-09.
    "FB": "META",
    # Anthem -> Elevance Health, ANTM -> ELV, effective 2022-06-28.
    "ANTM": "ELV",
    # Ball Corporation: BLL -> BALL, effective 2022-05-10.
    "BLL": "BALL",
    # CenturyLink -> Lumen Technologies: CTL -> LUMN, effective 2020-09-18.
    "CTL": "LUMN",
    # PerkinElmer -> Revvity: PKI -> RVTY, effective 2023-05-16.
    "PKI": "RVTY",
    # Willis Towers Watson: WLTW -> WTW, effective 2022-01-10.
    "WLTW": "WTW",
    # AmerisourceBergen -> Cencora: ABC -> COR, effective 2023-08-30.
    "ABC": "COR",
    # Everest Re Group -> Everest Group: RE -> EG, effective 2023-07-10.
    "RE": "EG",
    # FLEETCOR -> Corpay: FLT -> CPAY, effective 2024-03-25.
    "FLT": "CPAY",
    # Ceridian -> Dayforce: CDAY -> DAY, effective 2024-02-01.
    "CDAY": "DAY",
    # Fortune Brands Home & Security -> Fortune Brands Innovations: FBHS -> FBIN,
    # effective 2022-12-15.
    "FBHS": "FBIN",
    # ViacomCBS -> Paramount Global class B: VIAC -> PARA, effective 2022-02-17.
    "VIAC": "PARA",
    # NortonLifeLock -> Gen Digital: NLOK -> GEN, effective 2022-11-08.
    # The ticker/name change followed the Avast combination, but this is the
    # continuing listed NortonLifeLock registrant rather than mapping to an acquirer.
    "NLOK": "GEN",
}

# Known annotation emitted by the third-party membership dataset.
ANNOTATION_ALIASES = {
    "RVTY (PREVIOUSLY PKI)": "RVTY",
}


def clean_symbol(raw: str) -> str:
    """Normalize a membership symbol without guessing corporate actions."""
    s = str(raw).strip().upper().replace(".", "-")
    s = ANNOTATION_ALIASES.get(s, s)
    return SAME_COMPANY_ALIASES.get(s, s)


def alias_reason(old: str) -> str:
    return "same_company_ticker_change" if old in SAME_COMPANY_ALIASES else "identity"
