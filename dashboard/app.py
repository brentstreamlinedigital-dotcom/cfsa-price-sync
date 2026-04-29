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

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
ROOT          = Path(__file__).parent.parent
SA_KEY        = ROOT / "sa-key.json"
SPREADSHEET_ID = "1YRVzl7E48Y8kQ3V6yJbNrzj8QqqH7016tEVQ0IkH9Co"

NEON       = "#00ff88"
NEON_DIM   = "#00cc6a"
NEON_GLOW  = "rgba(0,255,136,0.15)"
RED        = "#ff4d6d"
AMBER      = "#ffb830"
BLUE       = "#38bdf8"
BG         = "#060810"
CARD       = "#0c0f1a"
CARD2      = "#111526"
BORDER     = "#1a1f35"
TEXT       = "#e2e8f0"
MUTED      = "#4a5270"

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
ACTIVE_SUPPLIERS = {"flex", "engel", "snomaster", "lite_optec"}

st.set_page_config(
    page_title="CFSA Price Sync",
    page_icon="🧊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────
# Global CSS
# ─────────────────────────────────────────────
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap');

/* ── Base ── */
html, body, .stApp {{
    background-color: {BG} !important;
    font-family: 'Inter', sans-serif;
    color: {TEXT};
}}
.stApp > header {{ display: none; }}
.block-container {{ padding: 2rem 2.5rem 3rem !important; max-width: 1400px; }}

/* ── Hide streamlit chrome ── */
#MainMenu, footer, .stDeployButton {{ display: none !important; }}

/* ── Scrollbar ── */
::-webkit-scrollbar {{ width: 6px; height: 6px; }}
::-webkit-scrollbar-track {{ background: {BG}; }}
::-webkit-scrollbar-thumb {{ background: {BORDER}; border-radius: 3px; }}
::-webkit-scrollbar-thumb:hover {{ background: {NEON_DIM}; }}

/* ── Top bar ── */
.topbar {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 0 28px 0;
    border-bottom: 1px solid {BORDER};
    margin-bottom: 32px;
}}
.topbar-left {{ display: flex; align-items: center; gap: 14px; }}
.topbar-logo {{
    width: 44px; height: 44px; border-radius: 10px;
    background: linear-gradient(135deg, {NEON_GLOW}, transparent);
    border: 1px solid {NEON_DIM};
    display: flex; align-items: center; justify-content: center;
    font-size: 1.4rem;
    box-shadow: 0 0 20px {NEON_GLOW};
}}
.topbar-title {{ font-size: 1.5rem; font-weight: 700; color: {TEXT}; letter-spacing: -0.02em; }}
.topbar-sub {{ font-size: 0.78rem; color: {MUTED}; margin-top: 2px; letter-spacing: 0.03em; text-transform: uppercase; }}
.topbar-right {{ display: flex; align-items: center; gap: 12px; }}
.live-badge {{
    display: flex; align-items: center; gap: 7px;
    background: rgba(0,255,136,0.06); border: 1px solid rgba(0,255,136,0.25);
    border-radius: 20px; padding: 5px 14px;
    font-size: 0.78rem; font-weight: 600; color: {NEON};
    letter-spacing: 0.05em;
}}
.live-dot {{
    width: 7px; height: 7px; border-radius: 50%;
    background: {NEON};
    box-shadow: 0 0 8px {NEON};
    animation: pulse 2s infinite;
}}
@keyframes pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: 0.5; transform: scale(0.8); }}
}}
.sync-time {{ font-size: 0.78rem; color: {MUTED}; }}

/* ── KPI cards ── */
.kpi-grid {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 16px;
    margin-bottom: 32px;
}}
.kpi-card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 14px;
    padding: 22px 20px 18px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
}}
.kpi-card::before {{
    content: '';
    position: absolute; top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--accent, {NEON}), transparent);
    opacity: 0.6;
}}
.kpi-card:hover {{ border-color: var(--accent, {NEON}); }}
.kpi-card.accent-green {{ --accent: {NEON}; }}
.kpi-card.accent-red   {{ --accent: {RED}; }}
.kpi-card.accent-amber {{ --accent: {AMBER}; }}
.kpi-card.accent-blue  {{ --accent: {BLUE}; }}
.kpi-icon {{
    font-size: 1.1rem; margin-bottom: 10px;
    width: 34px; height: 34px;
    background: rgba(0,255,136,0.06);
    border-radius: 8px; display: flex; align-items: center; justify-content: center;
}}
.kpi-value {{
    font-size: 2rem; font-weight: 800;
    color: var(--accent, {NEON});
    font-family: 'JetBrains Mono', monospace;
    letter-spacing: -0.02em;
    line-height: 1;
    text-shadow: 0 0 20px var(--accent, {NEON_GLOW});
}}
.kpi-label {{
    font-size: 0.75rem; color: {MUTED};
    margin-top: 6px; text-transform: uppercase;
    letter-spacing: 0.06em; font-weight: 500;
}}
.kpi-sublabel {{
    font-size: 0.7rem; color: {MUTED};
    margin-top: 3px; opacity: 0.6;
}}

/* ── Section header ── */
.section-label {{
    font-size: 0.7rem; font-weight: 700;
    color: {NEON}; letter-spacing: 0.12em;
    text-transform: uppercase; margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px;
}}
.section-label::after {{
    content: ''; flex: 1;
    height: 1px;
    background: linear-gradient(90deg, {BORDER}, transparent);
}}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {{
    background: {CARD} !important;
    border: 1px solid {BORDER} !important;
    border-radius: 12px !important;
    padding: 5px !important;
    gap: 4px !important;
}}
.stTabs [data-baseweb="tab"] {{
    background: transparent !important;
    border-radius: 8px !important;
    color: {MUTED} !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    padding: 8px 18px !important;
    border: none !important;
    transition: all 0.2s !important;
}}
.stTabs [aria-selected="true"] {{
    background: rgba(0,255,136,0.1) !important;
    color: {NEON} !important;
    font-weight: 600 !important;
    box-shadow: inset 0 0 0 1px rgba(0,255,136,0.25) !important;
}}
.stTabs [data-baseweb="tab-highlight"] {{ display: none !important; }}
.stTabs [data-baseweb="tab-border"] {{ display: none !important; }}
.stTabs [data-baseweb="tab-panel"] {{ padding-top: 24px !important; }}

/* ── Tables / dataframes ── */
.stDataFrame {{ border-radius: 10px !important; overflow: hidden; }}
[data-testid="stDataFrame"] > div {{
    border: 1px solid {BORDER} !important;
    border-radius: 10px !important;
}}
.stDataFrame table {{ background: {CARD} !important; }}
.stDataFrame th {{
    background: {CARD2} !important;
    color: {MUTED} !important;
    font-size: 0.72rem !important;
    text-transform: uppercase !important;
    letter-spacing: 0.06em !important;
    border-bottom: 1px solid {BORDER} !important;
}}
.stDataFrame td {{
    color: {TEXT} !important;
    font-size: 0.83rem !important;
    border-bottom: 1px solid rgba(26,31,53,0.5) !important;
}}

/* ── Selectbox / inputs ── */
.stSelectbox > div > div, .stTextInput > div > div {{
    background: {CARD} !important;
    border: 1px solid {BORDER} !important;
    border-radius: 8px !important;
    color: {TEXT} !important;
}}
.stSelectbox > div > div:focus-within, .stTextInput > div > div:focus-within {{
    border-color: {NEON} !important;
    box-shadow: 0 0 0 2px rgba(0,255,136,0.1) !important;
}}

/* ── Buttons ── */
.stButton > button {{
    background: rgba(0,255,136,0.06) !important;
    border: 1px solid rgba(0,255,136,0.3) !important;
    color: {NEON} !important;
    border-radius: 8px !important;
    font-size: 0.82rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.02em !important;
    transition: all 0.2s !important;
}}
.stButton > button:hover {{
    background: rgba(0,255,136,0.12) !important;
    border-color: {NEON} !important;
    box-shadow: 0 0 16px rgba(0,255,136,0.15) !important;
}}

/* ── Expanders ── */
.stExpander {{
    background: {CARD} !important;
    border: 1px solid {BORDER} !important;
    border-radius: 10px !important;
}}
.stExpander > div > div > div > summary {{
    color: {TEXT} !important;
    font-weight: 500 !important;
    font-size: 0.88rem !important;
}}

/* ── Status / info boxes ── */
.stInfo, .stSuccess {{ border-radius: 10px !important; }}

/* ── Supplier row cards ── */
.sup-row {{
    display: flex; align-items: center; justify-content: space-between;
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 14px 18px;
    margin-bottom: 8px;
    transition: border-color 0.2s;
}}
.sup-row:hover {{ border-color: rgba(0,255,136,0.2); }}
.sup-row-active {{ border-left: 3px solid {NEON}; }}
.sup-row-inactive {{ border-left: 3px solid {BORDER}; opacity: 0.65; }}
.sup-name {{ font-size: 0.9rem; font-weight: 600; color: {TEXT}; }}
.sup-meta {{ font-size: 0.75rem; color: {MUTED}; margin-top: 3px; }}
.sup-badge {{
    font-size: 0.68rem; font-weight: 700; letter-spacing: 0.08em;
    padding: 3px 10px; border-radius: 20px; text-transform: uppercase;
}}
.badge-live {{ background: rgba(0,255,136,0.1); color: {NEON}; border: 1px solid rgba(0,255,136,0.25); }}
.badge-off  {{ background: rgba(74,82,112,0.2); color: {MUTED}; border: 1px solid {BORDER}; }}

/* ── Alert rows ── */
.alert-row {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-left: 3px solid {AMBER};
    border-radius: 10px;
    padding: 14px 18px;
    margin-bottom: 8px;
    display: flex; align-items: flex-start; gap: 14px;
}}
.alert-icon {{ font-size: 1.2rem; margin-top: 1px; }}
.alert-title {{ font-size: 0.88rem; font-weight: 600; color: {TEXT}; }}
.alert-meta {{ font-size: 0.75rem; color: {MUTED}; margin-top: 3px; }}
.alert-price {{ font-family: 'JetBrains Mono', monospace; font-size: 0.82rem; }}

/* ── Price change rows ── */
.change-up   {{ color: {RED}; font-weight: 600; }}
.change-down {{ color: {NEON}; font-weight: 600; }}

/* ── Divider ── */
hr {{ border-color: {BORDER} !important; margin: 24px 0 !important; }}

/* ── Caption ── */
.stCaption {{ color: {MUTED} !important; font-size: 0.73rem !important; }}

/* ── Metric ── */
[data-testid="stMetricValue"] {{ color: {NEON} !important; font-family: 'JetBrains Mono', monospace !important; }}
[data-testid="stMetricLabel"] {{ color: {MUTED} !important; font-size: 0.75rem !important; text-transform: uppercase; letter-spacing: 0.05em; }}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────
@st.cache_data(ttl=120, show_spinner=False)
def load_sheets():
    creds = Credentials.from_service_account_file(
        str(SA_KEY),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    def sheet_df(name):
        try:
            ws = sh.worksheet(name)
            data = ws.get_all_records()
            return pd.DataFrame(data) if data else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    return {
        "master":        sheet_df("master"),
        "price_changes": sheet_df("price_changes"),
        "supplier_log":  sheet_df("supplier_log"),
        "error_flags":   sheet_df("error_flags"),
        "new_products":  sheet_df("new_products"),
    }

with st.spinner(""):
    try:
        sheets     = load_sheets()
        master        = sheets["master"]
        price_changes = sheets["price_changes"]
        supplier_log  = sheets["supplier_log"]
        error_flags   = sheets["error_flags"]
        new_products  = sheets["new_products"]
        load_error = None
    except Exception as e:
        load_error = str(e)
        master = price_changes = supplier_log = error_flags = new_products = pd.DataFrame()


# ─────────────────────────────────────────────
# Derived metrics
# ─────────────────────────────────────────────
linked = master[master["shopify_variant_id"].astype(str).str.strip().ne("")] if not master.empty else pd.DataFrame()

if not error_flags.empty and "resolved" in error_flags.columns:
    open_alerts = error_flags[error_flags["resolved"].astype(str).str.upper() != "YES"]
else:
    open_alerts = error_flags.copy() if not error_flags.empty else pd.DataFrame()

price_alert_count = (
    len(open_alerts[open_alerts["error_type"] == "price_alert"])
    if not open_alerts.empty and "error_type" in open_alerts.columns else 0
)
new_prod_count = len(new_products) if not new_products.empty else 0

last_sync_str = "—"
last_sync_full = ""
if not supplier_log.empty and "timestamp" in supplier_log.columns:
    try:
        ts = pd.to_datetime(supplier_log["timestamp"], errors="coerce").dropna()
        if not ts.empty:
            latest = ts.max().replace(tzinfo=timezone.utc)
            diff   = datetime.now(timezone.utc) - latest
            hrs    = int(diff.total_seconds() // 3600)
            mins   = int((diff.total_seconds() % 3600) // 60)
            last_sync_str  = f"{hrs}h {mins}m ago" if hrs > 0 else f"{mins}m ago"
            last_sync_full = latest.strftime("%d %b %Y · %H:%M UTC")
    except Exception:
        pass


# ─────────────────────────────────────────────
# Top bar
# ─────────────────────────────────────────────
if load_error:
    st.error(f"Could not connect to Google Sheets: {load_error}")
    st.stop()

_, btn_col = st.columns([10, 1])
with btn_col:
    if st.button("⟳ Refresh"):
        st.cache_data.clear()
        st.rerun()

st.markdown(f"""
<div class="topbar">
  <div class="topbar-left">
    <div class="topbar-logo">🧊</div>
    <div>
      <div class="topbar-title">CFSA Price Sync</div>
      <div class="topbar-sub">campingfridge.co.za · automated pricing</div>
    </div>
  </div>
  <div class="topbar-right">
    <div class="live-badge">
      <div class="live-dot"></div>
      LIVE
    </div>
    <div class="sync-time">Last sync: {last_sync_str}<br><span style="opacity:0.5;font-size:0.68rem">{last_sync_full}</span></div>
  </div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# KPI row
# ─────────────────────────────────────────────
total_products = len(master) if not master.empty else 0
linked_count   = len(linked)
unlinked_count = total_products - linked_count

st.markdown(f"""
<div class="kpi-grid">
  <div class="kpi-card accent-green">
    <div class="kpi-icon">📦</div>
    <div class="kpi-value" style="--accent:{NEON}">{total_products}</div>
    <div class="kpi-label">Products Tracked</div>
    <div class="kpi-sublabel">across all suppliers</div>
  </div>
  <div class="kpi-card accent-green">
    <div class="kpi-icon">🔗</div>
    <div class="kpi-value" style="--accent:{NEON}">{linked_count}</div>
    <div class="kpi-label">Live on Website</div>
    <div class="kpi-sublabel">{unlinked_count} not yet linked</div>
  </div>
  <div class="kpi-card accent-amber">
    <div class="kpi-icon">⚠️</div>
    <div class="kpi-value" style="--accent:{AMBER}">{price_alert_count}</div>
    <div class="kpi-label">Price Alerts</div>
    <div class="kpi-sublabel">held for review</div>
  </div>
  <div class="kpi-card accent-blue">
    <div class="kpi-icon">🆕</div>
    <div class="kpi-value" style="--accent:{BLUE}">{new_prod_count}</div>
    <div class="kpi-label">New Products</div>
    <div class="kpi-sublabel">awaiting review</div>
  </div>
  <div class="kpi-card accent-green">
    <div class="kpi-icon">⚡</div>
    <div class="kpi-value" style="--accent:{NEON};font-size:1.3rem">{last_sync_str}</div>
    <div class="kpi-label">Last Sync</div>
    <div class="kpi-sublabel">runs 07:00 & 13:00 SAST</div>
  </div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────
tab_changes, tab_suppliers, tab_alerts, tab_new, tab_master = st.tabs([
    "📈  Price Changes",
    "🔌  Suppliers",
    "⚠️  Alerts",
    "🆕  New Products",
    "📦  All Products",
])


# ══════════════════════════════════════════════
# TAB 1 — Price Changes
# ══════════════════════════════════════════════
with tab_changes:
    st.markdown('<div class="section-label">Price Change History</div>', unsafe_allow_html=True)

    if price_changes.empty:
        st.info("No price changes recorded yet. They'll appear here after the first sync detects a change.")
    else:
        pc = price_changes.copy()
        pc["date"] = pd.to_datetime(pc["date"], errors="coerce")
        pc = pc.sort_values("date", ascending=False)

        # Summary strip
        total_changes = len(pc)
        alerted_count = (pc["alerted"].astype(str).str.upper() == "YES").sum() if "alerted" in pc.columns else 0
        auto_applied  = total_changes - alerted_count
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Changes Recorded", total_changes)
        c2.metric("Auto-Applied to Website", auto_applied)
        c3.metric("Held for Review", alerted_count)

        st.markdown("<br>", unsafe_allow_html=True)

        # Filters
        f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
        with f1:
            sup_opts = ["All"] + sorted(pc["supplier"].dropna().unique().tolist())
            sup_f = st.selectbox("Supplier", sup_opts, key="pc_sup",
                                  format_func=lambda x: SUPPLIER_LABELS.get(x, x) if x != "All" else "All Suppliers")
        with f2:
            dir_f = st.selectbox("Direction", ["All", "Price Up ↑", "Price Down ↓", "Alerts only"], key="pc_dir")
        with f3:
            days_f = st.selectbox("Period", [7, 14, 30, 90, 365], key="pc_days",
                                   format_func=lambda x: f"Last {x} days")
        with f4:
            search_f = st.text_input("Search SKU", placeholder="e.g. MD60F", key="pc_search")

        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_f)
        mask = pc["date"] >= cutoff
        if sup_f != "All":
            mask &= pc["supplier"] == sup_f
        if dir_f == "Price Up ↑":
            mask &= pc["change_amt"].astype(str).apply(
                lambda x: float(x.replace(",","")) > 0 if x.strip() not in ("","—","-") else False
            )
        elif dir_f == "Price Down ↓":
            mask &= pc["change_amt"].astype(str).apply(
                lambda x: float(x.replace(",","")) < 0 if x.strip() not in ("","—","-") else False
            )
        elif dir_f == "Alerts only":
            mask &= pc["alerted"].astype(str).str.upper() == "YES"
        if search_f:
            mask &= pc["sku"].astype(str).str.lower().str.contains(search_f.lower())

        filtered = pc[mask].copy()

        if filtered.empty:
            st.info("No changes match these filters.")
        else:
            # Chart
            chart = filtered.copy()
            chart["day"] = chart["date"].dt.date
            daily = chart.groupby(["day","alerted"]).size().reset_index(name="n")

            fig = go.Figure()
            for alerted_val, colour, label in [("NO", NEON, "Auto-applied"), ("YES", AMBER, "Held for review")]:
                d = daily[daily["alerted"].astype(str).str.upper() == alerted_val]
                if not d.empty:
                    fig.add_bar(x=d["day"], y=d["n"], name=label,
                                marker_color=colour, opacity=0.85)

            fig.update_layout(
                barmode="stack", height=160,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=MUTED, family="Inter", size=11),
                margin=dict(l=0, r=0, t=8, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1, xanchor="right", x=1,
                            font_size=11, bgcolor="rgba(0,0,0,0)"),
                xaxis=dict(gridcolor=BORDER, zeroline=False),
                yaxis=dict(gridcolor=BORDER, zeroline=False),
            )
            st.plotly_chart(fig, use_container_width=True)

            # Table
            disp = filtered[["date","supplier","sku","description","old_price","new_price","change_amt","change_pct","alerted"]].copy()
            disp["date"] = disp["date"].dt.strftime("%d %b %Y  %H:%M")
            disp["supplier"] = disp["supplier"].map(lambda x: SUPPLIER_LABELS.get(x, x))

            def _style_row(row):
                styles = [""] * len(row)
                cols = list(row.index)
                try:
                    amt = float(str(row["change_amt"]).replace(",",""))
                    colour = RED if amt > 0 else NEON
                    for field in ["change_amt","change_pct"]:
                        if field in cols:
                            styles[cols.index(field)] = f"color:{colour};font-weight:600;font-family:JetBrains Mono,monospace"
                except Exception:
                    pass
                if str(row.get("alerted","")).upper() == "YES":
                    if "alerted" in cols:
                        styles[cols.index("alerted")] = f"color:{AMBER};font-weight:700"
                return styles

            styled = disp.style.apply(_style_row, axis=1)\
                .set_properties(**{"background-color": CARD, "color": TEXT, "font-size": "0.82rem"})
            st.dataframe(styled, use_container_width=True, height=420, hide_index=True)
            st.caption(f"Showing {len(filtered)} of {len(pc)} total changes   ·   "
                       f"🟢 green = dropped  🔴 red = rose  🟡 amber = held for review")


# ══════════════════════════════════════════════
# TAB 2 — Suppliers
# ══════════════════════════════════════════════
with tab_suppliers:
    st.markdown('<div class="section-label">Supplier Connection Status</div>', unsafe_allow_html=True)

    # Stats from master
    supplier_stats = {}
    if not master.empty:
        for sup, grp in master.groupby("supplier"):
            linked_n = grp["shopify_variant_id"].astype(str).str.strip().ne("").sum()
            supplier_stats[sup] = {"total": len(grp), "linked": int(linked_n)}

    last_run_by_sup = {}
    if not supplier_log.empty and "supplier" in supplier_log.columns:
        for sup, grp in supplier_log.groupby("supplier"):
            try:
                ts = pd.to_datetime(grp["timestamp"], errors="coerce").dropna()
                if not ts.empty:
                    last_run_by_sup[sup] = ts.max().strftime("%d %b · %H:%M")
            except Exception:
                pass

    # Live suppliers
    st.markdown(f"<div style='font-size:0.75rem;color:{NEON};font-weight:600;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:10px'>⚡ Live — Auto-updating</div>", unsafe_allow_html=True)
    for sup in sorted(ACTIVE_SUPPLIERS):
        stats   = supplier_stats.get(sup, {"total": 0, "linked": 0})
        last    = last_run_by_sup.get(sup, "Never")
        label   = SUPPLIER_LABELS.get(sup, sup)
        pct     = int(stats["linked"] / stats["total"] * 100) if stats["total"] else 0
        st.markdown(f"""
        <div class="sup-row sup-row-active">
          <div>
            <div class="sup-name">{label}</div>
            <div class="sup-meta">{stats['linked']} / {stats['total']} products linked to website · Last sync: {last}</div>
          </div>
          <div style="display:flex;align-items:center;gap:14px">
            <div style="text-align:right">
              <div style="font-size:1.1rem;font-weight:700;font-family:JetBrains Mono,monospace;color:{NEON}">{pct}%</div>
              <div style="font-size:0.68rem;color:{MUTED}">coverage</div>
            </div>
            <span class="sup-badge badge-live">Live</span>
          </div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown(f"<div style='font-size:0.75rem;color:{MUTED};font-weight:600;letter-spacing:0.08em;text-transform:uppercase;margin:20px 0 10px'>⚫ Inactive — Awaiting connection</div>", unsafe_allow_html=True)
    all_sups = set(SUPPLIER_LABELS.keys()) | set(supplier_stats.keys())
    for sup in sorted(all_sups - ACTIVE_SUPPLIERS):
        stats = supplier_stats.get(sup, {"total": 0, "linked": 0})
        label = SUPPLIER_LABELS.get(sup, sup)
        if stats["total"] == 0:
            continue
        st.markdown(f"""
        <div class="sup-row sup-row-inactive">
          <div>
            <div class="sup-name">{label}</div>
            <div class="sup-meta">{stats['total']} products tracked · {stats['linked']} linked · No pricing feed connected</div>
          </div>
          <span class="sup-badge badge-off">Inactive</span>
        </div>
        """, unsafe_allow_html=True)

    # Coverage chart
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-label">Website Coverage by Supplier</div>', unsafe_allow_html=True)

    if not master.empty:
        chart_sups = []
        for sup, stats in supplier_stats.items():
            if stats["total"] > 0:
                chart_sups.append({
                    "label":    SUPPLIER_LABELS.get(sup, sup),
                    "linked":   stats["linked"],
                    "unlinked": stats["total"] - stats["linked"],
                    "active":   sup in ACTIVE_SUPPLIERS,
                })
        csdf = pd.DataFrame(chart_sups).sort_values("linked", ascending=True)

        fig = go.Figure()
        fig.add_bar(y=csdf["label"], x=csdf["linked"],   orientation="h", name="On website",
                    marker_color=NEON, opacity=0.85)
        fig.add_bar(y=csdf["label"], x=csdf["unlinked"], orientation="h", name="Not linked",
                    marker_color=BORDER, opacity=0.7)
        fig.update_layout(
            barmode="stack", height=max(280, len(csdf) * 34),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=MUTED, family="Inter", size=11),
            margin=dict(l=0, r=0, t=8, b=0),
            xaxis=dict(gridcolor=BORDER, zeroline=False),
            yaxis=dict(gridcolor="rgba(0,0,0,0)"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, bgcolor="rgba(0,0,0,0)"),
        )
        st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════
# TAB 3 — Alerts
# ══════════════════════════════════════════════
with tab_alerts:
    st.markdown('<div class="section-label">Price Alerts & Errors</div>', unsafe_allow_html=True)

    if error_flags.empty:
        st.markdown(f"""
        <div style="background:{CARD};border:1px solid rgba(0,255,136,0.2);border-radius:12px;
                    padding:28px;text-align:center;color:{NEON}">
            ✅&nbsp;&nbsp;All clear — no alerts
        </div>
        """, unsafe_allow_html=True)
    else:
        ef = error_flags.copy()
        if "resolved" in ef.columns:
            open_ef   = ef[ef["resolved"].astype(str).str.upper() != "YES"]
            closed_ef = ef[ef["resolved"].astype(str).str.upper() == "YES"]
        else:
            open_ef   = ef.copy()
            closed_ef = pd.DataFrame()

        price_al = open_ef[open_ef["error_type"] == "price_alert"] if "error_type" in open_ef.columns else pd.DataFrame()
        other_al  = open_ef[open_ef["error_type"] != "price_alert"] if "error_type" in open_ef.columns else pd.DataFrame()

        c1, c2, c3 = st.columns(3)
        c1.metric("Open Alerts", len(open_ef))
        c2.metric("Price Alerts", len(price_al), help=">5% price move — held for manual review")
        c3.metric("Other Errors", len(other_al))

        st.markdown("<br>", unsafe_allow_html=True)

        if not open_ef.empty:
            for _, row in open_ef.iterrows():
                etype   = str(row.get("error_type",""))
                sku     = str(row.get("sku",""))
                sup     = SUPPLIER_LABELS.get(str(row.get("supplier","")), str(row.get("supplier","")))
                detail  = str(row.get("detail",""))
                flagged = str(row.get("flagged_at",""))[:16]
                icon    = "⚠️" if etype == "price_alert" else "❌"
                accent  = AMBER if etype == "price_alert" else RED
                st.markdown(f"""
                <div class="alert-row" style="border-left-color:{accent}">
                  <div class="alert-icon">{icon}</div>
                  <div style="flex:1">
                    <div class="alert-title">{sup} &mdash; <span style="font-family:JetBrains Mono,monospace;font-size:0.85rem">{sku}</span></div>
                    <div class="alert-meta">{detail}</div>
                    <div class="alert-meta" style="margin-top:4px;opacity:0.5">{flagged} · {etype}</div>
                  </div>
                </div>
                """, unsafe_allow_html=True)

            st.markdown(f"<br><span style='color:{MUTED};font-size:0.75rem'>To clear an alert: review the price manually in Shopify, then mark <code>resolved = Yes</code> in the error_flags sheet.</span>", unsafe_allow_html=True)

        if not closed_ef.empty:
            with st.expander(f"✅ {len(closed_ef)} resolved"):
                cols = [c for c in ["flagged_at","supplier","sku","error_type","detail"] if c in closed_ef.columns]
                st.dataframe(closed_ef[cols], use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════
# TAB 4 — New Products
# ══════════════════════════════════════════════
with tab_new:
    st.markdown('<div class="section-label">New Products — Not Yet on Website</div>', unsafe_allow_html=True)
    st.markdown(f"<p style='color:{MUTED};font-size:0.82rem;margin-bottom:20px'>These came through from active suppliers but have no Shopify listing yet. Forward to Ricky to decide which to add.</p>", unsafe_allow_html=True)

    if new_products.empty:
        st.markdown(f"""
        <div style="background:{CARD};border:1px solid {BORDER};border-radius:12px;
                    padding:28px;text-align:center;color:{MUTED}">
            No new products waiting for review
        </div>
        """, unsafe_allow_html=True)
    else:
        np_df = new_products.copy()
        if "supplier" in np_df.columns:
            np_df["_sup_label"] = np_df["supplier"].map(lambda x: SUPPLIER_LABELS.get(x, x))
            for sup_label, grp in np_df.groupby("_sup_label"):
                with st.expander(f"**{sup_label}** — {len(grp)} product(s)", expanded=True):
                    show_cols = [c for c in ["sku","description","selling_price","stock_status","date_found"] if c in grp.columns]
                    st.dataframe(grp[show_cols].reset_index(drop=True),
                                 use_container_width=True, hide_index=True)
        else:
            st.dataframe(np_df, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════
# TAB 5 — All Products
# ══════════════════════════════════════════════
with tab_master:
    st.markdown('<div class="section-label">All Tracked Products</div>', unsafe_allow_html=True)

    if master.empty:
        st.info("No products in master sheet yet.")
    else:
        m = master.copy()
        m["_sup_label"] = m["supplier"].map(lambda x: SUPPLIER_LABELS.get(x, x))
        m["_on_site"]   = m["shopify_variant_id"].astype(str).str.strip().ne("").map(
            {True: "✅ Yes", False: "❌ No"}
        )

        f1, f2, f3 = st.columns([2, 2, 3])
        with f1:
            sup_opts = ["All"] + sorted(m["_sup_label"].dropna().unique().tolist())
            sup_f = st.selectbox("Supplier", sup_opts, key="m_sup")
        with f2:
            web_opts = ["All", "✅ On website", "❌ Not linked"]
            web_f = st.selectbox("Website", web_opts, key="m_web")
        with f3:
            search = st.text_input("Search SKU or description", placeholder="e.g. Engel 60L", key="m_search")

        mask = pd.Series([True] * len(m), index=m.index)
        if sup_f != "All":
            mask &= m["_sup_label"] == sup_f
        if web_f == "✅ On website":
            mask &= m["_on_site"] == "✅ Yes"
        elif web_f == "❌ Not linked":
            mask &= m["_on_site"] == "❌ No"
        if search:
            s = search.lower()
            mask &= (m["sku"].astype(str).str.lower().str.contains(s) |
                     m["description"].astype(str).str.lower().str.contains(s))

        filtered_m = m[mask]
        show_cols  = [c for c in ["sku","_sup_label","description","selling_price","rrp","stock_status","_on_site","last_updated"] if c in filtered_m.columns]
        disp_m     = filtered_m[show_cols].rename(columns={"_sup_label":"supplier","_on_site":"on website"})

        def _style_master(row):
            styles = [""] * len(row)
            cols = list(row.index)
            if "stock_status" in cols:
                v = str(row["stock_status"])
                c = NEON if "In Stock" in v else (RED if "Out" in v else MUTED)
                styles[cols.index("stock_status")] = f"color:{c}"
            if "on website" in cols:
                v = str(row["on website"])
                styles[cols.index("on website")] = f"color:{NEON}" if "Yes" in v else f"color:{MUTED}"
            return styles

        styled_m = disp_m.style.apply(_style_master, axis=1)\
            .set_properties(**{"background-color": CARD, "color": TEXT, "font-size": "0.82rem"})

        st.caption(f"{len(filtered_m)} of {len(m)} products")
        st.dataframe(styled_m, use_container_width=True, height=520, hide_index=True)


# ─────────────────────────────────────────────
# Footer
# ─────────────────────────────────────────────
st.markdown(f"""
<hr>
<div style="display:flex;justify-content:space-between;align-items:center">
  <span style="color:{MUTED};font-size:0.72rem">CFSA Price Sync · Built by Streamline Digital · Data from Google Sheets</span>
  <span style="color:{MUTED};font-size:0.72rem">Auto-refreshes every 2 min · {datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")}</span>
</div>
""", unsafe_allow_html=True)
