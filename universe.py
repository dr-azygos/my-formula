from __future__ import annotations
"""
universe.py - maps every NIFTY 500 stock to one Step 1 group and one valuation model.

Fix #4 / #5: one table links Step 1 index -> peer stocks -> valuation model.
The auto map is written to inputs/universe_map.csv on every run so you can
see it. To correct a stock, add a row to inputs/universe_overrides.csv:
    symbol,group,model
    BAJFINANCE,Financials,nbfc
Valid models: bank, psu_bank, nbfc, other_fin, pharma, hospital, oil_gas, power,
it, auto, fmcg, metal, realty, media, cons_dur, none
"""
import os
import time

import pandas as pd
import requests

import config as C

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# NSE "Industry" column (macro sector) -> (group, default model)
INDUSTRY_MAP = [
    ("financial services", "Financials", "nbfc"),
    ("information technology", "IT", "it"),
    ("healthcare", "Health", "pharma"),
    ("automobile", "Auto", "auto"),
    ("fast moving consumer goods", "FMCG", "fmcg"),
    ("metals", "Metal", "metal"),
    ("realty", "Realty", "realty"),
    ("media", "Media", "media"),
    ("consumer durables", "Consumer Durables", "cons_dur"),
    ("oil gas", "Energy", "oil_gas"),
    ("power", "Energy", "power"),
]

OTHER_FIN_WORDS = ["insurance", "life", "general insur", "asset management", "amc", "exchange",
                   "depository", "computer age", "kfin", "securities", "broking", "angel one",
                   "motilal", "capital market", "wealth", "360 one", "nuvama", "iifl capital",
                   "prudent", "anand rathi"]
HOSPITAL_WORDS = ["hospital", "health", "diagnost", "pathlab", "medi", "fortis", "narayana", "max ",
                  "rainbow", "aster", "metropolis", "krsnaa", "vijaya", "global health", "apollo"]

# which models each Step 1 index draws its peers from
INDEX_MODELS = {
    "Nifty Bank": {"bank", "psu_bank"},
    "Nifty Pvt Bank": {"bank"},
    "Nifty Psu Bank": {"psu_bank"},
    "Nifty Fin Service": {"bank", "psu_bank", "nbfc", "other_fin"},
    "Nifty Pharma": {"pharma"},
    "Nifty Healthcare": {"pharma", "hospital"},
    "Nifty Energy": {"oil_gas", "power"},
    "Nifty Oil Gas": {"oil_gas"},
    "Nifty It": {"it"},
    "Nifty Auto": {"auto"},
    "Nifty Fmcg": {"fmcg"},
    "Nifty Metal": {"metal"},
    "Nifty Realty": {"realty"},
    "Nifty Media": {"media"},
    "Nifty Consumer Durables": {"cons_dur"},
}

FINANCIAL_MODELS = {"bank", "psu_bank", "nbfc"}


def _get_csv(url: str, cache_name: str) -> pd.DataFrame:
    p = os.path.join(C.CACHE_DIR, cache_name)
    if not (os.path.exists(p) and time.time() - os.path.getmtime(p) < 7 * 86400):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            open(p, "w", encoding="utf-8").write(r.text)
        except Exception:
            if not os.path.exists(p):
                seed = os.path.join(C.INPUT_DIR, "seed", cache_name)   # copy shipped with the cloud app
                if os.path.exists(seed):
                    return pd.read_csv(seed)
                raise
    return pd.read_csv(p)


def _symbols(df: pd.DataFrame) -> set[str]:
    col = next(c for c in df.columns if c.strip().lower() == "symbol")
    return set(df[col].astype(str).str.strip().str.upper())


def build_universe() -> pd.DataFrame:
    """Return DataFrame[symbol, company, industry, group, model]."""
    n500 = _get_csv(C.INDEX_URLS["NIFTY 500"], "nifty500list.csv")
    n500.columns = [c.strip() for c in n500.columns]
    banks = _symbols(_get_csv(C.INDEX_URLS["Nifty Bank"], "niftybank.csv")) | \
        _symbols(_get_csv(C.INDEX_URLS["Nifty Pvt Bank"], "niftypvtbank.csv"))
    psu = _symbols(_get_csv(C.INDEX_URLS["Nifty Psu Bank"], "niftypsubank.csv"))
    pharma_idx = _symbols(_get_csv(C.INDEX_URLS["Nifty Pharma"], "niftypharma.csv"))

    rows = []
    for _, r in n500.iterrows():
        sym = str(r["Symbol"]).strip().upper()
        name = str(r.get("Company Name", ""))
        ind = str(r.get("Industry", ""))
        low_n, low_i = name.lower(), ind.lower()
        group, model = "Other", "none"
        for key, g, m in INDUSTRY_MAP:
            if key in low_i:
                group, model = g, m
                break
        if group == "Financials":
            if sym in psu:
                model = "psu_bank"
            elif sym in banks or " bank" in f" {low_n}":
                model = "bank"
            elif any(w in low_n for w in OTHER_FIN_WORDS):
                model = "other_fin"
        if group == "Health" and sym not in pharma_idx and any(w in low_n for w in HOSPITAL_WORDS):
            model = "hospital"
        rows.append({"symbol": sym, "company": name, "industry": ind, "group": group, "model": model})
    u = pd.DataFrame(rows)

    ov = os.path.join(C.INPUT_DIR, "universe_overrides.csv")
    if os.path.exists(ov):
        o = pd.read_csv(ov)
        o["symbol"] = o["symbol"].astype(str).str.strip().str.upper()
        for _, r in o.iterrows():
            m = u["symbol"] == r["symbol"]
            if m.any():
                if isinstance(r.get("group"), str):
                    u.loc[m, "group"] = r["group"]
                if isinstance(r.get("model"), str):
                    u.loc[m, "model"] = r["model"]
    u.to_csv(os.path.join(C.INPUT_DIR, "universe_map.csv"), index=False)
    return u


def peers_for_index(u: pd.DataFrame, index_name: str) -> pd.DataFrame:
    models = INDEX_MODELS.get(index_name, set())
    return u[u["model"].isin(models)].copy()
