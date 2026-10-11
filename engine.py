from __future__ import annotations
"""
engine.py - the v3.1 screen: Step 1 (sectors), Step 2 (stocks), Step 3 (risk).

Used by app.py (GUI) and by `python app.py --cli`. No tkinter here.
"""
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np
import pandas as pd

import config as C
import data_upstox as D
import fundamentals as F
import rrg_core as R
import universe as U

_PV = tuple(int(x) for x in pd.__version__.split(".")[:2])
MONTH_END = "ME" if _PV >= (2, 2) else "M"          # pandas < 2.2 only knows "M"

QUAL_FAMILY = {"bank": "fin", "psu_bank": "fin", "nbfc": "fin", "other_fin": "other_fin"}

# valuation model: (multiple, driver, yield)  - doc 2c sector map
VAL_MODELS = {
    "bank": ("P/B", "roe3", "E/P"), "psu_bank": ("P/B", "roe3", "E/P"), "nbfc": ("P/B", "roe3", "E/P"),
    "other_fin": (None, None, "E/P"),
    "pharma": ("EV/Sales", "ebitda_margin", "E/P"), "hospital": ("EV/Sales", "ebitda_margin", "E/P"),
    "oil_gas": ("EV/EBITDA5y", "roce5", "FCF"), "power": ("P/B", "roe3", "E/P"),
    "it": ("EV/Sales", "ebit_margin", "FCF"), "auto": ("EV/EBITDA", "roce", "E/P"),
    "fmcg": ("EV/Sales", "ebit_margin", "FCF"), "metal": ("EV/EBITDA5y", "roce5", "B/P"),
    "realty": ("P/B", "roe3", "B/P"), "media": ("EV/EBITDA", "ebitda_margin", "E/P"),
    "cons_dur": ("EV/EBITDA", "roce", "E/P"),
}


# ------------------------------------------------------------------ small helpers
def pct(s: pd.Series) -> pd.Series:
    """Percentile rank 0-100 among non-missing values. -inf ranks last, +inf ranks best."""
    s = pd.to_numeric(s, errors="coerce")
    return s.rank(pct=True, method="average") * 100


def theil_sen(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    slopes = []
    n = len(x)
    for i in range(n):
        dx = x[i + 1:] - x[i]
        ok = dx != 0
        slopes.extend(((y[i + 1:] - y[i])[ok] / dx[ok]).tolist())
    b = float(np.median(slopes)) if slopes else 0.0
    a = float(np.median(y - b * x))
    return a, b


def _read_opt_csv(name: str, cols: list[str]) -> pd.DataFrame:
    p = os.path.join(C.INPUT_DIR, name)
    if not os.path.exists(p):
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(p)
    df.columns = [c.strip().lower() for c in df.columns]
    if "symbol" in df:
        df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    return df


def monthly_returns(daily: pd.DataFrame, now: datetime) -> pd.Series:
    m = daily["close"].resample(MONTH_END).last()
    m = m[m.index.to_period("M") < pd.Timestamp(now.date()).to_period("M")]   # completed months only
    return m.pct_change().dropna()


# ------------------------------------------------------------------ the screener
class Screener:
    def __init__(self, token: str, log=print, progress=None, now: datetime | None = None, fetch=None):
        self.token = token
        self.log = log
        self.progress = progress or (lambda done, total, label="": None)
        self.now = now or datetime.now()
        self.fetch = fetch or (lambda ik: D.daily_candles(ik, self.token))   # injectable for tests
        self.issues: list[dict] = []
        self.daily: dict[str, pd.DataFrame] = {}

    def issue(self, what, detail):
        self.issues.append({"item": what, "issue": detail})

    # -------------------------------------------------------------- data
    def get_daily(self, name: str, ik: str, adjust=True) -> pd.DataFrame | None:
        if name in self.daily:
            return self.daily[name]
        try:
            df = self.fetch(ik)
        except D.DataError as e:
            if "token" in str(e).lower():
                raise
            self.issue(name, str(e))
            self.daily[name] = None
            return None
        if adjust:
            df, notes = D.adjust_splits(df)
            for n in notes:
                self.issue(name, n)
        self.daily[name] = df
        return df

    def fetch_many(self, items: list[tuple[str, str]], label: str):
        todo = [(n, k) for n, k in items if n not in self.daily]
        done = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(self.get_daily, n, k): n for n, k in todo}
            for f in as_completed(futs):
                f.result()
                done += 1
                self.progress(done, len(todo), label)

    # -------------------------------------------------------------- Step 3b: regime
    def regime(self, bench_d: pd.DataFrame) -> dict:
        # monthly: NIFTY 50 month-end close vs its 10-month SMA (the monthly twin of the 40-week SMA)
        n_sma = C.TREND_SMA[C.RRG_TF]
        bars = R.to_bars(bench_d, C.RRG_TF, self.now)["close"]
        sma = bars.rolling(n_sma).mean()
        mclose = R.to_monthly(bench_d, self.now)["close"]
        rows = []
        for me, c in mclose.items():
            s = sma[sma.index <= me]
            if s.dropna().empty:
                continue
            ret24 = c / mclose.shift(C.REGIME_RET_MONTHS).get(me, np.nan) - 1
            rows.append({"month": me, "close": c, "sma": s.iloc[-1], "below": c < s.iloc[-1], "ret24": ret24})
        if not rows:
            return {"bear": False, "detail": "not enough NIFTY 50 history"}
        h = pd.DataFrame(rows).set_index("month")
        state = False
        k = C.REGIME_CONFIRM_MONTHS
        for i in range(len(h)):
            last_k = h["below"].iloc[max(0, i - k + 1): i + 1]
            r24 = h["ret24"].iloc[i]
            if len(last_k) == k and last_k.all() or (pd.notna(r24) and r24 < 0):
                state = True
            elif len(last_k) == k and (~last_k).all() and (pd.isna(r24) or r24 >= 0):
                state = False
        last = h.iloc[-1]
        return {"bear": bool(state),
                "detail": (f"NIFTY 50 {last['close']:.0f} vs {n_sma}-{'month' if C.RRG_TF == 'month' else 'week'} SMA {last['sma']:.0f} at "
                           f"{h.index[-1].date()}; 24-m return "
                           + (f"{last['ret24']*100:.1f}%" if pd.notna(last['ret24']) else "n/a")),
                "history": h}

    # -------------------------------------------------------------- Step 1
    def step1(self, bench_d: pd.DataFrame, uni: pd.DataFrame, imap: dict, tf: str = "week") -> pd.DataFrame:
        """Sector screen on monthly (config.RRG_TF, drives the portfolio) or weekly bars."""
        bench_w = R.to_bars(bench_d, tf, self.now)
        unit = "wks" if tf == "week" else "months"
        n_sma = C.TREND_SMA[tf]
        rows = []
        for name, ik in C.SECTORS_MAPPING.items():
            d = self.daily.get(name)
            if d is None:
                rows.append({"index": name, "status": "no data"})
                continue
            w = R.to_bars(d, tf, self.now)
            rr = R.rrg(w, bench_w)
            if rr is None or rr["valid_bars"] < 6:
                rows.append({"index": name, "status": "too little history"})
                continue
            sma = w["close"].rolling(n_sma).mean()
            above = w["close"] > sma
            group = next((g for g, mem in C.GROUPS.items() if name in mem), "Context")
            q = rr["quadrant"]
            rrg_ok = q == "Leading" or (q == "Improving" and R.rising(rr["mom"], 2))
            exit_flags = []
            if q == "Weakening" and R.falling(rr["ratio"], 3):
                exit_flags.append(f"Weakening + RS-Ratio down 3 {unit}")
            if len(above) >= 2 and not above.iloc[-1] and not above.iloc[-2]:
                exit_flags.append(f"2 closes below {n_sma}-{unit[:-1]} SMA")
            if q == "Lagging":
                exit_flags.append("Lagging (act next week)" if tf == "week" else "Lagging")
            live = {}
            if C.SHOW_LIVE_RRG:
                wl = R.to_bars(d, tf, self.now, live=True)
                rl = R.rrg(wl, R.to_bars(bench_d, tf, self.now, live=True))
                if rl is not None:
                    live = {"live_quadrant": rl["quadrant"], "live_rs_ratio": round(rl["last_ratio"], 2),
                            "live_rs_mom": round(rl["last_mom"], 2),
                            "live_bar_of": wl.index[-1].date().isoformat()
                            + ("" if wl.index[-1] == w.index[-1] else " (unfinished)")}
            rows.append({
                "index": name, "group": group, "quadrant": q,
                "rs_ratio": round(rr["last_ratio"], 2), "rs_mom": round(rr["last_mom"], 2),
                "mom_rising_2bars": R.rising(rr["mom"], 2),
                "above_trend": bool(above.iloc[-1]) if pd.notna(sma.iloc[-1]) else False,
                "rrg_ok": rrg_ok, "breadth": np.nan, "eligible": False, "selected": False,
                "exit_flags": "; ".join(exit_flags), "status": "",
                "bar_of": w.index[-1].date().isoformat(), **live,
            })
        S = pd.DataFrame(rows)

        # breadth only for sectors that pass RRG + trend and belong to a group
        for col, default in (("group", "Context"), ("rrg_ok", False), ("above_trend", False), ("rs_ratio", np.nan),
                             ("breadth", np.nan), ("eligible", False), ("selected", False), ("exit_flags", "")):
            if col not in S:
                S[col] = default
        S["eligible"] = S["eligible"].fillna(False).astype(bool)
        S["selected"] = S["selected"].fillna(False).astype(bool)
        cand = S[(S["group"] != "Context") & S["rrg_ok"].fillna(False).astype(bool)
                 & S["above_trend"].fillna(False).astype(bool)]
        need = set()
        for name in cand["index"]:
            need |= set(U.peers_for_index(uni, name)["symbol"])
        items = [(s, imap[s]) for s in sorted(need) if s in imap]
        for s in sorted(need - set(imap)):
            self.issue(s, "not in Upstox instrument list")
        if items:
            self.log(f"Breadth ({tf}ly): fetching {len(items)} stocks for {len(cand)} candidate sectors")
            self.fetch_many(items, "breadth")
        for i, r in cand.iterrows():
            peers = U.peers_for_index(uni, r["index"])["symbol"]
            ups, n = 0, 0
            for s in peers:
                d = self.daily.get(s)
                if d is None:
                    continue
                rr = R.rrg(R.to_bars(d, tf, self.now), bench_w)
                if rr is None or rr["valid_bars"] < 1:
                    continue
                n += 1
                ups += rr["last_ratio"] > 100
            S.at[i, "breadth"] = round(ups / n, 3) if n else np.nan
            S.at[i, "eligible"] = bool(n and ups / n >= C.BREADTH_MIN)

        # one per group, highest RS-Ratio; then top MAX_SECTORS by RS-Ratio
        el = S[S["eligible"] == True]
        best = el.sort_values("rs_ratio", ascending=False).groupby("group").head(1)
        chosen = best.sort_values("rs_ratio", ascending=False).head(C.MAX_SECTORS)["index"]
        S.loc[S["index"].isin(chosen), "selected"] = True
        S["status"] = np.where(S["selected"], "SELECTED",
                               np.where(S["eligible"], "eligible (not top of group / top 4)", S["status"]))
        return S.sort_values(["selected", "eligible", "rs_ratio"], ascending=False).reset_index(drop=True)

    # -------------------------------------------------------------- Step 2
    def step2_sector(self, index_name: str, uni: pd.DataFrame, imap: dict, weights: dict,
                     fin_m: pd.DataFrame, pledge: pd.DataFrame) -> pd.DataFrame:
        peers = U.peers_for_index(uni, index_name)
        items = [(s, imap[s]) for s in peers["symbol"] if s in imap]
        self.fetch_many(items, index_name)
        idx_w = R.to_bars(self.daily[index_name], C.RRG_TF, self.now)
        idx_live = R.to_bars(self.daily[index_name], C.RRG_TF, self.now, live=True)

        rows = []
        for _, p in peers.iterrows():
            s = p["symbol"]
            d = self.daily.get(s)
            if d is None or len(d) < 60:
                continue
            liq = float((d["close"] * d["volume"]).iloc[-C.LIQ_LOOKBACK_DAYS:].median() / 1e7)
            rr = R.rrg(R.to_bars(d, C.RRG_TF, self.now), idx_w)
            rl = R.rrg(R.to_bars(d, C.RRG_TF, self.now, live=True), idx_live) if C.SHOW_LIVE_RRG else None
            c = d["close"]
            m2 = float(c.iloc[-1] / c.iloc[-C.M2_DAYS:].max())
            m3 = np.nan
            if len(c) >= C.M3_START + 1:
                w = c.pct_change().iloc[-C.M3_START:-C.M3_SKIP].dropna()
                pret = float((1 + w).prod() - 1)
                m3 = (float((w > 0).mean() - (w < 0).mean())) if pret > 0 else F.NEG_INF
            rows.append({"symbol": s, "company": p["company"], "model": p["model"], "price": float(c.iloc[-1]),
                         "liq_cr": round(liq, 2), "rrg_vs_sector": rr["quadrant"] if rr else "n/a",
                         "rs_ratio_vs_sector": round(rr["last_ratio"], 2) if rr else np.nan,
                         "live_rrg_vs_sector": rl["quadrant"] if rl else "n/a",
                         "M2": m2, "M3": m3})
        T = pd.DataFrame(rows)
        if T.empty:
            return T

        # liquidity gate (doc 2a rules 2-3)
        T["liq_ok"] = T["liq_cr"] >= C.LIQ_MIN_CR
        floor_ok = T[T["liq_ok"]]
        cut = floor_ok["liq_cr"].quantile(C.LIQ_BOTTOM_CUT) if len(floor_ok) else np.nan
        if (floor_ok["liq_cr"] >= cut).sum() >= C.MIN_PEERS_FOR_CUT:
            T["liq_ok"] = T["liq_ok"] & (T["liq_cr"] >= cut)
        L = T[T["liq_ok"]].copy()
        n_peers = len(L)
        if L.empty:
            T["note"] = "no liquid peers"
            return T

        # M1 residual momentum vs leave-one-out equal-weight peers
        Rm = pd.DataFrame({s: monthly_returns(self.daily[s], self.now) for s in L["symbol"]})
        Rm = Rm.iloc[-C.M1_REG_MONTHS:]
        m1 = {}
        for s in L["symbol"]:
            others = Rm.drop(columns=[s])
            if others.shape[1] < 2:
                continue
            bench = others.mean(axis=1)
            dfm = pd.concat([Rm[s], bench], axis=1, keys=["r", "b"]).dropna()
            if len(dfm) < C.M1_MIN_MONTHS:
                continue
            b, a = np.polyfit(dfm["b"].values, dfm["r"].values, 1)
            eps = dfm["r"] - (a + b * dfm["b"])
            win = eps.iloc[-12:-1]
            sd = win.std()
            if len(win) >= 10 and sd > 0:
                m1[s] = float(win.sum() / sd)
        L["M1"] = L["symbol"].map(m1)
        mp = pd.concat([pct(L[c]) for c in ("M1", "M2", "M3")], axis=1)
        L["MOM"] = mp.mean(axis=1, skipna=True)

        # fundamentals
        fund = {s: F.load(s) for s in L["symbol"]}
        L["has_fund"] = L["symbol"].map(lambda s: fund[s] is not None)
        for s, f in fund.items():
            if f is None:
                self.issue(s, "no fundamentals: not in Tickertape snapshot and no Screener file (new listing?)")
            elif f["file_age_days"] > (C.TT_STALE_DAYS if f.get("source") == "tickertape" else C.FUND_STALE_DAYS):
                self.issue(s, f"{f.get('source', 'fundamentals')} data {f['file_age_days']:.0f} days old")
        fm = fin_m.set_index("symbol") if len(fin_m) else pd.DataFrame()
        pl = pledge.set_index("symbol") if len(pledge) else pd.DataFrame()

        def qinputs(s, model):
            f = fund[s]
            if f is None:
                return {}
            fam = QUAL_FAMILY.get(model, "nonfin")
            if fam == "nonfin":
                return {"Q1": f["q1"], "Q2": f["q2"], "Q3": f["q3"], "Q4": f["q4"], "Q5": f["q5"]}
            if fam == "fin":
                g = fm.loc[s] if s in fm.index else {}
                gn = g.get("gnpa_pct", np.nan) if len(g) else np.nan
                cr = g.get("crar_pct", np.nan) if len(g) else np.nan
                return {"Q1": f["roa3"], "Q2": f["roe3"], "Q3": -gn if pd.notna(gn) else np.nan,
                        "Q4": f["payout"], "Q5": cr}
            return {"Q1": f["roe3"], "Q2": f["payout"], "Q3": f["neg_roe_sd5"]}

        Q = pd.DataFrame([{**{"symbol": s}, **qinputs(s, m)} for s, m in zip(L["symbol"], L["model"])]).set_index("symbol")
        L = L.set_index("symbol")
        L["fam"] = L["model"].map(lambda m: QUAL_FAMILY.get(m, "nonfin"))
        qual = pd.Series(np.nan, index=L.index)
        for fam, grp in L.groupby("fam"):
            sub = Q.loc[grp.index].dropna(axis=1, how="all")
            if sub.empty:
                continue
            p = sub.apply(pct)
            enough = p.notna().sum(axis=1) >= 2
            qual.loc[grp.index] = p.mean(axis=1).where(enough)
        L["QUAL"] = qual
        L["QUAL_rank"] = pct(L["QUAL"])
        for c in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            if c in Q:
                L[c] = Q[c]
        L["fscore"] = [fund[s]["fscore"] if fund[s] else np.nan for s in L.index]
        L["fscore_n"] = [fund[s]["fscore_n"] if fund[s] else np.nan for s in L.index]
        L["pledge"] = [pl.loc[s, "pledge_pct"] if s in pl.index
                       else (fund[s].get("pledge", np.nan) if fund[s] else np.nan) for s in L.index]
        L["fund_src"] = [fund[s].get("source", "") if fund[s] else "" for s in L.index]

        # valuation, ranked within each valuation model inside the peer group
        vrows = {}
        for s in L.index:
            f = fund[s]
            if f is None:
                continue
            mcap = L.at[s, "price"] * f["shares"] / 1e7 if f["shares"] and np.isfinite(f["shares"]) else np.nan
            ev = mcap + f["borrow"] - f["cash"] if np.isfinite(mcap) else np.nan
            pos = lambda v: v if (v is not None and np.isfinite(v) and v > 0) else np.nan
            mult = {"P/B": _safe(mcap, pos(f["equity"])), "EV/Sales": _safe(ev, pos(f["sales_ttm"])),
                    "EV/EBITDA": _safe(ev, pos(f["ebitda_ttm"])), "EV/EBITDA5y": _safe(ev, pos(f["ebitda_5y"]))}
            drv = {"roe3": f["roe3"], "roce": f["roce"], "roce5": f["roce5"],
                   "ebitda_margin": _safe(f["ebitda_ttm"], pos(f["sales_ttm"])),
                   "ebit_margin": _safe(f["ebit_ttm"], pos(f["sales_ttm"]))}
            eq = f["equity"]
            yld = {"E/P": _safe(f["np_ttm"], mcap),
                   "FCF": _safe(f["cfo"] - f["capex"], mcap) if np.isfinite(f["capex"]) else np.nan,
                   "B/P": _safe(eq, mcap) if (eq is not None and np.isfinite(eq) and eq > 0) else F.NEG_INF}
            mname, dname, yname = VAL_MODELS.get(L.at[s, "model"], (None, None, "E/P"))
            vrows[s] = {"multiple": mult.get(mname, np.nan) if mname else np.nan,
                        "driver": drv.get(dname, np.nan) if dname else np.nan,
                        "V2": yld.get(yname, np.nan), "mcap_cr": mcap,
                        "val_model": f"{mname or '-'} vs {dname or '-'} | {yname}"}
        V = pd.DataFrame.from_dict(vrows, orient="index")
        L["VAL"] = np.nan
        L["V1"] = np.nan
        if not V.empty:
            for c in V.columns:
                L[c] = V[c]
            vgroup = L["model"].replace({"psu_bank": "bank"})       # PSU and private banks: one valuation pool
            for model, grp in L[L.index.isin(V.index)].groupby(vgroup[L.index.isin(V.index)]):
                ok = grp["multiple"].gt(0) & grp["driver"].notna() & grp["driver"].gt(0)
                g_ok = grp[ok]
                if len(g_ok) >= C.V1_MIN_PEERS:
                    x = g_ok["driver"].clip(*g_ok["driver"].quantile(list(C.WINSOR)).values).values.astype(float)
                    y = np.log(g_ok["multiple"].values.astype(float))
                    a, b = theil_sen(x, y)
                    L.loc[g_ok.index, "V1"] = -(y - (a + b * x))
                v1p = pct(L.loc[grp.index, "V1"])
                v2p = pct(L.loc[grp.index, "V2"])
                L.loc[grp.index, "VAL"] = pd.concat([v1p, v2p], axis=1).mean(axis=1, skipna=True)
                if len(grp) < 3:
                    L.loc[grp.index, "VAL"] = 50.0          # too few to compare: neutral
        L["VAL_filled"] = L["VAL"].isna()
        L["VAL"] = L["VAL"].fillna(50.0)

        # gates (doc 2d order)
        nonfin = L["fam"] == "nonfin"
        f_need = L["fscore_n"].apply(lambda n: math.ceil(C.FSCORE_MIN * n / 9) if pd.notna(n) and n > 0 else np.nan)
        L["g_quality"] = (L["has_fund"] & L["QUAL"].notna() & (L["QUAL_rank"] >= (1 - C.QUAL_TOP_FRACTION) * 100)
                          & (~nonfin | (L["fscore"] >= f_need))
                          & (L["pledge"].isna() | (L["pledge"] < C.PLEDGE_MAX)))
        L["g_momentum"] = L["rrg_vs_sector"].isin(["Leading", "Improving"]) & (L["MOM"] >= C.MOM_GATE)
        L["pass_all"] = L["g_quality"] & L["g_momentum"]
        L["StockScore"] = (weights["QUAL"] * L["QUAL"].fillna(0) + weights["MOM"] * L["MOM"].fillna(0)
                           + weights["VAL"] * L["VAL"])
        L["n_liquid_peers"] = n_peers
        L = L.reset_index().rename(columns={"index": "symbol"})
        out = pd.concat([L, T[~T["liq_ok"]]], ignore_index=True)
        out["sector_index"] = index_name
        out["rank"] = out["StockScore"].where(out["pass_all"] == True).rank(ascending=False, method="first").astype("Int64")
        return out.sort_values(["pass_all", "StockScore"], ascending=[False, False]).reset_index(drop=True)

    # -------------------------------------------------------------- run everything
    def run(self) -> dict:
        self.log("Loading universe and instrument map ...")
        uni = U.build_universe()
        imap = D.instrument_map()
        try:
            known = D.index_keys()
            for n, k in C.SECTORS_MAPPING.items():
                if k not in known:
                    self.issue(n, f"'{k}' is not in Upstox's index list - fix SECTORS_MAPPING in config.py")
        except Exception:
            pass
        self.log("Fetching NIFTY 50 and 18 sector indices ...")
        bench_d = self.get_daily(C.BENCH_NAME, C.BENCH_KEY, adjust=False)
        if bench_d is None:
            raise D.DataError("NIFTY 50 data missing - cannot run")
        self.fetch_many([(n, k) for n, k in C.SECTORS_MAPPING.items()], "indices")
        for n in C.SECTORS_MAPPING:
            if self.daily.get(n) is None:
                self.issue(n, "index data missing - sector skipped (no fallback to NIFTY 50)")
        reg = self.regime(bench_d)
        weights = C.W_BEAR if reg["bear"] else C.W_NORMAL
        self.log(("BEAR regime: " if reg["bear"] else "Normal regime: ") + reg["detail"])

        S = self.step1(bench_d, uni, imap, C.RRG_TF)          # monthly RRG drives the screen
        chosen = list(S.loc[S["selected"] == True, "index"])
        self.log(f"Selected sectors ({C.RRG_TF}ly RRG): {', '.join(chosen) if chosen else 'none'}")
        S_o, cmp_ = pd.DataFrame(), pd.DataFrame()
        if C.LIVE_TF_COMPARISON:
            other = "week" if C.RRG_TF == "month" else "month"
            self.log(f"Comparison only: same sector screen on {other}ly bars ...")
            S_o = self.step1(bench_d, uni, imap, other)
            S_w, S_m = (S_o, S) if C.RRG_TF == "month" else (S, S_o)
            cmp_ = compare_tf(S_w, S_m)

        hold = _read_opt_csv("holdings.csv", ["symbol", "sector_index"])
        extra = [x for x in hold.get("sector_index", pd.Series(dtype=str)).dropna().unique()
                 if x in C.SECTORS_MAPPING and x not in chosen and x in U.INDEX_MODELS]
        fin_m = _read_opt_csv("fin_metrics.csv", ["symbol", "gnpa_pct", "crar_pct"])
        pledge = _read_opt_csv("pledge.csv", ["symbol", "pledge_pct"])

        tables = {}
        for name in chosen + extra:
            self.log(f"Scoring stocks in {name} ...")
            tables[name] = self.step2_sector(name, uni, imap, weights, fin_m, pledge)
        stocks = pd.concat(tables.values(), ignore_index=True) if tables else pd.DataFrame()

        port = self.portfolio(S, tables, chosen, reg, hold)
        hc = self.holdings_check(hold, S, tables)
        prices = [{"symbol": "NIFTY 50" if k == C.BENCH_NAME else k, "close": float(v["close"].iloc[-1]),
                   "date": v.index[-1].date().isoformat()}
                  for k, v in self.daily.items() if v is not None and len(v)]
        return {"run_at": self.now, "regime": reg, "weights": weights, "sectors": S, "sectors_other_tf": S_o,
                "prices": pd.DataFrame(prices, columns=["symbol", "close", "date"]),
                "rrg_compare": cmp_, "stocks": stocks,
                "portfolio": port["table"], "portfolio_note": port["note"], "holdings": hc,
                "issues": pd.DataFrame(self.issues, columns=["item", "issue"]).drop_duplicates()}

    # -------------------------------------------------------------- Step 2d + 3
    def portfolio(self, S, tables, chosen, reg, hold) -> dict:
        slot = 1 / C.N_SLOTS
        held = set(hold["symbol"]) if len(hold) else set()
        rows = []
        for name in chosen:
            T = tables.get(name)
            if T is None or T.empty:
                continue
            n_liq = int(T["n_liquid_peers"].dropna().iloc[0]) if T["n_liquid_peers"].notna().any() else 0
            k = C.MAX_PER_SMALL_SECTOR if n_liq < C.SMALL_SECTOR_PEERS else C.MAX_PER_SECTOR
            picks = T[T["pass_all"] == True].head(k)
            for _, r in picks.iterrows():
                if C.BEAR_BLOCKS_NEW_BUYS and reg["bear"] and r["symbol"] not in held:
                    continue                                   # v3.1 behaviour only
                rows.append({"slot": len(rows) + 1, "symbol": r["symbol"], "company": r["company"],
                             "sector_index": name, "StockScore": round(r["StockScore"], 1),
                             "QUAL": round(r["QUAL"], 1), "MOM": round(r["MOM"], 1), "VAL": round(r["VAL"], 1),
                             "weight": slot, "price": r["price"]})
        P = pd.DataFrame(rows)
        n_free = C.N_SLOTS - len(P)
        parking = "Liquid fund" if (reg["bear"] and C.BEAR_BLOCKS_NEW_BUYS) else "NIFTY 50 ETF"
        if n_free > 0:
            P = pd.concat([P, pd.DataFrame([{"slot": "free", "symbol": parking, "company": f"{n_free} unused slots",
                                             "weight": n_free * slot}])], ignore_index=True)

        # vol scaling (doc 3.1): realised vol of the equal-weight picks, last 6 months
        note = ""
        stocks = [s for s in P["symbol"] if s in self.daily and self.daily[s] is not None]
        if stocks:
            rets = pd.DataFrame({s: self.daily[s]["close"].pct_change() for s in stocks}).iloc[-C.VOL_LOOKBACK_DAYS:]
            sig = float(rets.mean(axis=1).std() * np.sqrt(252))
            if C.SIGMA_TARGET:
                expo = min(1.0, C.SIGMA_TARGET / sig) if sig > 0 else 1.0
                note = f"Realised vol {sig*100:.1f}% vs target {C.SIGMA_TARGET*100:.1f}% -> equity exposure {expo*100:.0f}%"
                P.loc[P["slot"] != "free", "weight"] *= expo
                P = pd.concat([P, pd.DataFrame([{"slot": "vol", "symbol": "Liquid fund",
                                                 "company": "vol-scaling cash",
                                                 "weight": 1 - P["weight"].sum()}])], ignore_index=True)
            else:
                note = (f"Realised vol of picks {sig*100:.1f}%. SIGMA_TARGET not set yet (set it from the "
                        f"build-period backtest), so exposure stays 100%.")
        P["weight_%"] = (P["weight"] * 100).round(2)
        return {"table": P.drop(columns=["weight"]), "note": note}

    def holdings_check(self, hold, S, tables) -> pd.DataFrame:
        if hold is None or hold.empty:
            return pd.DataFrame()
        out = []
        for _, h in hold.iterrows():
            s, idx = h["symbol"], h.get("sector_index")
            reasons = []
            srow = S[S["index"] == idx]
            if not srow.empty and srow.iloc[0].get("exit_flags"):
                reasons.append("SECTOR EXIT: " + srow.iloc[0]["exit_flags"])
            T = tables.get(idx)
            if T is None:
                reasons.append("sector not scored - add sector_index in holdings.csv")
            else:
                r = T[T["symbol"] == s]
                if r.empty:
                    reasons.append("not in sector peer list")
                else:
                    r = r.iloc[0]
                    n = r.get("n_liquid_peers", np.nan)
                    n = int(n) if pd.notna(n) else 0
                    top = C.SELL_BUFFER_TOP if n >= 10 else max(1, n // 2)
                    if not r.get("g_quality", False):
                        reasons.append("failed quality gate")
                    if r.get("rrg_vs_sector") == "Lagging":
                        reasons.append("RRG vs sector Lagging")
                    if pd.isna(r.get("rank")) or r["rank"] > top:
                        reasons.append(f"outside top {top}")
            out.append({"symbol": s, "sector_index": idx, "action": "SELL" if reasons else "HOLD",
                        "reasons": "; ".join(reasons)})
        return pd.DataFrame(out)


def compare_tf(S_w: pd.DataFrame, S_m: pd.DataFrame) -> pd.DataFrame:
    """Side-by-side weekly vs monthly Step 1, one row per index."""
    cols = ["index", "group", "quadrant", "rs_ratio", "rs_mom", "above_trend", "breadth", "eligible", "selected"]
    w = S_w.reindex(columns=cols).set_index("index")
    m = S_m.reindex(columns=cols).set_index("index")
    out = w.drop(columns="group").add_prefix("W_").join(m.drop(columns="group").add_prefix("M_"), how="outer")
    out.insert(0, "group", w["group"].combine_first(m["group"]))
    def verdict(r):
        ws, ms = r["W_selected"] == True, r["M_selected"] == True
        if ws and ms:
            return "Both select"
        if ws or ms:
            return "Weekly only" if ws else "Monthly only"
        return "Same quadrant" if r["W_quadrant"] == r["M_quadrant"] else "Quadrants differ"
    out["agreement"] = out.apply(verdict, axis=1)
    order = {"Both select": 0, "Weekly only": 1, "Monthly only": 2, "Quadrants differ": 3, "Same quadrant": 4}
    out = out.reset_index().rename(columns={"index": "index"})
    return out.sort_values(by="agreement", key=lambda s: s.map(order)).reset_index(drop=True)


def _safe(a, b):
    try:
        if a is None or b is None or not np.isfinite(a) or not np.isfinite(b) or b == 0:
            return np.nan
        return a / b
    except TypeError:
        return np.nan


def export_excel(res: dict, path: str):
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame([{"run_at": res["run_at"], "bear_regime": res["regime"]["bear"],
                       "regime_detail": res["regime"]["detail"], "weights": str(res["weights"]),
                       "portfolio_note": res["portfolio_note"]}]).to_excel(xw, sheet_name="Summary", index=False)
        res["sectors"].to_excel(xw, sheet_name="Step1 Sectors", index=False)
        if len(res.get("rrg_compare", [])):
            res["rrg_compare"].to_excel(xw, sheet_name="RRG weekly vs monthly", index=False)
        res["portfolio"].to_excel(xw, sheet_name="Portfolio", index=False)
        if not res["stocks"].empty:
            res["stocks"].to_excel(xw, sheet_name="Step2 Stocks", index=False)
        if not res["holdings"].empty:
            res["holdings"].to_excel(xw, sheet_name="Holdings check", index=False)
        res["issues"].to_excel(xw, sheet_name="Data issues", index=False)
        if len(res.get("prices", [])):          # last close of every symbol fetched (for the Paper Trader app)
            res["prices"].to_excel(xw, sheet_name="Prices", index=False)
