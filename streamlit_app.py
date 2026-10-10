"""
streamlit_app.py - My Formula screener as a web app (phone-friendly), for Streamlit Community Cloud.

Same engine as app.py. The screen runs in a background thread on the server, so it keeps going
if your phone locks; the last result is kept on the server and shown when you reopen the page.

Secrets (Streamlit Cloud -> App -> Settings -> Secrets):
    UPSTOX_ANALYTICS_TOKEN = "eyJ..."
    APP_PASSWORD = "something"          # optional extra lock
"""
import io
import os
import pickle
import threading
import time
import traceback
import zipfile
from datetime import datetime

os.environ["TZ"] = "Asia/Kolkata"             # completed-bar rules use Indian market time
if hasattr(time, "tzset"):
    time.tzset()

import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
_zip = os.path.join(HERE, "screener_xlsx.zip")  # fundamentals shipped as one zip (GitHub web upload limit)
if os.path.exists(_zip) and not os.path.isdir(os.path.join(HERE, "screener_xlsx")):
    with zipfile.ZipFile(_zip) as z:
        z.extractall(HERE)

try:                                          # token from Streamlit secrets -> env, read by data_upstox
    if "UPSTOX_ANALYTICS_TOKEN" in st.secrets:
        os.environ["UPSTOX_ANALYTICS_TOKEN"] = st.secrets["UPSTOX_ANALYTICS_TOKEN"]
except Exception:
    pass

import config as C          # noqa: E402
import data_upstox as D     # noqa: E402
import engine as E          # noqa: E402

LAST = os.path.join(C.OUTPUT_DIR, "last_result.pkl")
HOLD = os.path.join(C.INPUT_DIR, "holdings.csv")

st.set_page_config(page_title="My Formula", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
.block-container{padding-top:1.2rem;padding-bottom:3rem}
div[data-testid="stMetricValue"]{font-size:1.25rem}
</style>""", unsafe_allow_html=True)


# ------------------------------------------------------------------ optional password
def locked() -> bool:
    pw = ""
    try:
        pw = st.secrets.get("APP_PASSWORD", "")
    except Exception:
        pass
    if not pw or st.session_state.get("ok"):
        return False
    st.title("My Formula")
    x = st.text_input("Password", type="password")
    if x:
        if x == pw:
            st.session_state.ok = True
            st.rerun()
        st.error("Wrong password")
    return True


if locked():
    st.stop()


# ------------------------------------------------------------------ background job (survives page reloads)
class Job:
    def __init__(self):
        self.running = False
        self.log: list[str] = []
        self.progress = (0, 0, "")
        self.error = ""
        self.started = None
        self.lock = threading.Lock()


@st.cache_resource
def job() -> Job:
    return Job()


def run_screen(J: Job, tok: str):
    try:
        sc = E.Screener(tok, log=lambda m: J.log.append(f"{datetime.now():%H:%M:%S}  {m}"),
                        progress=lambda d, t, l="": setattr(J, "progress", (d, t, l)))
        res = sc.run()
        bio = io.BytesIO()
        E.export_excel(res, bio)
        res["xlsx"] = bio.getvalue()
        with open(LAST, "wb") as f:
            pickle.dump(res, f)
        J.log.append(f"{datetime.now():%H:%M:%S}  Done.")
    except Exception as e:
        J.error = f"{e}"
        J.log.append(traceback.format_exc())
    finally:
        J.running = False


def start(J: Job):
    tok = D.load_token()
    if not tok:
        st.error("No Upstox analytics token. Add UPSTOX_ANALYTICS_TOKEN in the app's Secrets.")
        return
    with J.lock:
        if J.running:
            return
        J.running, J.error, J.log, J.started, J.progress = True, "", [], datetime.now(), (0, 0, "")
    threading.Thread(target=run_screen, args=(J, tok), daemon=True).start()


def last_result():
    if os.path.exists(LAST):
        try:
            with open(LAST, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None
    return None


# ------------------------------------------------------------------ header
J = job()
res = last_result()
st.title("My Formula")
st.caption("Monthly RRG sector screen → stock ranking → 12-slot portfolio · v3.2 · research tool, not advice")

c1, c2 = st.columns([1, 2])
with c1:
    if st.button("▶ Run screen", type="primary", disabled=J.running, width="stretch"):
        start(J)
        st.rerun()
with c2:
    if res is not None:
        st.download_button("Download Excel", res["xlsx"], file_name=f"MyFormula_{res['run_at']:%Y-%m-%d_%H%M}.xlsx",
                           width="stretch")

if J.running:
    d, t, l = J.progress
    st.info(f"Running since {J.started:%H:%M}. You can lock your phone; reopen this page to see the result.")
    st.progress(d / t if t else 0.0, text=f"{l} {d}/{t}" if t else "starting…")
    with st.expander("Log", expanded=False):
        st.code("\n".join(J.log[-40:]) or "…")
    time.sleep(3)
    st.rerun()
if J.error:
    st.error(f"Last run failed: {J.error}")

if res is None:
    st.warning("No result yet. Tap **Run screen** (2–5 minutes the first time each day).")
    st.stop()

reg = res["regime"]
w = res["weights"]
m1, m2, m3 = st.columns(3)
m1.metric("Last run", f"{res['run_at']:%d %b %Y, %H:%M}")
m2.metric("Regime", "BEAR" if reg["bear"] else "Normal")
m3.metric("Weights Q/M/V", f"{w['QUAL']:.2f} / {w['MOM']:.2f} / {w['VAL']:.2f}")
st.caption(reg["detail"])


def show(df: pd.DataFrame, cols: list[str]):
    x = df[[c for c in cols if c in df.columns]].copy()
    for c in x.columns:
        if x[c].dtype == object:            # mixed types (e.g. slot 1..12 and "free") break Arrow
            x[c] = x[c].map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
    st.dataframe(x, hide_index=True, width="stretch")


tabs = st.tabs(["Portfolio", "Sectors", "Stocks", "Holdings", "Issues", "Log"])

with tabs[0]:
    P = res["portfolio"]
    show(P, ["slot", "symbol", "sector_index", "StockScore", "QUAL", "MOM", "VAL", "price", "weight_%"])
    if res.get("portfolio_note"):
        st.caption(res["portfolio_note"])

with tabs[1]:
    S = res["sectors"].copy()
    st.caption("Decisions use completed months. live_* = including the unfinished month (same as RRG v6 on month); changes daily.")
    sel = S[S["selected"] == True]
    if len(sel):
        st.success("Selected: " + ", ".join(sel["index"]))
    show(S, ["index", "status", "quadrant", "rs_ratio", "rs_mom", "live_quadrant", "live_rs_ratio", "live_rs_mom",
             "above_trend", "breadth", "exit_flags", "bar_of", "live_bar_of"])

with tabs[2]:
    T = res["stocks"]
    if T.empty:
        st.info("No sector selected, so no stocks scored.")
    else:
        secs = list(T["sector_index"].unique())
        pick = st.selectbox("Sector", secs)
        only = st.toggle("Only stocks passing all gates", value=False)
        x = T[T["sector_index"] == pick]
        if only:
            x = x[x["pass_all"] == True]
        show(x.sort_values("rank"), ["rank", "symbol", "StockScore", "QUAL", "MOM", "VAL", "pass_all", "rrg_vs_sector",
                                     "live_rrg_vs_sector", "fscore", "liq_cr", "price"])

with tabs[3]:
    H = res["holdings"]
    if H is not None and len(H):
        show(H, ["symbol", "sector_index", "action", "reasons"])
    st.markdown("**Your holdings** (used by the next run). One per line: `SYMBOL,Sector index`")
    cur = open(HOLD, encoding="utf-8").read() if os.path.exists(HOLD) else "symbol,sector_index\n"
    txt = st.text_area("holdings.csv", cur, height=180, label_visibility="collapsed")
    if st.button("Save holdings"):
        body = txt.strip()
        if not body.lower().startswith("symbol"):
            body = "symbol,sector_index\n" + body
        open(HOLD, "w", encoding="utf-8").write(body + "\n")
        st.success("Saved. Run the screen to get SELL/HOLD for them.")
    st.caption("Note: the free host resets its disk when the app restarts. Keep holdings.csv in your GitHub repo too.")

with tabs[4]:
    show(res["issues"], ["item", "issue"])

with tabs[5]:
    st.code("\n".join(J.log[-200:]) or "No run in this server session yet.")
