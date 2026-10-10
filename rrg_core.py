from __future__ import annotations
"""
rrg_core.py - your RRG maths, unchanged, plus the v3.1 safeguards.

calculate_rrg_stable_math() is copied verbatim from RRG_SOFTWARE_V6.py so the
numbers match what you see in the RRG Quantum Scanner. Everything else here
wraps it: completed-bar trimming, warm-up masking, quadrant labels.
"""
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

import config as C


# ------------------------------------------------------------------ verbatim from RRG_SOFTWARE_V6.py
def calculate_rrg_stable_math(df_s, df_b):
    try:
        df_s, df_b = df_s.copy(), df_b.copy()
        df_s['close'] = pd.to_numeric(df_s['close'], errors='coerce')
        df_b['close'] = pd.to_numeric(df_b['close'], errors='coerce')
        combined = pd.merge(df_s[['close']], df_b[['close']], left_index=True, right_index=True, how='inner',
                            suffixes=('_s', '_b')).dropna()
        if len(combined) < 30: return None

        rs = (combined['close_s'] / combined['close_b']) * 100
        rs_ratio = (rs.rolling(10).mean() / rs.rolling(30).mean()) * 100
        rs_mom = 100 + (rs_ratio - rs_ratio.ewm(span=2).mean()) * 10

        clean_r = rs_ratio.fillna(100).replace([np.inf, -np.inf], 100)
        clean_m = rs_mom.fillna(100).replace([np.inf, -np.inf], 100)
        r, m = clean_r.iloc[-1], clean_m.iloc[-1]

        return {"ratio_series": clean_r.tolist(), "mom_series": clean_m.tolist(), "dates": combined.index,
                "last_ratio": r, "last_mom": m}
    except Exception:
        return None
# ------------------------------------------------------------------ end verbatim


def quadrant(r, m):
    """Same thresholds as plot_results() in your app."""
    if r > 100 and m > 100:
        return "Leading"
    if r <= 100 and m <= 100:
        return "Lagging"
    if r > 100 and m <= 100:
        return "Weakening"
    return "Improving"


def to_weekly(daily: pd.DataFrame, now: datetime | None = None, live: bool = False) -> pd.DataFrame:
    """Daily OHLCV -> weekly bars labelled by the week's last trading day.

    Fix #10: drops the current week if it is not complete yet (before Friday 15:30 IST),
    so signals never come from a part-week candle. live=True keeps it (same as RRG v6).
    """
    if daily is None or daily.empty:
        return pd.DataFrame()
    d = daily.copy()
    d["_wk"] = d.index.to_period("W-FRI")
    agg = {"close": "last"}
    if "volume" in d:
        agg["volume"] = "sum"
    w = d.groupby("_wk").agg(agg)
    w.index = pd.DatetimeIndex(d.index.to_series().groupby(d["_wk"]).max().values)
    w.index.name = None
    now = now or datetime.now()
    last_day = w.index[-1]
    same_week = last_day.to_period("W-FRI") == pd.Timestamp(now.date()).to_period("W-FRI")
    week_done = now.weekday() > 4 or (now.weekday() == 4 and now.time() >= dtime(15, 30))
    if same_week and not week_done and not live:
        w = w.iloc[:-1]
    return w


def to_monthly(daily: pd.DataFrame, now: datetime | None = None, live: bool = False) -> pd.DataFrame:
    """Daily -> monthly bars labelled by the month's last trading day.

    The current month is dropped until it is over (same completed-bar rule as weekly).
    live=True keeps the unfinished month (what RRG v6 shows); it changes daily until month-end.
    """
    if daily is None or daily.empty:
        return pd.DataFrame()
    d = daily.copy()
    d["_m"] = d.index.to_period("M")
    agg = {"close": "last"}
    if "volume" in d:
        agg["volume"] = "sum"
    m = d.groupby("_m").agg(agg)
    m.index = pd.DatetimeIndex(d.index.to_series().groupby(d["_m"]).max().values)
    m.index.name = None
    now = now or datetime.now()
    if not live and m.index[-1].to_period("M") >= pd.Timestamp(now.date()).to_period("M"):
        m = m.iloc[:-1]
    return m


def to_bars(daily: pd.DataFrame, tf: str = "week", now: datetime | None = None, live: bool = False) -> pd.DataFrame:
    return to_monthly(daily, now, live) if tf == "month" else to_weekly(daily, now, live)


def rrg(series_df: pd.DataFrame, bench_df: pd.DataFrame) -> dict | None:
    """Run your RRG and return validated series.

    Fix #11: values from the first RRG_MIN_VALID_BARS bars (filled with 100 by
    fillna) are set to NaN so they can never be read as a signal.
    """
    d = calculate_rrg_stable_math(series_df, bench_df)
    if d is None:
        return None
    idx = pd.DatetimeIndex(d["dates"])
    r = pd.Series(d["ratio_series"], index=idx, dtype=float)
    m = pd.Series(d["mom_series"], index=idx, dtype=float)
    r.iloc[: C.RRG_MIN_VALID_BARS - 1] = np.nan
    m.iloc[: C.RRG_MIN_VALID_BARS - 1] = np.nan
    if r.dropna().empty:
        return None
    last_r, last_m = r.iloc[-1], m.iloc[-1]
    return {
        "ratio": r, "mom": m,
        "last_ratio": float(last_r), "last_mom": float(last_m),
        "quadrant": quadrant(last_r, last_m),
        "valid_bars": int(r.notna().sum()),
    }


def rising(s: pd.Series, n: int = 2) -> bool:
    """True when the last n steps all rose: s[t] > s[t-1] > ... > s[t-n]."""
    s = s.dropna()
    if len(s) < n + 1:
        return False
    tail = s.iloc[-(n + 1):].values
    return bool(np.all(np.diff(tail) > 0))


def falling(s: pd.Series, n: int = 3) -> bool:
    s = s.dropna()
    if len(s) < n + 1:
        return False
    tail = s.iloc[-(n + 1):].values
    return bool(np.all(np.diff(tail) < 0))
