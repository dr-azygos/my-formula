"""
config.py - frozen parameters for "My Formula" (System v3.1).

Every number here comes from the v3.1 doc's "Frozen parameters" table.
Do NOT tune these after looking at results.
"""
import os

# ---------------------------------------------------------------- paths
HERE = os.path.dirname(os.path.abspath(__file__))          # ...\Cash\My formula
CASH = os.path.dirname(HERE)                                 # ...\Cash
CACHE_DIR = os.path.join(HERE, "cache")
OUTPUT_DIR = os.path.join(HERE, "output")
INPUT_DIR = os.path.join(HERE, "inputs")
SCREENER_XLSX_DIR = os.path.join(CASH, "screener_xlsx")      # from screener_downloader.py
# cloud copy (streamlit_app.py): screener files sit inside the app folder, or env MF_SCREENER_DIR
if os.environ.get("MF_SCREENER_DIR"):
    SCREENER_XLSX_DIR = os.environ["MF_SCREENER_DIR"]
elif os.path.isdir(os.path.join(HERE, "screener_xlsx")):
    SCREENER_XLSX_DIR = os.path.join(HERE, "screener_xlsx")
for _d in (CACHE_DIR, OUTPUT_DIR, INPUT_DIR):
    os.makedirs(_d, exist_ok=True)

# Upstox ANALYTICS token (read-only, valid ~1 year; no daily login).
# Generate it at https://account.upstox.com/developer/apps -> Analytics Token.
# Saved by the app's "Set analytics token" button, or paste it into this file yourself.
ANALYTICS_TOKEN_FILE = os.path.join(HERE, "analytics_token.txt")
ANALYTICS_TOKEN_ENV = "UPSTOX_ANALYTICS_TOKEN"

# ---------------------------------------------------------------- Step 1: RRG
RRG_SHORT, RRG_LONG, RRG_EMA_SPAN, RRG_SCALE = 10, 30, 2, 10   # same as RRG_SOFTWARE_V6
RRG_MIN_VALID_BARS = 30            # first 29 bars are fillna(100) in your code -> invalid
TREND_SMA_WEEKS = 40
TREND_SMA = {"week": 40, "month": 10}   # 40 weeks ~ 10 months: same trend filter on either timeframe
RRG_TF = "month"                   # timeframe that drives the screen (your choice: monthly, not weekly)
LIVE_TF_COMPARISON = False
# Live RRG: also show the value including the unfinished current month (what RRG v6 shows).
# Display only - buy/sell decisions always use completed months, because the live value
# keeps changing ("repaints") until the month closes.
SHOW_LIVE_RRG = True         # True = also run the weekly screen each time, for the side-by-side tab
BREADTH_MIN = 0.50
MAX_SECTORS = 4
HISTORY_DAYS = 2600                # ~7 years: monthly RRG needs 30 months warm-up + a usable trail

BENCH_NAME = "Nifty 50"
BENCH_KEY = "NSE_INDEX|Nifty 50"

# your 18 sector indices. Keys must match Upstox's instrument list exactly; three differ from
# RRG_SOFTWARE_V6.py (Oil & Gas, Consumer Durables, Healthcare), which Upstox rejects with HTTP 400.
SECTORS_MAPPING = {
    "Nifty Bank": "NSE_INDEX|Nifty Bank",
    "Nifty It": "NSE_INDEX|Nifty IT",
    "Nifty Auto": "NSE_INDEX|Nifty Auto",
    "Nifty Fmcg": "NSE_INDEX|Nifty FMCG",
    "Nifty Metal": "NSE_INDEX|Nifty Metal",
    "Nifty Pharma": "NSE_INDEX|Nifty Pharma",
    "Nifty Realty": "NSE_INDEX|Nifty Realty",
    "Nifty Psu Bank": "NSE_INDEX|Nifty PSU Bank",
    "Nifty Pvt Bank": "NSE_INDEX|Nifty Pvt Bank",
    "Nifty Fin Service": "NSE_INDEX|Nifty Fin Service",
    "Nifty Media": "NSE_INDEX|Nifty Media",
    "Nifty Energy": "NSE_INDEX|Nifty Energy",
    "Nifty Oil Gas": "NSE_INDEX|NIFTY OIL AND GAS",          # Upstox spelling
    "Nifty Consumer Durables": "NSE_INDEX|NIFTY CONSR DURBL",  # Upstox spelling
    "Nifty Healthcare": "NSE_INDEX|NIFTY HEALTHCARE",         # Upstox spelling
    "Nifty Commodities": "NSE_INDEX|Nifty Commodities",
    "Nifty Infra": "NSE_INDEX|Nifty Infra",
    "Nifty Cpse": "NSE_INDEX|Nifty CPSE",
}

# one index per group (doc Step 1 table)
GROUPS = {
    "Financials": ["Nifty Bank", "Nifty Pvt Bank", "Nifty Psu Bank", "Nifty Fin Service"],
    "Health": ["Nifty Pharma", "Nifty Healthcare"],
    "Energy": ["Nifty Energy", "Nifty Oil Gas"],
    "IT": ["Nifty It"],
    "Auto": ["Nifty Auto"],
    "FMCG": ["Nifty Fmcg"],
    "Metal": ["Nifty Metal"],
    "Realty": ["Nifty Realty"],
    "Media": ["Nifty Media"],
    "Consumer Durables": ["Nifty Consumer Durables"],
}
CONTEXT_ONLY = ["Nifty Commodities", "Nifty Infra", "Nifty Cpse"]

# constituent lists (same URLs as your RRG app) - used to classify banks / PSU banks / pharma
INDEX_URLS = {
    "NIFTY 500": "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "Nifty Bank": "https://niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
    "Nifty Psu Bank": "https://niftyindices.com/IndexConstituent/ind_niftypsubanklist.csv",
    "Nifty Pvt Bank": "https://niftyindices.com/IndexConstituent/ind_nifty_privatebanklist.csv",
    "Nifty Pharma": "https://niftyindices.com/IndexConstituent/ind_niftypharmalist.csv",
}

# ---------------------------------------------------------------- Step 2: peers and momentum
LIQ_MIN_CR = 5.0                   # 6-month median daily traded value, Rs crore
LIQ_LOOKBACK_DAYS = 126
LIQ_BOTTOM_CUT = 0.20
MIN_PEERS_FOR_CUT = 10
SMALL_SECTOR_PEERS = 5             # below this: max 2 stocks
M1_REG_MONTHS, M1_MIN_MONTHS = 36, 24
M2_DAYS = 252
M3_START, M3_SKIP = 252, 21
MOM_GATE = 50.0

# ---------------------------------------------------------------- Step 2: quality and valuation
QUAL_TOP_FRACTION = 0.70           # keep top 70% -> percentile >= 30
FSCORE_MIN = 5
PLEDGE_MAX = 20.0
Q3_CAP, Q4_CAP = 2.0, 1.0
V1_MIN_PEERS = 10
WINSOR = (0.05, 0.95)
FUND_STALE_DAYS = 150              # warn if a screener export is older than this

# ---------------------------------------------------------------- Step 2d / 3
W_NORMAL = {"QUAL": 0.40, "MOM": 0.40, "VAL": 0.20}
W_BEAR = {"QUAL": 0.50, "MOM": 0.25, "VAL": 0.25}
N_SLOTS = 12
MAX_PER_SECTOR = 3
MAX_PER_SMALL_SECTOR = 2
STOCK_TRIM = 0.12
SELL_BUFFER_TOP = 5
VOL_LOOKBACK_DAYS = 126
# Set this from the build-period backtest (expanding-window rule), then freeze it.
# While it is None the app shows realised vol but keeps exposure at 100%.
SIGMA_TARGET = None
REGIME_CONFIRM_MONTHS = 2
# v3.2: the bear regime only re-weights StockScore (W_BEAR). New buys continue and spare slots stay
# in the NIFTY 50 ETF. (v3.1 stopped buying and parked cash in a liquid fund; the backtest ablation
# showed that cost about 7%/yr vs NIFTY 50, t = 2.6.) Set to True to restore the v3.1 behaviour.
BEAR_BLOCKS_NEW_BUYS = False
REGIME_RET_MONTHS = 24

# ---------------------------------------------------------------- RRG weekly vs monthly test
CMP_HISTORY_START = "2008-01-01"   # daily index history downloaded for the historical test
CMP_SECTOR_WEIGHT = 0.25           # each selected sector = 3 slots x 8.33%; rest in NIFTY 50
CMP_COST_PER_SIDE = 0.0025         # 0.5% round trip, as in the validation protocol
