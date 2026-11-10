"""
report.py - exports one screen result as
    1) a PDF report with the reasoning behind every decision (regime, sectors, picks, sells, orders)
    2) a Kite order spreadsheet (.xlsx) + a basket file (.json) to place everything as basket orders

Used by app.py (button "Export PDF + Kite orders"), streamlit_app.py (download buttons) and bot.py.

Kite basket notes
- Kite allows up to 20 orders per basket, so orders are split into Basket 1, Basket 2, ...
- Sells come first, so the money is free before the buys.
- If inputs/holdings.csv has a "qty" column (symbol,sector_index,qty), orders are the DIFFERENCE between what
  you hold and the target (a true rebalance). Without it, the list is the full target portfolio to buy, and
  SELL lines carry no quantity (fill in what you hold).
- The .json uses Kite's documented basket fields (exchange, tradingsymbol, transaction_type, quantity,
  order_type, price, product, variety, validity). Kite web's "Import basket" reads the JSON that Kite itself
  exports; if it rejects this file, add the orders from the spreadsheet (2 minutes) and send one exported
  basket file to Claude so the format can be matched exactly.
Research tool, not investment advice.
"""
from __future__ import annotations

import io
import json
import math
import os
from datetime import datetime

import numpy as np
import pandas as pd

import config as C
import engine as E


# ------------------------------------------------------------------ helpers
def _tick(p: float) -> float:
    """Round to NSE tick size Rs 0.05."""
    return round(round(p / 0.05) * 0.05, 2)


def _price_of(res: dict, sym: str) -> float | None:
    pr = res.get("prices")
    if pr is not None and len(pr):
        r = pr[pr["symbol"] == sym]
        if len(r):
            return float(r["close"].iloc[-1])
    st = res.get("stocks")
    if st is not None and len(st) and "symbol" in st:
        r = st[st["symbol"] == sym]
        if len(r) and pd.notna(r["price"].iloc[0]):
            return float(r["price"].iloc[0])
    return None


def _holdings_qty() -> dict:
    p = os.path.join(C.INPUT_DIR, "holdings.csv")
    if not os.path.exists(p):
        return {}
    h = pd.read_csv(p)
    h.columns = [c.strip().lower() for c in h.columns]
    if "qty" not in h or "symbol" not in h:
        return {}
    h["symbol"] = h["symbol"].astype(str).str.strip().str.upper()
    q = pd.to_numeric(h["qty"], errors="coerce")
    return {s: int(v) for s, v in zip(h["symbol"], q) if pd.notna(v)}


# ------------------------------------------------------------------ orders
def kite_orders(res: dict, capital: float, etf: bool = True) -> pd.DataFrame:
    """One row per order, sells first, split into baskets of KITE_BASKET_MAX."""
    P = res["portfolio"]
    O = E.order_sheet(P, capital, etf=etf) if capital and capital > 0 else pd.DataFrame()
    target, why = {}, {}
    if len(O):
        for _, r in O[O["slot"].astype(str).str.isdigit()].iterrows():
            target[r["symbol"]] = int(r["qty"])
            why[r["symbol"]] = f"pick, target {r.get('target_%', r.get('weight_%', ''))}%"
        if etf:
            free = O[O["slot"] == "free"]
            amt = float(free["amount_Rs"].iloc[0]) if len(free) else 0.0
            px = _price_of(res, C.ETF_SYMBOL)
            if amt > 0:
                if px:
                    target[C.ETF_SYMBOL] = int(math.floor(amt / px))
                    why[C.ETF_SYMBOL] = f"free slots in NIFTY 50 ETF (Rs {amt:,.0f})"
                else:
                    target[C.ETF_SYMBOL] = None
                    why[C.ETF_SYMBOL] = f"buy Rs {amt:,.0f} of the ETF (price not available - enter quantity)"
    held = _holdings_qty()
    H = res.get("holdings")
    sell_flag = {}
    if H is not None and len(H):
        sell_flag = {r["symbol"]: r.get("reasons", "") for _, r in H.iterrows() if r.get("action") == "SELL"}
    rows = []
    if held:                                            # true rebalance: difference to what you hold
        for s in sorted(set(held) | set(target)):
            t = target.get(s, 0)
            if t is None:
                rows.append({"side": "BUY", "symbol": s, "qty": None, "reason": why[s]})
                continue
            d = t - held.get(s, 0)
            if d > 0:
                rows.append({"side": "BUY", "symbol": s, "qty": d,
                             "reason": why.get(s, "") + (f"; you hold {held[s]}" if s in held else "")})
            elif d < 0:
                rs = sell_flag.get(s) or ("not in the new target" if s not in target else f"trim to {t}")
                rows.append({"side": "SELL", "symbol": s, "qty": -d, "reason": rs})
    else:
        for s, why_s in sell_flag.items():
            rows.append({"side": "SELL", "symbol": s, "qty": None, "reason": why_s + " (enter the quantity you hold)"})
        for s, t in target.items():
            if s in sell_flag:
                continue
            rows.append({"side": "BUY", "symbol": s, "qty": t, "reason": why[s]})
    df = pd.DataFrame(rows, columns=["side", "symbol", "qty", "reason"])
    if df.empty:
        return df
    df["price_ref"] = [(_price_of(res, s) or np.nan) for s in df["symbol"]]
    df["limit_price"] = [(_tick(p) if pd.notna(p) else np.nan) for p in df["price_ref"]]
    df["value_Rs"] = (df["qty"].astype(float) * df["price_ref"]).round(0)
    df = df.sort_values(["side", "symbol"], key=lambda c: c.map({"SELL": 0, "BUY": 1}) if c.name == "side" else c)
    df = df.reset_index(drop=True)
    df.insert(0, "basket", (df.index // C.KITE_BASKET_MAX) + 1)
    return df


def kite_rows(df: pd.DataFrame) -> list[dict]:
    """Orders in Kite's documented basket field names."""
    out = []
    for _, r in df.iterrows():
        o = {"exchange": "NSE", "tradingsymbol": r["symbol"], "transaction_type": r["side"],
             "quantity": int(r["qty"]) if pd.notna(r["qty"]) else 0, "product": C.KITE_PRODUCT,
             "order_type": C.KITE_ORDER_TYPE, "variety": "regular", "validity": "DAY"}
        if C.KITE_ORDER_TYPE == "LIMIT" and pd.notna(r["limit_price"]):
            o["price"] = float(r["limit_price"])
        out.append(o)
    return out


def export_orders_xlsx(res: dict, capital: float, etf: bool, path_or_buf) -> pd.DataFrame:
    df = kite_orders(res, capital, etf)
    kite = pd.DataFrame(kite_rows(df)) if len(df) else pd.DataFrame()
    with pd.ExcelWriter(path_or_buf, engine="openpyxl") as xw:
        if len(df):
            k = kite.rename(columns={"exchange": "Exchange", "tradingsymbol": "Tradingsymbol",
                                     "transaction_type": "Transaction type", "quantity": "Quantity",
                                     "product": "Product", "order_type": "Order type", "price": "Price",
                                     "variety": "Variety", "validity": "Validity"})
            k.insert(0, "Basket", df["basket"].values)
            k["Reason"] = df["reason"].values
            k.to_excel(xw, sheet_name="Kite orders", index=False)
            v = df.rename(columns={"price_ref": "last_close"})
            v.to_excel(xw, sheet_name="Order details", index=False)
        how = pd.DataFrame({"How to place these in Kite": [
            f"Run of {res['run_at']:%d %b %Y %H:%M}. Capital Rs {capital:,.0f}. "
            + ("Free slots in NIFTY 50 ETF." if etf else "No ETF: 100% in stocks."),
            "1. Kite web -> Orders -> Baskets -> New basket (name it e.g. 'MyFormula Basket 1').",
            "2. Either: click 'Import basket' and choose MyFormula_kite_basket_1.json (if Kite accepts it),",
            "   or: add each row of sheet 'Kite orders' with that Basket number (search symbol, BUY/SELL, qty, CNC).",
            f"3. Max {C.KITE_BASKET_MAX} orders per basket - use Basket 2 for the rest. Place SELL baskets first.",
            f"4. Orders are {C.KITE_ORDER_TYPE} at the last close; edit the price in Kite if the market has moved.",
            "5. Check the margin shown by Kite, then 'Execute'. Afterwards update inputs/holdings.csv (add a qty column).",
            "Rebalancing: if holdings.csv has a qty column, enter as capital the TOTAL value (current holdings + new cash);",
            "   the list then shows only the difference to buy or sell. Rows without a quantity are not put in the .json.",
            "Research tool, not investment advice."]})
        how.to_excel(xw, sheet_name="How to", index=False)
    return df


def basket_json_files(res: dict, capital: float, etf: bool) -> dict:
    """{filename: bytes} - one JSON per basket (Kite documented basket fields)."""
    df = kite_orders(res, capital, etf)
    out = {}
    if len(df):
        df = df[df["qty"].notna() & (df["qty"].astype(float) > 0)]   # a basket order needs a quantity
    for b, g in df.groupby("basket") if len(df) else []:
        out[f"MyFormula_kite_basket_{b}.json"] = json.dumps(kite_rows(g), indent=1).encode()
    return out


# ------------------------------------------------------------------ PDF report
def _fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    cands = [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
             ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf")]
    for reg, bold in cands:
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("RPT", reg)); pdfmetrics.registerFont(TTFont("RPTB", bold))
                from reportlab.pdfbase.pdfmetrics import registerFontFamily
                registerFontFamily("RPT", normal="RPT", bold="RPTB", italic="RPT", boldItalic="RPTB")
                return "RPT", "RPTB", "₹"
            except Exception:
                pass
    return "Helvetica", "Helvetica-Bold", "Rs "


def _f(v, d=1):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA:
            return "–"
        return f"{float(v):.{d}f}"
    except Exception:
        return str(v)


def _stock_reason(r, S) -> str:
    sec = S[S["index"] == r.get("sector_index")]
    q = sec.iloc[0] if len(sec) else {}
    parts = [f"Sector {r.get('sector_index')} is {q.get('quadrant', '?')} vs NIFTY 50 "
             f"(RS-Ratio {_f(q.get('rs_ratio'))}, RS-Mom {_f(q.get('rs_mom'))}, breadth {_f(100 * q.get('breadth', np.nan), 0)}%)."
             if len(sec) else ""]
    parts.append(f"Stock is {r.get('rrg_vs_sector', '?')} vs its sector; momentum {_f(r.get('MOM'), 0)}/100 "
                 f"(rank among {int(r.get('n_liquid_peers', 0) or 0)} liquid peers: #{r.get('rank', '–')}).")
    parts.append(f"Quality {_f(r.get('QUAL'), 0)}/100, F-score {_f(r.get('fscore'), 0)}/9, "
                 f"pledge {_f(r.get('pledge'), 1)}%. Value {_f(r.get('VAL'), 0)}/100. "
                 f"Liquidity {_f(r.get('liq_cr'), 1)} cr/day.")
    return " ".join(p for p in parts if p)


def _why_not(r) -> str:
    out = []
    if not r.get("liq_ok", True):
        return "too illiquid"
    if not r.get("g_quality", False):
        out.append("quality gate (QUAL/F-score/pledge)")
    if not r.get("g_momentum", False):
        out.append(f"momentum gate (RRG vs sector {r.get('rrg_vs_sector', '?')}, MOM {_f(r.get('MOM'), 0)})")
    return "failed " + " and ".join(out) if out else "passed, but sector already has its maximum picks"


def _rrg_plot(points: list[dict], title: str, xlabel: str):
    """RRG scatter with 6-bar tails. points: name, r, m, tr (list), tm (list), hi (bool)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    pts = [p for p in points if pd.notna(p.get("r")) and pd.notna(p.get("m"))]
    if not pts:
        return None
    vals = [v for p in pts for v in ([p["r"], p["m"]] + list(p.get("tr") or []) + list(p.get("tm") or []))
            if pd.notna(v)]
    span = max(4.0, max(abs(v - 100) for v in vals) + 1.0)
    lo, hi = 100 - span, 100 + span
    fig, ax = plt.subplots(figsize=(7, 5.4))
    ax.set_xlim(lo, hi); ax.set_ylim(lo, hi)
    for (x0, y0, x1, y1, c) in [(100, 100, hi, hi, "#e3f4ea"), (100, lo, hi, 100, "#fdf1de"),
                                (lo, lo, 100, 100, "#fbe5e5"), (lo, 100, 100, hi, "#e6eefc")]:
        ax.fill_between([x0, x1], y0, y1, color=c, zorder=0)
    ax.axhline(100, color="#8a94a6", lw=1); ax.axvline(100, color="#8a94a6", lw=1)
    for lab, x, y in [("Leading", hi, hi), ("Weakening", hi, lo), ("Lagging", lo, lo), ("Improving", lo, hi)]:
        ax.text(x, y, lab, ha="right" if x == hi else "left", va="top" if y == hi else "bottom", fontsize=9,
                color="#1b2a4a", alpha=0.55, fontweight="bold")
    palette = ["#2e9e5b", "#d64545", "#7b5cc4", "#1f9aa8", "#e8a33d", "#2f6fdb"]
    k = 0
    for p in sorted(pts, key=lambda p: p.get("hi", False)):
        if p.get("hi"):
            col = palette[k % len(palette)]; k += 1
            tr, tm = list(p.get("tr") or []), list(p.get("tm") or [])
            if len(tr) >= 2 and len(tr) == len(tm):
                ax.plot(tr, tm, "-", color=col, lw=1.6, alpha=0.75, zorder=3)
                ax.scatter(tr[:-1], tm[:-1], s=12, color=col, alpha=0.6, zorder=3)
            ax.scatter(p["r"], p["m"], s=80, color=col, edgecolor="white", zorder=4)
            ax.annotate(p["name"], (p["r"], p["m"]), xytext=(5, 4), textcoords="offset points", fontsize=8.5,
                        fontweight="bold", color=col)
        else:
            ax.scatter(p["r"], p["m"], s=16, color="#8a94a6", alpha=0.7, zorder=2)
            ax.annotate(p["name"], (p["r"], p["m"]), xytext=(3, 2), textcoords="offset points", fontsize=6.5,
                        color="#6b7385")
    ax.set_xlabel(xlabel); ax.set_ylabel("RS-Momentum")
    ax.set_title(title, fontsize=10, loc="left")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight"); plt.close(fig)
    buf.seek(0)
    return buf


def _sector_chart(S: pd.DataFrame):
    if "rs_ratio" not in S:
        return None
    pts = [{"name": str(r["index"]).replace("Nifty ", ""), "r": r.get("rs_ratio"), "m": r.get("rs_mom"),
            "tr": r.get("trail_ratio") if isinstance(r.get("trail_ratio"), list) else [],
            "tm": r.get("trail_mom") if isinstance(r.get("trail_mom"), list) else [], "hi": bool(r.get("selected"))}
           for _, r in S.iterrows() if r.get("group") != "Context"]
    return _rrg_plot(pts, "Sectors vs NIFTY 50 (completed months; coloured = selected, line = last 6 months)",
                     "RS-Ratio vs NIFTY 50")


def _stock_chart(T: pd.DataFrame, sector: str, picks: set):
    if T is None or not len(T) or "rs_mom_vs_sector" not in T:
        return None
    g = T[(T["sector_index"] == sector) & T.get("liq_ok", pd.Series(True, index=T.index)).fillna(False).astype(bool)]
    if g.empty:
        return None
    pts = [{"name": r["symbol"], "r": r.get("rs_ratio_vs_sector"), "m": r.get("rs_mom_vs_sector"),
            "tr": r.get("trail_ratio") if isinstance(r.get("trail_ratio"), list) else [],
            "tm": r.get("trail_mom") if isinstance(r.get("trail_mom"), list) else [], "hi": r["symbol"] in picks}
           for _, r in g.iterrows()]
    return _rrg_plot(pts, f"Stocks vs {sector} index (coloured = picked or held, line = last 6 months; grey = other peers)",
                     f"RS-Ratio vs {sector}")


def export_pdf(res: dict, capital: float, etf: bool, path_or_buf) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    FN, FB, RS = _fonts()
    NAVY, BLUE, GREY = colors.HexColor("#1b2a4a"), colors.HexColor("#2f6fdb"), colors.HexColor("#8a94a6")
    body = ParagraphStyle("b", fontName=FN, fontSize=8.8, leading=12, spaceAfter=3)
    small = ParagraphStyle("s", parent=body, fontSize=7.6, leading=10, textColor=colors.HexColor("#4a5265"))
    h1 = ParagraphStyle("h1", fontName=FB, fontSize=16, leading=20, textColor=NAVY, spaceAfter=4)
    h2 = ParagraphStyle("h2", fontName=FB, fontSize=11.5, leading=15, textColor=BLUE, spaceBefore=8, spaceAfter=4,
                        keepWithNext=1)
    cell = ParagraphStyle("c", fontName=FN, fontSize=7.6, leading=9.6)
    cellh = ParagraphStyle("ch", fontName=FB, fontSize=7.6, leading=9.6, textColor=colors.white)
    W = A4[0] - 30 * mm

    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("₹", RS)

    def tbl(head, rows, widths, bg=None):
        data = [[Paragraph(esc(h), cellh) for h in head]] + [[Paragraph(esc(c), cell) for c in r] for r in rows]
        t = Table(data, colWidths=[w * W for w in widths], repeatRows=1)
        stl = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP"),
               ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d5dae3")),
               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f5f8")]),
               ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
        for (r, c) in (bg or []):
            stl.append(("BACKGROUND", (0, r), (-1, r), c))
        t.setStyle(TableStyle(stl))
        return t

    S, T, P, H, reg = res["sectors"], res.get("stocks", pd.DataFrame()), res["portfolio"], res.get("holdings"), res["regime"]
    w = res["weights"]
    story = [Paragraph("My Formula - monthly screen report", h1),
             Paragraph(esc(f"Run {res['run_at']:%d %b %Y, %H:%M} · capital {RS}{capital:,.0f} · "
                           + ("free slots in NIFTY 50 ETF" if etf else "no ETF (100% in stocks)")), body),
             Paragraph("Research tool, not investment advice. Check every order before placing it.", small),
             Spacer(1, 4)]

    # 1 summary
    picks = P[P["slot"].astype(str).str.isdigit()] if len(P) else P
    sel = S[S["selected"] == True]
    sells = H[H["action"] == "SELL"] if H is not None and len(H) else pd.DataFrame()
    story += [Paragraph("1. Summary", h2)]
    summ = [["Market mood", ("BEAR" if reg["bear"] else "Normal") + f" - {reg['detail']}"],
            ["Score weights", f"Quality {w['QUAL']:.2f} · Momentum {w['MOM']:.2f} · Value {w['VAL']:.2f}"],
            ["Sectors selected", ", ".join(sel["index"]) or "none"],
            ["Stocks picked", f"{len(picks)} of 12 slots" + (f"; {12 - len(picks)} slots in {C.ETF_SYMBOL}" if etf else "")],
            ["Holdings to sell", ", ".join(sells["symbol"]) if len(sells) else "none"],
            ["Minimum capital", E.min_capital_text(P) or "–"]]
    story.append(tbl(["Item", "Result"], summ, [0.22, 0.78]))

    # 2 sectors
    story += [Paragraph("2. Sectors - why these were chosen", h2)]
    ch = _sector_chart(S)
    if ch is not None:
        story.append(Image(ch, width=W * 0.72, height=W * 0.72 * 5.4 / 7))
    rows = []
    for _, r in S.iterrows():
        if r.get("group") == "Context":
            continue
        if r.get("selected"):
            why = "SELECTED"
        elif r.get("eligible"):
            why = "passed all tests, but not the best in its group / not in top 4"
        else:
            f = []
            if not r.get("rrg_ok", False):
                f.append(f"RRG {r.get('quadrant', '?')}")
            if not r.get("above_trend", False):
                f.append("below 10-month average")
            if pd.notna(r.get("breadth")) and r.get("breadth", 1) < C.BREADTH_MIN:
                f.append(f"breadth {_f(100 * r['breadth'], 0)}%")
            why = "failed: " + ", ".join(f) if f else str(r.get("status", ""))
        rows.append([str(r["index"]).replace("Nifty ", ""), r.get("quadrant", ""), _f(r.get("rs_ratio")), _f(r.get("rs_mom")),
                     "yes" if r.get("above_trend") else "no", _f(100 * r["breadth"], 0) + "%" if pd.notna(r.get("breadth")) else "–",
                     why + (f" · exit flag: {r['exit_flags']}" if r.get("exit_flags") else "")])
    hl = [(i + 1, colors.HexColor("#e3f4ea")) for i, rr in enumerate(rows) if rr[-1].startswith("SELECTED")]
    story.append(tbl(["Sector", "Quadrant", "RS-Ratio", "RS-Mom", "Trend", "Breadth", "Decision"], rows,
                     [0.15, 0.11, 0.09, 0.09, 0.06, 0.08, 0.42], hl))

    # 3 picks
    from reportlab.platypus import CondPageBreak
    story += [CondPageBreak(W * 0.62), Paragraph("3. Stocks picked - and why", h2)]
    held_syms = set(H["symbol"]) if H is not None and len(H) else set()
    pick_syms = set(picks["symbol"]) if len(picks) else set()
    extra = sorted(set(H["sector_index"].dropna()) - set(sel["index"])) if H is not None and len(H) else []
    for sec in list(sel["index"]) + extra:
        cimg = _stock_chart(T, sec, pick_syms | held_syms)
        if cimg is not None:
            story.append(KeepTogether([Paragraph(f"Where each stock sits vs the {esc(sec)} index", body),
                                       Image(cimg, width=W * 0.72, height=W * 0.72 * 5.4 / 7)]))
    if len(picks):
        rows = []
        for _, p in picks.iterrows():
            r = T[T["symbol"] == p["symbol"]].iloc[0] if len(T) and (T["symbol"] == p["symbol"]).any() else p
            rows.append([p["symbol"], str(p.get("sector_index", "")).replace("Nifty ", ""), _f(p.get("StockScore")),
                         _f(p.get("QUAL"), 0), _f(p.get("MOM"), 0), _f(p.get("VAL"), 0), _f(p.get('weight_%'), 2),
                         _stock_reason(r, S)])
        story.append(tbl(["Stock", "Sector", "Score", "Q", "M", "V", "Wt %", "Reason"], rows,
                         [0.13, 0.11, 0.07, 0.045, 0.045, 0.045, 0.065, 0.49]))
    else:
        story.append(Paragraph("No stock passed every test this month, so all slots are held in the NIFTY 50 ETF.", body))
    if len(T) and "pass_all" in T:
        miss = T[(T["pass_all"] != True) & T["sector_index"].isin(sel["index"])].sort_values("StockScore", ascending=False)
        miss = miss.groupby("sector_index").head(3)
        if len(miss):
            story.append(Paragraph("Near misses (best-scoring stocks that were not bought):", body))
            story.append(tbl(["Stock", "Sector", "Score", "Why not"],
                             [[r["symbol"], str(r["sector_index"]).replace("Nifty ", ""), _f(r.get("StockScore")), _why_not(r)]
                              for _, r in miss.iterrows()], [0.14, 0.16, 0.08, 0.62]))

    # 4 holdings
    story += [Paragraph("4. Your holdings - sell or hold", h2)]
    if H is not None and len(H):
        story.append(tbl(["Stock", "Sector", "Action", "Reason"],
                         [[r["symbol"], str(r.get("sector_index", "")).replace("Nifty ", ""), r["action"], r.get("reasons", "") or "passes every rule"]
                          for _, r in H.iterrows()], [0.14, 0.16, 0.1, 0.6],
                         [(i + 1, colors.HexColor("#fbe5e5")) for i, (_, r) in enumerate(H.iterrows()) if r["action"] == "SELL"]))
    else:
        story.append(Paragraph("No holdings listed in inputs/holdings.csv, so nothing to check.", body))

    # 5 orders
    story += [Paragraph("5. Orders to place (Kite)", h2)]
    od = kite_orders(res, capital, etf) if capital and capital > 0 else pd.DataFrame()
    if len(od):
        story.append(tbl(["#", "Side", "Stock", "Qty", "Limit", "Value", "Why"],
                         [[r["basket"], r["side"], r["symbol"], "" if pd.isna(r["qty"]) else int(r["qty"]),
                           _f(r["limit_price"], 2), f"{RS}{r['value_Rs']:,.0f}" if pd.notna(r["value_Rs"]) else "–", r["reason"]]
                          for _, r in od.iterrows()], [0.04, 0.07, 0.16, 0.07, 0.1, 0.12, 0.44]))
        story.append(Paragraph(f"Orders are {C.KITE_ORDER_TYPE} {C.KITE_PRODUCT} at the last close (rounded to {RS}0.05). "
                               f"Max {C.KITE_BASKET_MAX} per Kite basket. Place SELLs first. The spreadsheet "
                               "'MyFormula_kite_orders' has the same list in Kite's fields.", small))
    else:
        story.append(Paragraph("Enter your capital in the app to get the order list.", body))

    # 6 issues
    I = res.get("issues")
    if I is not None and len(I):
        story += [Paragraph("6. Data notes", h2),
                  tbl(["Item", "Note"], [[r["item"], r["issue"]] for _, r in I.head(40).iterrows()], [0.2, 0.8])]
    story += [Spacer(1, 8), Paragraph("Rules: see My_Formula_Guide.pdf. Decisions use completed monthly bars only.", small)]

    def deco(c, d):
        c.saveState(); c.setFont(FN, 7); c.setFillColor(GREY)
        c.drawString(15 * mm, 8 * mm, "My Formula · research tool, not investment advice")
        c.drawRightString(A4[0] - 15 * mm, 8 * mm, str(d.page)); c.restoreState()

    doc = SimpleDocTemplate(path_or_buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title="My Formula screen report")
    doc.build(story, onFirstPage=deco, onLaterPages=deco)


def export_all(res: dict, capital: float, etf: bool, out_dir: str | None = None) -> list[str]:
    """PDF -> exports/pdf_reports, Kite .xlsx + basket .json -> exports/kite_orders (or all in out_dir).
    Returns the paths."""
    pdf_dir = out_dir or C.EXPORT_PDF_DIR
    kite_dir = out_dir or C.EXPORT_KITE_DIR
    for d in (pdf_dir, kite_dir):
        os.makedirs(d, exist_ok=True)
    ts = f"{res['run_at']:%Y-%m-%d_%H%M}"
    pdf = os.path.join(pdf_dir, f"MyFormula_report_{ts}.pdf")
    xl = os.path.join(kite_dir, f"MyFormula_kite_orders_{ts}.xlsx")
    export_pdf(res, capital, etf, pdf)
    export_orders_xlsx(res, capital, etf, xl)
    paths = [pdf, xl]
    for name, data in basket_json_files(res, capital, etf).items():
        p = os.path.join(kite_dir, name.replace(".json", f"_{ts}.json"))
        with open(p, "wb") as f:
            f.write(data)
        paths.append(p)
    return paths
