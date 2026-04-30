"""
CFSA Price Sync — Dashboard
Run with: python3 -m streamlit run dashboard/app.py
"""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials
import gspread

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
ROOT           = Path(__file__).parent.parent
SA_KEY         = ROOT / "sa-key.json"
SPREADSHEET_ID = "1YRVzl7E48Y8kQ3V6yJbNrzj8QqqH7016tEVQ0IkH9Co"

# Palette — green is used sparingly as ONE accent colour
G      = "#00e87a"          # neon green — hero numbers & active states only
G_20   = "rgba(0,232,122,.20)"
G_10   = "rgba(0,232,122,.10)"
G_06   = "rgba(0,232,122,.06)"
G_03   = "rgba(0,232,122,.03)"
RED    = "#f05c6e"
AMBER  = "#f0a84a"
BLUE   = "#4a9eff"
BG     = "#08090e"          # near-black with a blue-black tint
C1     = "#0e1118"          # card surface
C2     = "#131720"          # card surface raised
BDR    = "rgba(255,255,255,.07)"
BDR_G  = "rgba(0,232,122,.18)"
T1     = "#f0f2f8"          # primary text
T2     = "#8892a4"          # secondary text
T3     = "#4a5260"          # muted text
MONO   = "'JetBrains Mono', 'Fira Code', monospace"

SUPPLIER_LABELS = {
    "flex":                "Flex Adventures",
    "engel":               "Engel",
    "snomaster":           "Snomaster",
    "lite_optec":          "Lite Optec",
    "dag":                 "D.A.G",
    "dometic_frontrunner": "Dometic (Front Runner)",
    "dometic_thrsa":       "Dometic (THRSA)",
    "arb":                 "ARB",
    "coldfactor":          "ColdFactor",
    "highon":              "HighOn",
    "tsunami":             "Tsunami Coolers",
    "frozen":              "Frozen",
}
ACTIVE_SUPPLIERS = {"flex", "engel", "snomaster", "lite_optec", "dag", "dometic_thrsa", "dometic_frontrunner"}

st.set_page_config(
    page_title="CFSA Price Sync",
    page_icon="🧊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────
# CSS — one clean system, no competing effects
# ─────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,300..800;1,14..32,300..800&family=JetBrains+Mono:wght@400;600&display=swap');

/* ── Reset & base ─────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; }}

html, body, [class*="css"], .stApp {{
    background: {BG} !important;
    font-family: 'Inter', system-ui, -apple-system, sans-serif !important;
    color: {T1} !important;
    -webkit-font-smoothing: antialiased;
}}

.block-container {{
    padding: 2.5rem 3rem 4rem !important;
    max-width: 1360px !important;
}}

/* hide streamlit chrome */
#MainMenu, footer, header, .stDeployButton {{ display: none !important; }}

/* scrollbar */
::-webkit-scrollbar {{ width: 5px; height: 5px; background: {BG}; }}
::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,.1); border-radius: 10px; }}

/* ── Top nav ──────────────────────────────── */
.nav {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 28px;
    margin-bottom: 32px;
    border-bottom: 1px solid {BDR};
}}
.nav-brand {{ display: flex; align-items: center; gap: 12px; }}
.nav-icon {{
    width: 38px; height: 38px; border-radius: 9px;
    background: {C2};
    border: 1px solid {BDR};
    display: flex; align-items: center; justify-content: center;
    font-size: 1.15rem;
}}
.nav-name {{
    font-size: 1rem; font-weight: 650; color: {T1};
    letter-spacing: -0.01em;
}}
.nav-sub {{
    font-size: 0.72rem; color: {T3}; margin-top: 1px;
    letter-spacing: 0.04em; text-transform: uppercase;
}}
.nav-right {{ display: flex; align-items: center; gap: 16px; }}
.live-pill {{
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 12px; border-radius: 99px;
    border: 1px solid {BDR_G};
    background: {G_06};
    font-size: 0.7rem; font-weight: 700;
    color: {G}; letter-spacing: 0.1em; text-transform: uppercase;
}}
.pulse {{
    width: 6px; height: 6px; border-radius: 50%;
    background: {G};
    /* glow ONLY on this tiny dot — deliberate, contained */
    box-shadow: 0 0 0 2px {G_20};
    animation: beat 2.2s ease-in-out infinite;
}}
@keyframes beat {{
    0%, 100% {{ box-shadow: 0 0 0 2px {G_20}; }}
    50% {{ box-shadow: 0 0 0 4px rgba(0,232,122,.08); }}
}}
.nav-time {{ font-size: 0.75rem; color: {T3}; text-align: right; line-height: 1.5; }}

/* ── KPI grid ─────────────────────────────── */
.kpi-row {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 14px;
    margin-bottom: 36px;
}}
.kpi {{
    position: relative;
    background: {C1};
    border: 1px solid {BDR};
    border-radius: 14px;
    padding: 20px 20px 16px;
    overflow: hidden;
    transition: border-color .25s ease;
}}
/* The subtle glow lives as an absolutely-positioned radial gradient
   behind the number — like a light source deep inside the card.
   It's the ONLY glow in the whole card; nothing else glows. */
.kpi-glow {{
    position: absolute;
    bottom: -24px; left: 50%;
    transform: translateX(-50%);
    width: 80%; height: 80px;
    background: radial-gradient(ellipse at center, {G_20} 0%, transparent 72%);
    filter: blur(16px);
    pointer-events: none;
    z-index: 0;
}}
.kpi-glow-amber {{
    background: radial-gradient(ellipse at center, rgba(240,168,74,.22) 0%, transparent 72%);
}}
.kpi-glow-blue {{
    background: radial-gradient(ellipse at center, rgba(74,158,255,.2) 0%, transparent 72%);
}}
.kpi-glow-red {{
    background: radial-gradient(ellipse at center, rgba(240,92,110,.2) 0%, transparent 72%);
}}
.kpi:hover {{ border-color: {BDR_G}; }}
.kpi-body {{ position: relative; z-index: 1; }}
.kpi-label {{
    font-size: 0.68rem; font-weight: 600;
    color: {T3}; letter-spacing: 0.1em;
    text-transform: uppercase; margin-bottom: 10px;
}}
.kpi-num {{
    font-size: 2.1rem; font-weight: 750;
    font-family: {MONO};
    color: {G}; line-height: 1;
    letter-spacing: -0.03em;
}}
.kpi-num-amber  {{ color: {AMBER}; }}
.kpi-num-blue   {{ color: {BLUE}; }}
.kpi-num-white  {{ color: {T1}; }}
.kpi-sub {{
    font-size: 0.7rem; color: {T3};
    margin-top: 5px;
}}

/* ── Section label ────────────────────────── */
.slabel {{
    font-size: 0.68rem; font-weight: 700;
    color: {T3}; letter-spacing: 0.12em; text-transform: uppercase;
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 16px;
}}
.slabel::after {{
    content: '';
    flex: 1; height: 1px;
    background: {BDR};
}}

/* ── Tabs ─────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {{
    background: {C1} !important;
    border: 1px solid {BDR} !important;
    border-radius: 11px !important;
    padding: 4px !important; gap: 3px !important;
    width: fit-content !important;
}}
.stTabs [data-baseweb="tab"] {{
    background: transparent !important;
    border-radius: 7px !important;
    color: {T3} !important;
    font-size: 0.8rem !important; font-weight: 500 !important;
    padding: 7px 16px !important;
    border: none !important; outline: none !important;
    transition: color .15s, background .15s !important;
    white-space: nowrap !important;
}}
.stTabs [aria-selected="true"] {{
    background: {C2} !important;
    color: {T1} !important; font-weight: 600 !important;
    border: 1px solid {BDR} !important;
}}
.stTabs [data-baseweb="tab-highlight"],
.stTabs [data-baseweb="tab-border"] {{ display: none !important; }}
.stTabs [data-baseweb="tab-panel"] {{ padding-top: 28px !important; }}

/* ── Inputs & selects ─────────────────────── */
.stSelectbox > div > div > div,
.stTextInput > div > div > input {{
    background: {C1} !important;
    border: 1px solid {BDR} !important;
    border-radius: 8px !important;
    color: {T1} !important;
    font-size: 0.83rem !important;
}}
.stSelectbox > div > div:focus-within,
.stTextInput > div > div:focus-within {{
    border-color: {BDR_G} !important;
    box-shadow: 0 0 0 3px {G_06} !important;
}}
label {{ color: {T2} !important; font-size: 0.75rem !important; }}

/* ── Button ───────────────────────────────── */
.stButton > button {{
    background: {C2} !important;
    border: 1px solid {BDR} !important;
    color: {T2} !important; border-radius: 8px !important;
    font-size: 0.8rem !important; font-weight: 500 !important;
    transition: all .2s !important;
}}
.stButton > button:hover {{
    border-color: {BDR_G} !important;
    color: {G} !important;
    background: {G_06} !important;
}}

/* ── Tables ───────────────────────────────── */
[data-testid="stDataFrame"] > div {{
    border: 1px solid {BDR} !important;
    border-radius: 10px !important; overflow: hidden !important;
}}
.stDataFrame {{ border-radius: 10px !important; }}
.stDataFrame thead tr th {{
    background: {C2} !important;
    color: {T3} !important;
    font-size: 0.68rem !important; font-weight: 600 !important;
    text-transform: uppercase !important; letter-spacing: 0.08em !important;
    border-bottom: 1px solid {BDR} !important;
    padding: 10px 12px !important;
}}
.stDataFrame tbody tr td {{
    background: {C1} !important;
    color: {T1} !important; font-size: 0.82rem !important;
    border-bottom: 1px solid rgba(255,255,255,.03) !important;
    padding: 9px 12px !important;
}}
.stDataFrame tbody tr:hover td {{ background: {C2} !important; }}

/* ── Cards (supplier rows, alerts) ───────── */
.card {{
    background: {C1};
    border: 1px solid {BDR};
    border-radius: 11px;
    padding: 16px 20px;
    margin-bottom: 8px;
    transition: border-color .2s;
}}
.card:hover {{ border-color: rgba(255,255,255,.12); }}
.card-active  {{ border-left: 2px solid {G}; padding-left: 18px; }}
.card-inactive {{ opacity: .55; }}
.card-amber {{ border-left: 2px solid {AMBER}; padding-left: 18px; }}
.card-red   {{ border-left: 2px solid {RED}; padding-left: 18px; }}

.row-between {{
    display: flex; align-items: center; justify-content: space-between;
}}
.card-title   {{ font-size: 0.88rem; font-weight: 600; color: {T1}; }}
.card-sub     {{ font-size: 0.73rem; color: {T3}; margin-top: 3px; }}
.card-sub b   {{ color: {T2}; font-weight: 500; }}

.badge {{
    font-size: 0.65rem; font-weight: 700;
    letter-spacing: 0.09em; text-transform: uppercase;
    padding: 3px 9px; border-radius: 99px;
}}
.badge-green {{ background: {G_10}; color: {G}; border: 1px solid rgba(0,232,122,.2); }}
.badge-grey  {{ background: rgba(255,255,255,.04); color: {T3}; border: 1px solid {BDR}; }}

.cov-num {{
    font-size: 1.05rem; font-weight: 700;
    font-family: {MONO}; color: {G};
}}

/* ── Metric widgets ───────────────────────── */
[data-testid="stMetricValue"] {{
    font-family: {MONO} !important;
    color: {T1} !important; font-size: 1.6rem !important;
}}
[data-testid="stMetricLabel"] {{
    color: {T3} !important; font-size: 0.68rem !important;
    text-transform: uppercase; letter-spacing: 0.08em;
}}

/* ── Expander ─────────────────────────────── */
.stExpander {{
    background: {C1} !important;
    border: 1px solid {BDR} !important;
    border-radius: 10px !important;
}}

/* ── Caption / info ───────────────────────── */
.stCaption, small {{ color: {T3} !important; font-size: 0.72rem !important; }}
.stSuccess {{ border-radius: 10px !important; }}
.stInfo    {{ border-radius: 10px !important; }}

/* ── Divider ──────────────────────────────── */
hr {{ border-color: {BDR} !important; }}

/* ── Plotly tooltip ───────────────────────── */
.plotly .hoverlayer .hovertext {{
    background: {C2} !important;
    border: 1px solid {BDR} !important;
    border-radius: 8px !important;
}}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=120, show_spinner=False)
def load_sheets():
    creds = Credentials.from_service_account_file(
        str(SA_KEY),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    def to_df(name):
        try:
            data = sh.worksheet(name).get_all_records()
            return pd.DataFrame(data) if data else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    return {k: to_df(k) for k in ["master", "price_changes", "supplier_log", "error_flags", "new_products"]}

with st.spinner(""):
    try:
        sheets        = load_sheets()
        master        = sheets["master"]
        price_changes = sheets["price_changes"]
        supplier_log  = sheets["supplier_log"]
        error_flags   = sheets["error_flags"]
        new_products  = sheets["new_products"]
        load_error    = None
    except Exception as e:
        load_error = str(e)
        master = price_changes = supplier_log = error_flags = new_products = pd.DataFrame()

if load_error:
    st.error(f"Could not connect to Google Sheets: {load_error}")
    st.stop()


# ─────────────────────────────────────────────────────────────
# Derived metrics
# ─────────────────────────────────────────────────────────────
linked_mask   = master["shopify_variant_id"].astype(str).str.strip().ne("") if not master.empty else pd.Series([], dtype=bool)
linked_count  = int(linked_mask.sum()) if not master.empty else 0
total_count   = len(master)
unlinked      = total_count - linked_count

if not error_flags.empty and "resolved" in error_flags.columns:
    open_ef   = error_flags[error_flags["resolved"].astype(str).str.upper() != "YES"]
else:
    open_ef   = error_flags.copy() if not error_flags.empty else pd.DataFrame()

price_alerts  = (open_ef[open_ef["error_type"] == "price_alert"]
                 if not open_ef.empty and "error_type" in open_ef.columns
                 else pd.DataFrame())
new_count     = len(new_products) if not new_products.empty else 0

last_sync_rel, last_sync_abs = "—", ""
if not supplier_log.empty and "timestamp" in supplier_log.columns:
    try:
        ts   = pd.to_datetime(supplier_log["timestamp"], errors="coerce").dropna()
        if not ts.empty:
            lat  = ts.max().replace(tzinfo=timezone.utc)
            diff = datetime.now(timezone.utc) - lat
            h, m = int(diff.total_seconds()//3600), int((diff.total_seconds()%3600)//60)
            last_sync_rel = f"{h}h {m}m ago" if h else f"{m}m ago"
            last_sync_abs = lat.strftime("%d %b  %H:%M UTC")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Navigation bar
# ─────────────────────────────────────────────────────────────
_, rbtn = st.columns([11, 1])
with rbtn:
    if st.button("↻ Refresh"):
        st.cache_data.clear()
        st.rerun()

st.markdown(f"""
<div class="nav">
  <div class="nav-brand">
    <div class="nav-icon">🧊</div>
    <div>
      <div class="nav-name">CFSA Price Sync</div>
      <div class="nav-sub">campingfridge.co.za</div>
    </div>
  </div>
  <div class="nav-right">
    <div class="live-pill"><div class="pulse"></div>Live</div>
    <div class="nav-time">
      {last_sync_rel}<br>
      <span style="font-size:.68rem;color:{T3}">{last_sync_abs}</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# KPI row — glow only in the ambient layer behind numbers
# ─────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="kpi-row">

  <div class="kpi">
    <div class="kpi-glow"></div>
    <div class="kpi-body">
      <div class="kpi-label">Products Tracked</div>
      <div class="kpi-num">{total_count}</div>
      <div class="kpi-sub">{unlinked} not yet linked</div>
    </div>
  </div>

  <div class="kpi">
    <div class="kpi-glow"></div>
    <div class="kpi-body">
      <div class="kpi-label">Live on Website</div>
      <div class="kpi-num">{linked_count}</div>
      <div class="kpi-sub">prices auto-updating</div>
    </div>
  </div>

  <div class="kpi">
    <div class="kpi-glow kpi-glow-amber"></div>
    <div class="kpi-body">
      <div class="kpi-label">Price Alerts</div>
      <div class="kpi-num kpi-num-amber">{len(price_alerts)}</div>
      <div class="kpi-sub">held for review</div>
    </div>
  </div>

  <div class="kpi">
    <div class="kpi-glow kpi-glow-blue"></div>
    <div class="kpi-body">
      <div class="kpi-label">New Products</div>
      <div class="kpi-num kpi-num-blue">{new_count}</div>
      <div class="kpi-sub">awaiting Shopify listing</div>
    </div>
  </div>

  <div class="kpi">
    <div class="kpi-glow"></div>
    <div class="kpi-body">
      <div class="kpi-label">Last Sync</div>
      <div class="kpi-num" style="font-size:1.15rem;letter-spacing:-.01em">{last_sync_rel}</div>
      <div class="kpi-sub">07:00 &amp; 13:00 SAST daily</div>
    </div>
  </div>

</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────
t1, t2, t3, t4, t5 = st.tabs([
    "Price Changes", "Suppliers", "Alerts", "New Products", "All Products"
])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 1 — Price Changes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with t1:
    if price_changes.empty:
        st.info("No price changes recorded yet.")
    else:
        pc = price_changes.copy()
        pc["date"] = pd.to_datetime(pc["date"], errors="coerce")
        pc = pc.sort_values("date", ascending=False)

        total_ch   = len(pc)
        alerted_ch = (pc["alerted"].astype(str).str.upper() == "YES").sum() if "alerted" in pc.columns else 0
        auto_ch    = total_ch - alerted_ch

        m1, m2, m3 = st.columns(3)
        m1.metric("Total Recorded",      total_ch)
        m2.metric("Auto-Applied",        auto_ch,  help="Price change ≤5% — applied automatically")
        m3.metric("Held for Review",     alerted_ch, help="Price change >5% — awaiting manual approval")

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="slabel">Price change log</div>', unsafe_allow_html=True)

        # Filters
        fa, fb, fc, fd = st.columns([2,2,2,2])
        with fa:
            sup_opts = ["All suppliers"] + sorted(pc["supplier"].dropna().unique().tolist())
            sup_f = st.selectbox("Supplier", sup_opts, key="pc_s",
                                  format_func=lambda x: SUPPLIER_LABELS.get(x, x))
        with fb:
            dir_f = st.selectbox("Direction", ["All", "Price up ↑", "Price down ↓", "Alerts only"], key="pc_d")
        with fc:
            days_f = st.selectbox("Period", [7,14,30,90,365], key="pc_p",
                                   format_func=lambda x: f"Last {x} days")
        with fd:
            srch = st.text_input("Search SKU", placeholder="MD60F …", key="pc_q")

        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_f)
        msk = pc["date"] >= cutoff
        if sup_f != "All suppliers":
            msk &= pc["supplier"] == sup_f
        if dir_f == "Price up ↑":
            msk &= pc["change_amt"].astype(str).apply(
                lambda x: (float(x.replace(",","")) > 0) if x.strip() not in ("","—","−","-") else False)
        elif dir_f == "Price down ↓":
            msk &= pc["change_amt"].astype(str).apply(
                lambda x: (float(x.replace(",","")) < 0) if x.strip() not in ("","—","−","-") else False)
        elif dir_f == "Alerts only":
            msk &= pc["alerted"].astype(str).str.upper() == "YES"
        if srch:
            msk &= pc["sku"].astype(str).str.lower().str.contains(srch.lower())

        filtered = pc[msk].copy()

        if filtered.empty:
            st.markdown(f"""
            <div class="card" style="text-align:center;padding:24px;margin-top:8px">
              <div style="color:{T3};font-size:.83rem">No changes match these filters</div>
            </div>""", unsafe_allow_html=True)
        else:
            # Table
            disp = filtered[["date","supplier","sku","description",
                              "old_price","new_price","change_amt","change_pct","alerted"]].copy()
            disp["date"]     = disp["date"].dt.strftime("%d %b %Y  %H:%M")
            disp["supplier"] = disp["supplier"].map(lambda x: SUPPLIER_LABELS.get(x, x))

            def _row_style(row):
                s = [""] * len(row)
                idx = list(row.index)
                try:
                    amt = float(str(row.get("change_amt","")).replace(",",""))
                    col = RED if amt > 0 else G
                    for f in ["change_amt","change_pct"]:
                        if f in idx:
                            s[idx.index(f)] = f"color:{col};font-weight:600;font-family:JetBrains Mono,monospace"
                except Exception:
                    pass
                if str(row.get("alerted","")).upper() == "YES" and "alerted" in idx:
                    s[idx.index("alerted")] = f"color:{AMBER};font-weight:700"
                return s

            st.dataframe(
                disp.style.apply(_row_style, axis=1)
                    .set_properties(**{"background-color": C1, "color": T1, "font-size":"0.81rem"}),
                use_container_width=True, height=420, hide_index=True,
            )
            st.caption(f"{len(filtered)} changes shown   ·   green = price dropped   ·   red = rose   ·   amber = held for review")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 2 — Suppliers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with t2:
    sup_stats = {}
    if not master.empty:
        for sup, grp in master.groupby("supplier"):
            lk = grp["shopify_variant_id"].astype(str).str.strip().ne("").sum()
            sup_stats[sup] = {"total": len(grp), "linked": int(lk)}

    last_run_sup = {}
    if not supplier_log.empty and "supplier" in supplier_log.columns:
        for sup, grp in supplier_log.groupby("supplier"):
            try:
                ts = pd.to_datetime(grp["timestamp"], errors="coerce").dropna()
                if not ts.empty:
                    last_run_sup[sup] = ts.max().strftime("%d %b  %H:%M")
            except Exception:
                pass

    st.markdown('<div class="slabel">Active — prices auto-updating</div>', unsafe_allow_html=True)
    for sup in sorted(ACTIVE_SUPPLIERS):
        s = sup_stats.get(sup, {"total":0,"linked":0})
        pct = int(s["linked"]/s["total"]*100) if s["total"] else 0
        lr  = last_run_sup.get(sup, "Never")
        st.markdown(f"""
        <div class="card card-active">
          <div class="row-between">
            <div>
              <div class="card-title">{SUPPLIER_LABELS.get(sup,sup)}</div>
              <div class="card-sub">
                <b>{s['linked']}</b> of <b>{s['total']}</b> products linked to website
                &nbsp;·&nbsp; Last sync: <b>{lr}</b>
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:16px">
              <div style="text-align:right">
                <div class="cov-num">{pct}%</div>
                <div style="font-size:.65rem;color:{T3}">coverage</div>
              </div>
              <span class="badge badge-green">Live</span>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown(f"<div style='margin-top:24px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="slabel">Inactive — no pricing feed connected</div>', unsafe_allow_html=True)
    inactive = sorted((set(SUPPLIER_LABELS) | set(sup_stats)) - ACTIVE_SUPPLIERS)
    for sup in inactive:
        s = sup_stats.get(sup, {"total":0,"linked":0})
        if s["total"] == 0: continue
        st.markdown(f"""
        <div class="card card-inactive">
          <div class="row-between">
            <div>
              <div class="card-title">{SUPPLIER_LABELS.get(sup,sup)}</div>
              <div class="card-sub"><b>{s['total']}</b> products &nbsp;·&nbsp; {s['linked']} linked &nbsp;·&nbsp; Needs dealer portal or email feed</div>
            </div>
            <span class="badge badge-grey">Inactive</span>
          </div>
        </div>
        """, unsafe_allow_html=True)

    # Coverage chart
    st.markdown(f"<div style='margin-top:28px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="slabel">Website coverage</div>', unsafe_allow_html=True)
    if sup_stats:
        rows = [{"sup": SUPPLIER_LABELS.get(s,s), "linked": v["linked"],
                 "gap": v["total"]-v["linked"]} for s,v in sup_stats.items() if v["total"]>0]
        cdf = pd.DataFrame(rows).sort_values("linked", ascending=True)
        fig2 = go.Figure()
        fig2.add_bar(y=cdf["sup"], x=cdf["linked"], orientation="h",
                     name="On website", marker_color=G, opacity=.8)
        fig2.add_bar(y=cdf["sup"], x=cdf["gap"],    orientation="h",
                     name="Not linked", marker_color="rgba(255,255,255,.06)", opacity=1)
        fig2.update_layout(
            barmode="stack", height=max(260, len(cdf)*36),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=T3, family="Inter", size=11),
            margin=dict(l=0,r=0,t=4,b=0),
            legend=dict(orientation="h", x=1, xanchor="right", y=1.08,
                        bgcolor="rgba(0,0,0,0)", font_size=11),
            xaxis=dict(gridcolor="rgba(255,255,255,.04)", zeroline=False),
            yaxis=dict(gridcolor="rgba(0,0,0,0)"),
        )
        st.plotly_chart(fig2, use_container_width=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 3 — Alerts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with t3:
    if error_flags.empty or open_ef.empty:
        st.markdown(f"""
        <div class="card" style="text-align:center;padding:32px;border-color:{G_10}">
          <div style="font-size:1.3rem;margin-bottom:8px">✅</div>
          <div style="color:{T2};font-weight:500">All clear — no open alerts</div>
        </div>""", unsafe_allow_html=True)
    else:
        ef_pa  = open_ef[open_ef["error_type"]=="price_alert"] if "error_type" in open_ef.columns else pd.DataFrame()
        ef_oth = open_ef[open_ef["error_type"]!="price_alert"] if "error_type" in open_ef.columns else pd.DataFrame()

        m1, m2 = st.columns(2)
        m1.metric("Price Alerts",  len(ef_pa),  help="Price moved >5% — not yet auto-applied")
        m2.metric("Other Errors",  len(ef_oth))

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="slabel">Open items</div>', unsafe_allow_html=True)

        for _, row in open_ef.iterrows():
            et  = str(row.get("error_type",""))
            sku = str(row.get("sku",""))
            sup = SUPPLIER_LABELS.get(str(row.get("supplier","")), str(row.get("supplier","")))
            det = str(row.get("detail",""))
            ts  = str(row.get("flagged_at",""))[:16]
            accent = AMBER if et=="price_alert" else RED
            icon   = "⚠" if et=="price_alert" else "✕"
            st.markdown(f"""
            <div class="card {'card-amber' if et=='price_alert' else 'card-red'}">
              <div class="row-between">
                <div>
                  <div class="card-title" style="display:flex;align-items:center;gap:8px">
                    <span style="color:{accent}">{icon}</span>
                    <span style="font-family:{MONO};font-size:.82rem">{sku}</span>
                    <span style="color:{T3};font-weight:400;font-size:.8rem">— {sup}</span>
                  </div>
                  <div class="card-sub" style="margin-top:5px">{det}</div>
                  <div class="card-sub" style="margin-top:3px;opacity:.5">{ts} · {et}</div>
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown(f"<div style='margin-top:12px;font-size:.73rem;color:{T3}'>To resolve: check the price in Shopify, then set <code>resolved = Yes</code> in the error_flags sheet tab.</div>", unsafe_allow_html=True)

    if not error_flags.empty and "resolved" in error_flags.columns:
        resolved = error_flags[error_flags["resolved"].astype(str).str.upper()=="YES"]
        if not resolved.empty:
            with st.expander(f"✅  {len(resolved)} resolved"):
                cols = [c for c in ["flagged_at","supplier","sku","error_type","detail"] if c in resolved.columns]
                st.dataframe(resolved[cols], use_container_width=True, hide_index=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 4 — New Products
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with t4:
    st.markdown('<div class="slabel">Not yet on the website</div>', unsafe_allow_html=True)
    st.markdown(f"<p style='color:{T3};font-size:.82rem;margin-bottom:20px'>These came through from active suppliers but have no Shopify listing. Forward to Ricky to decide which to add.</p>", unsafe_allow_html=True)

    if new_products.empty:
        st.markdown(f"""
        <div class="card" style="text-align:center;padding:28px">
          <div style="color:{T3}">No new products waiting for review</div>
        </div>""", unsafe_allow_html=True)
    else:
        np_df = new_products.copy()
        if "supplier" in np_df.columns:
            np_df["_label"] = np_df["supplier"].map(lambda x: SUPPLIER_LABELS.get(x,x))
            for label, grp in np_df.groupby("_label"):
                with st.expander(f"**{label}** — {len(grp)} product(s)", expanded=True):
                    cols = [c for c in ["sku","description","selling_price","stock_status","date_found"] if c in grp.columns]
                    st.dataframe(grp[cols].reset_index(drop=True), use_container_width=True, hide_index=True)
        else:
            st.dataframe(np_df, use_container_width=True, hide_index=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 5 — All Products
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with t5:
    st.markdown('<div class="slabel">Master product list</div>', unsafe_allow_html=True)

    if master.empty:
        st.info("No products loaded yet.")
    else:
        m = master.copy()
        m["_sup"]  = m["supplier"].map(lambda x: SUPPLIER_LABELS.get(x,x))
        m["_site"] = m["shopify_variant_id"].astype(str).str.strip().ne("").map({True:"✓ Yes", False:"✗ No"})

        fa, fb, fc = st.columns([2,2,3])
        with fa:
            sopts = ["All"] + sorted(m["_sup"].dropna().unique())
            sf = st.selectbox("Supplier", sopts, key="ms")
        with fb:
            wf = st.selectbox("On website", ["All","✓ Yes","✗ No"], key="mw")
        with fc:
            sq = st.text_input("Search SKU / description", placeholder="Engel 60L …", key="mq")

        msk = pd.Series([True]*len(m), index=m.index)
        if sf  != "All":          msk &= m["_sup"]  == sf
        if wf  == "✓ Yes":        msk &= m["_site"] == "✓ Yes"
        elif wf == "✗ No":        msk &= m["_site"] == "✗ No"
        if sq:
            sl = sq.lower()
            msk &= m["sku"].astype(str).str.lower().str.contains(sl) | m["description"].astype(str).str.lower().str.contains(sl)

        fm = m[msk]
        show = [c for c in ["sku","_sup","description","selling_price","rrp","stock_status","_site","last_updated"] if c in fm.columns]
        dm   = fm[show].rename(columns={"_sup":"supplier","_site":"on website"})

        def _ms_style(row):
            s = [""]*len(row); idx = list(row.index)
            if "stock_status" in idx:
                v = str(row["stock_status"])
                c = G if "In Stock" in v else (RED if "Out" in v else T3)
                s[idx.index("stock_status")] = f"color:{c}"
            if "on website" in idx:
                v = str(row["on website"])
                s[idx.index("on website")] = f"color:{G}" if "Yes" in v else f"color:{T3}"
            return s

        st.caption(f"{len(fm)} of {len(m)} products")
        st.dataframe(
            dm.style.apply(_ms_style, axis=1).set_properties(**{"background-color":C1,"color":T1,"font-size":"0.81rem"}),
            use_container_width=True, height=520, hide_index=True,
        )


# ─────────────────────────────────────────────────────────────
# Footer
# ─────────────────────────────────────────────────────────────
st.markdown(f"""
<hr>
<div style="display:flex;justify-content:space-between;padding-top:4px">
  <span style="color:{T3};font-size:.7rem">CFSA Price Sync · Streamline Digital</span>
  <span style="color:{T3};font-size:.7rem">{datetime.now(timezone.utc).strftime("%d %b %Y  %H:%M UTC")}</span>
</div>
""", unsafe_allow_html=True)
