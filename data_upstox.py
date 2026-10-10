from __future__ import annotations
"""
data_upstox.py - Upstox access: analytics token, instrument map, daily candles.

* Token: Upstox ANALYTICS token (read-only market data, valid ~1 year), kept in
  analytics_token.txt or env UPSTOX_ANALYTICS_TOKEN. No daily OAuth login.
* Candles: Upstox v3 daily candles, cached per instrument per day in ./cache.
* Fix #12: a missing instrument raises DataError - never a silent fallback
  to NIFTY 50.
"""
import gzip
import json
import os
import time
from datetime import date, timedelta
from urllib.parse import quote

import pandas as pd
import requests

import config as C

V3_URL = "https://api.upstox.com/v3/historical-candle/{ik}/days/1/{to}/{frm}"
INSTR_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


class DataError(Exception):
    pass


# ------------------------------------------------------------------ token
def load_token() -> str | None:
    """Analytics token from env var or analytics_token.txt (first non-empty line)."""
    t = os.environ.get(C.ANALYTICS_TOKEN_ENV)
    if t and t.strip():
        return t.strip()
    p = C.ANALYTICS_TOKEN_FILE
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return None


def save_token(tok: str):
    with open(C.ANALYTICS_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok.strip() + "\n")


def check_token(tok: str) -> tuple[bool, str]:
    """Quick test call (NIFTY 50, last 10 days). Returns (ok, message)."""
    to = date.today().isoformat()
    frm = (date.today() - timedelta(days=10)).isoformat()
    url = V3_URL.format(ik=quote("NSE_INDEX|Nifty 50", safe=""), to=to, frm=frm)
    try:
        r = requests.get(url, headers={"Accept": "application/json", "Authorization": f"Bearer {tok}"}, timeout=20)
    except Exception as e:
        return False, f"Network error: {e}"
    if r.status_code == 200:
        return True, "Analytics token OK"
    if r.status_code in (401, 403):
        return False, "Token rejected by Upstox (expired or not an analytics token)"
    return False, f"Upstox returned HTTP {r.status_code}"


# ------------------------------------------------------------------ instruments
def instrument_map() -> dict[str, str]:
    """NSE equity trading_symbol -> instrument_key (cached for a day)."""
    p = os.path.join(C.CACHE_DIR, "NSE.json.gz")
    if not (os.path.exists(p) and time.time() - os.path.getmtime(p) < 86400):
        r = requests.get(INSTR_URL, timeout=120, headers=UA)
        r.raise_for_status()
        open(p, "wb").write(r.content)
    rows = json.loads(gzip.decompress(open(p, "rb").read()))
    return {x["trading_symbol"]: x["instrument_key"] for x in rows
            if x.get("segment") == "NSE_EQ" and x.get("instrument_type") in (None, "EQ")}


def index_keys() -> set[str]:
    """All NSE_INDEX instrument keys in the Upstox master (uses the same daily cache)."""
    instrument_map()                                   # makes sure the cache file exists
    p = os.path.join(C.CACHE_DIR, "NSE.json.gz")
    rows = json.loads(gzip.decompress(open(p, "rb").read()))
    return {x["instrument_key"] for x in rows if x.get("segment") == "NSE_INDEX"}


# ------------------------------------------------------------------ candles
def _cache_path(ik: str) -> str:
    safe = ik.replace("|", "_").replace(" ", "_").replace("&", "and").replace("/", "_")
    return os.path.join(C.CACHE_DIR, f"{safe}.pkl")


def daily_candles(ik: str, token: str, days: int = C.HISTORY_DAYS, retries: int = 3) -> pd.DataFrame:
    """Daily OHLCV from Upstox v3, cached for the calendar day."""
    cp = _cache_path(ik).replace(".pkl", f"_{days}d.pkl")
    if os.path.exists(cp) and date.fromtimestamp(os.path.getmtime(cp)) == date.today():
        return pd.read_pickle(cp)
    to = date.today().isoformat()
    frm = (date.today() - timedelta(days=days)).isoformat()
    url = V3_URL.format(ik=quote(ik, safe=""), to=to, frm=frm)
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    last_err = None
    for a in range(retries):
        try:
            r = requests.get(url, headers=h, timeout=30)
            if r.status_code in (401, 403):
                raise DataError("Upstox analytics token rejected (expired after ~1 year?) - generate a new one and use Set analytics token")
            if r.status_code == 429:
                time.sleep(1.5 * (a + 1))
                continue
            r.raise_for_status()
            c = r.json().get("data", {}).get("candles", [])
            if not c:
                raise DataError(f"No candles for {ik}")
            df = pd.DataFrame(c, columns=["ts", "open", "high", "low", "close", "volume", "oi"])
            df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None).dt.normalize()
            df = df.set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)
            df = df[~df.index.duplicated(keep="last")]
            df.to_pickle(cp)
            return df
        except DataError:
            raise
        except Exception as e:
            last_err = e
            time.sleep(1.0 * (a + 1))
    raise DataError(f"Fetch failed for {ik}: {last_err}")


def long_daily(ik: str, token: str, start: str = "2008-01-01", chunk_days: int = 1800) -> pd.DataFrame:
    """Long daily history (for the weekly-vs-monthly test), fetched in ~5-year chunks, cached per day."""
    cp = _cache_path(ik).replace(".pkl", f"_hist{start[:4]}.pkl")
    if os.path.exists(cp) and date.fromtimestamp(os.path.getmtime(cp)) == date.today():
        return pd.read_pickle(cp)
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    parts, end = [], date.today()
    first = date.fromisoformat(start)
    while end > first:
        frm = max(first, end - timedelta(days=chunk_days))
        url = V3_URL.format(ik=quote(ik, safe=""), to=end.isoformat(), frm=frm.isoformat())
        for a in range(3):
            r = requests.get(url, headers=h, timeout=60)
            if r.status_code == 429:
                time.sleep(1.5 * (a + 1))
                continue
            break
        if r.status_code in (401, 403):
            raise DataError("Upstox analytics token rejected - set a new one")
        if r.status_code != 200:
            raise DataError(f"History fetch failed for {ik}: HTTP {r.status_code}")
        c = r.json().get("data", {}).get("candles", [])
        if not c:
            break                                       # no older data for this instrument
        parts.append(pd.DataFrame(c, columns=["ts", "open", "high", "low", "close", "volume", "oi"]))
        end = frm - timedelta(days=1)
        time.sleep(0.1)
    if not parts:
        raise DataError(f"No history for {ik}")
    df = pd.concat(parts)
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None).dt.normalize()
    df = df.set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated(keep="last")]
    df.to_pickle(cp)
    return df


# ------------------------------------------------------------------ split / bonus repair
_SPLIT_RATIOS = [1 / 2, 1 / 3, 2 / 3, 1 / 4, 1 / 5, 2 / 5, 1 / 10, 1 / 20, 1 / 1.5]


def adjust_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Back-adjust one-day drops that match a split/bonus ratio within 5%.

    Upstox candles may not be adjusted for corporate actions; an unadjusted
    split looks like a 50-90% crash and breaks M1, M2, M3 and the RRG.
    Returns the adjusted frame and notes on what was changed (shown in the app).
    """
    if df is None or len(df) < 2:
        return df, []
    d = df.copy()
    notes = []
    ratio = d["close"] / d["close"].shift(1)
    for ts, x in ratio[ratio < 0.70].items():
        best = min(_SPLIT_RATIOS, key=lambda s: abs(x / s - 1))
        if abs(x / best - 1) < 0.05:
            mask = d.index < ts
            for col in ("open", "high", "low", "close"):
                d.loc[mask, col] = d.loc[mask, col] * best
            if "volume" in d:
                d.loc[mask, "volume"] = d.loc[mask, "volume"] / best
            notes.append(f"{ts.date()}: x{best:.3f} split/bonus adjusted")
    return d, notes
