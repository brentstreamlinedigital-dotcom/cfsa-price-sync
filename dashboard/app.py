"""
CFSA Price Sync — Dashboard
Run with: streamlit run dashboard/app.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials
import gspread

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
SA_KEY = ROOT / "sa-key.json"
SPREADSHEET_ID = "1YRVzl7E48Y8kQ3V6yJbNrzj8QqqH7016tEVQ0IkH9Co"

SUPPLIER_LABELS = {
    "flex":               "Flex Adventures",
    "engel":              "Engel",
    "snomaster":          "Snomaster",
    "lite_optec":         "Lite Optec (Frozen/Coleman)",
    "dag":                "D.A.G",
    "dometic_frontrunner":"Dometic (Front Runner)",
    "dometic_thrsa":      "Dometic (THRSA)",
    "arb":                "ARB",
    "coldfactor":         "ColdFactor",
    "highon":             "HighOn",
    "tsunami":            "Tsunami Coolers",
    "frozen":             "Frozen",
}

ACTIVE_SUPPLIERS = {"flex", "engel", "snomaster", "lite_optec"}

st.set_page_config(
    page_title="CFSA Price Sync",
    page_icon="🧊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .stApp { background-color: #0f1117; }
    .metric-card {
        background: #1c1f2e;
        border: 1px solid #2d3147;
        border-radius: 12px;
        padding: 20px 24px;
        text-align: center;
    }
    .metric-value { font-size: 2.2rem; font-weight: 700; color: #ffffff; }
    .metric-label { font-size: 0.85rem; color: #8b8fa8; margin-top: 4px; }
    .section-header {
        font-size: 1.1rem; font-weight: 600; color: #c8cadb;
        border-bottom: 1px solid #2d3147; padding-bottom: 8px; margin-bottom: 16px;
    }
    .status-live { color: #22c55e; font-weight: 600; }
    .status-inactive { color: #6b7280; }
    .status-alert { color: #f59e0b; font-weight: 600; }
    .pill {
        display: inline-block; padding: 2px 10px; border-radius: 20px;
        font-size: 0.78rem; font-weight: 600;
    }
    .pill-green { background: #052e16; color: #22c55e; }
    .pill-red   { background: #2d0a0a; color: #f87171; }
    .pill-amber { background: #2d1a00; color: #f59e0b; }
    .pill-grey  { background: #1c1f2e; color: #6b7280; }
    div[data-testid="stMetricValue"] { color: white; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
with st.spinner("Loading data..."):
    try:
        sheets = load_sheets()
        master        = sheets["master"]
        price_changes = sheets["price_changes"]
        supplier_log  = sheets["supplier_log"]
        error_flags   = sheets["error_flags"]
        new_products  = sheets["new_products"]
        load_error = None
    except Exception as e:
        load_error = str(e)
        master = price_changes = supplier_log = error_flags = new_products = pd.DataFrame()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
col_logo, col_title, col_refresh = st.columns([1, 6, 1])
with col_logo:
    st.markdown("## 🧊")
with col_title:
    st.markdown("# CFSA Price Sync")
    st.caption("campingfridge.co.za — Automated pricing dashboard")
with col_refresh:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("⟳ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

if load_error:
    st.error(f"Could not load spreadsheet: {load_error}")
    st.stop()

st.markdown("---")

# ---------------------------------------------------------------------------
# KPI row
# ---------------------------------------------------------------------------
linked = master[master["shopify_variant_id"].astype(str).str.strip().ne("")] if not master.empty else pd.DataFrame()
open_alerts = error_flags[error_flags["resolved"].astype(str).str.upper() != "YES"] if not error_flags.empty else pd.DataFrame()
price_alert_count = len(open_alerts[open_alerts["error_type"] == "price_alert"]) if not open_alerts.empty else 0
new_prod_count = len(new_products) if not new_products.empty else 0

# Last sync time from supplier_log
last_sync = "Never"
if not supplier_log.empty and "timestamp" in supplier_log.columns:
    try:
        ts = pd.to_datetime(supplier_log["timestamp"], errors="coerce").dropna()
        if not ts.empty:
            latest = ts.max()
            # Format as "3 hours ago" style
            diff = datetime.now(timezone.utc) - latest.to_pydatetime().replace(tzinfo=timezone.utc)
            hrs = int(diff.total_seconds() // 3600)
            mins = int((diff.total_seconds() % 3600) // 60)
            last_sync = f"{hrs}h {mins}m ago" if hrs > 0 else f"{mins}m ago"
    except Exception:
        pass

k1, k2, k3, k4, k5 = st.columns(5)

with k1:
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value">{len(master) if not master.empty else 0}</div>
        <div class="metric-label">Products Tracked</div>
    </div>""", unsafe_allow_html=True)
with k2:
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value" style="color:#22c55e">{len(linked)}</div>
        <div class="metric-label">Linked to Website</div>
    </div>""", unsafe_allow_html=True)
with k3:
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value" style="color:#f59e0b">{price_alert_count}</div>
        <div class="metric-label">Price Alerts</div>
    </div>""", unsafe_allow_html=True)
with k4:
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value" style="color:#60a5fa">{new_prod_count}</div>
        <div class="metric-label">New Products</div>
    </div>""", unsafe_allow_html=True)
with k5:
    st.markdown(f"""<div class="metric-card">
        <div class="metric-value" style="color:#a78bfa; font-size:1.4rem">{last_sync}</div>
        <div class="metric-label">Last Sync</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_changes, tab_suppliers, tab_alerts, tab_new, tab_master = st.tabs([
    "📈 Price Changes",
    "🔌 Suppliers",
    "⚠️ Alerts",
    "🆕 New Products",
    "📦 All Products",
])

# ── TAB 1: Price Changes ────────────────────────────────────────────────────
with tab_changes:
    st.markdown('<div class="section-header">Recent Price Changes</div>', unsafe_allow_html=True)

    if price_changes.empty:
        st.info("No price changes recorded yet. They'll appear here after the first sync run.")
    else:
        pc = price_changes.copy()

        # Parse dates
        pc["date"] = pd.to_datetime(pc["date"], errors="coerce")
        pc = pc.sort_values("date", ascending=False)

        # Filters row
        f1, f2, f3 = st.columns([2, 2, 2])
        with f1:
            suppliers_in_pc = ["All"] + sorted(pc["supplier"].dropna().unique().tolist())
            sup_filter = st.selectbox("Supplier", suppliers_in_pc, key="pc_sup")
        with f2:
            direction = st.selectbox("Direction", ["All", "Price Up ↑", "Price Down ↓", "Alerted only"], key="pc_dir")
        with f3:
            days_back = st.selectbox("Time period", [7, 14, 30, 90, 365], format_func=lambda x: f"Last {x} days", key="pc_days")

        # Apply filters
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_back)
        mask = pc["date"] >= cutoff
        if sup_filter != "All":
            mask &= pc["supplier"] == sup_filter
        if direction == "Price Up ↑":
            mask &= pc["change_amt"].astype(str).str.replace(",", "").str.strip().apply(
                lambda x: float(x) > 0 if x and x not in ("", "-") else False
            )
        elif direction == "Price Down ↓":
            mask &= pc["change_amt"].astype(str).str.replace(",", "").str.strip().apply(
                lambda x: float(x) < 0 if x and x not in ("", "-") else False
            )
        elif direction == "Alerted only":
            mask &= pc["alerted"].astype(str).str.upper() == "YES"

        filtered = pc[mask].copy()

        if filtered.empty:
            st.info("No changes match the selected filters.")
        else:
            # Chart
            chart_data = filtered.copy()
            chart_data["date_day"] = chart_data["date"].dt.date
            daily_counts = chart_data.groupby("date_day").size().reset_index(name="changes")
            fig = px.bar(
                daily_counts, x="date_day", y="changes",
                color_discrete_sequence=["#6366f1"],
                labels={"date_day": "", "changes": "Changes"},
                height=180,
            )
            fig.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#c8cadb",
                margin=dict(l=0, r=0, t=10, b=0),
                showlegend=False,
                xaxis=dict(gridcolor="#2d3147"),
                yaxis=dict(gridcolor="#2d3147"),
            )
            st.plotly_chart(fig, use_container_width=True)

            # Table
            display = filtered[["date", "supplier", "sku", "description", "old_price", "new_price", "change_amt", "change_pct", "alerted"]].copy()
            display["date"] = display["date"].dt.strftime("%d %b %Y %H:%M")
            display["supplier"] = display["supplier"].map(lambda x: SUPPLIER_LABELS.get(x, x))

            def color_change(val):
                s = str(val).replace(",", "").strip()
                try:
                    v = float(s)
                    if v > 0: return "color: #f87171"
                    if v < 0: return "color: #22c55e"
                except Exception: pass
                return ""

            def color_alerted(val):
                return "color: #f59e0b; font-weight:600" if str(val).upper() == "YES" else "color: #6b7280"

            styled = display.style\
                .applymap(color_change, subset=["change_amt", "change_pct"])\
                .applymap(color_alerted, subset=["alerted"])\
                .set_properties(**{"background-color": "#1c1f2e", "color": "#e2e4f0"})

            st.dataframe(styled, use_container_width=True, height=400)
            st.caption(f"{len(filtered)} change(s) — green = price dropped, red = price rose, amber = held for review")

# ── TAB 2: Suppliers ────────────────────────────────────────────────────────
with tab_suppliers:
    st.markdown('<div class="section-header">Supplier Connection Status</div>', unsafe_allow_html=True)

    # Build summary from master sheet + supplier_log
    supplier_stats = {}
    if not master.empty:
        for sup, grp in master.groupby("supplier"):
            linked_count = grp["shopify_variant_id"].astype(str).str.strip().ne("").sum()
            supplier_stats[sup] = {
                "total": len(grp),
                "linked": int(linked_count),
                "last_updated": grp["last_updated"].max() if "last_updated" in grp.columns else "",
            }

    last_run_by_sup = {}
    if not supplier_log.empty and "supplier" in supplier_log.columns:
        for sup, grp in supplier_log.groupby("supplier"):
            try:
                ts = pd.to_datetime(grp["timestamp"], errors="coerce").dropna()
                if not ts.empty:
                    last_run_by_sup[sup] = ts.max().strftime("%d %b %Y %H:%M")
            except Exception:
                pass

    all_suppliers = sorted(set(list(supplier_stats.keys()) + list(SUPPLIER_LABELS.keys())))

    rows = []
    for sup in all_suppliers:
        stats = supplier_stats.get(sup, {"total": 0, "linked": 0})
        is_active = sup in ACTIVE_SUPPLIERS
        label = SUPPLIER_LABELS.get(sup, sup)
        last_run = last_run_by_sup.get(sup, "—")
        rows.append({
            "Status": "🟢 Live" if is_active else "⚫ Inactive",
            "Supplier": label,
            "Products Tracked": stats["total"],
            "Linked to Website": stats["linked"],
            "Last Sync": last_run,
        })

    sup_df = pd.DataFrame(rows)
    active_df = sup_df[sup_df["Status"] == "🟢 Live"]
    inactive_df = sup_df[sup_df["Status"] == "⚫ Inactive"]

    st.markdown("**Live — prices auto-updating**")
    st.dataframe(
        active_df.drop(columns=["Status"]).reset_index(drop=True),
        use_container_width=True, hide_index=True,
    )

    st.markdown("<br>**Inactive — waiting for supplier connection**", unsafe_allow_html=True)
    st.dataframe(
        inactive_df.drop(columns=["Status", "Last Sync"]).reset_index(drop=True),
        use_container_width=True, hide_index=True,
    )

    # Pie chart
    if not master.empty:
        pie_data = master.copy()
        pie_data["is_linked"] = pie_data["shopify_variant_id"].astype(str).str.strip().ne("")
        by_sup = pie_data.groupby("supplier").agg(
            total=("sku", "count"),
            linked=("is_linked", "sum")
        ).reset_index()
        by_sup["label"] = by_sup["supplier"].map(lambda x: SUPPLIER_LABELS.get(x, x))

        st.markdown("<br>", unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            fig = px.pie(
                by_sup, values="total", names="label",
                title="Products by supplier",
                color_discrete_sequence=px.colors.qualitative.Set3,
                hole=0.4,
            )
            fig.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", font_color="#c8cadb",
                margin=dict(l=0, r=0, t=40, b=0), height=300,
                legend=dict(font_size=11),
            )
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            fig2 = go.Figure(go.Bar(
                x=by_sup["label"],
                y=by_sup["linked"],
                name="Linked",
                marker_color="#22c55e",
            ))
            fig2.add_bar(
                x=by_sup["label"],
                y=by_sup["total"] - by_sup["linked"],
                name="Unlinked",
                marker_color="#374151",
            )
            fig2.update_layout(
                barmode="stack",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#c8cadb",
                title="Website coverage",
                margin=dict(l=0, r=0, t=40, b=60),
                height=300,
                legend=dict(font_size=11),
                xaxis=dict(tickangle=-30, gridcolor="#2d3147"),
                yaxis=dict(gridcolor="#2d3147"),
            )
            st.plotly_chart(fig2, use_container_width=True)

# ── TAB 3: Alerts ───────────────────────────────────────────────────────────
with tab_alerts:
    st.markdown('<div class="section-header">Price Alerts & Errors</div>', unsafe_allow_html=True)

    if error_flags.empty:
        st.success("No alerts. All good!")
    else:
        ef = error_flags.copy()
        open_ef = ef[ef["resolved"].astype(str).str.upper() != "YES"]

        c1, c2 = st.columns(2)
        with c1:
            price_alerts = open_ef[open_ef["error_type"] == "price_alert"]
            st.metric("Open Price Alerts", len(price_alerts), help="Price moved >5% — held for manual review")
        with c2:
            shopify_errs = open_ef[open_ef["error_type"] != "price_alert"]
            st.metric("Other Errors", len(shopify_errs))

        if not open_ef.empty:
            st.markdown("**Unresolved items**")
            cols_to_show = [c for c in ["flagged_at", "supplier", "sku", "error_type", "detail", "resolved"] if c in open_ef.columns]
            display_ef = open_ef[cols_to_show].copy()
            if "supplier" in display_ef.columns:
                display_ef["supplier"] = display_ef["supplier"].map(lambda x: SUPPLIER_LABELS.get(x, x))

            def style_error_type(val):
                if val == "price_alert":
                    return "color: #f59e0b; font-weight: 600"
                return "color: #f87171"

            styled_ef = display_ef.style.applymap(style_error_type, subset=["error_type"])
            st.dataframe(styled_ef, use_container_width=True, height=350, hide_index=True)
            st.caption("To resolve an alert, manually check + update the price in Shopify, then mark 'Yes' in the 'resolved' column of the master sheet's error_flags tab.")

        # Resolved section
        resolved = ef[ef["resolved"].astype(str).str.upper() == "YES"]
        if not resolved.empty:
            with st.expander(f"✅ {len(resolved)} resolved alerts"):
                st.dataframe(resolved, use_container_width=True, hide_index=True)

# ── TAB 4: New Products ─────────────────────────────────────────────────────
with tab_new:
    st.markdown('<div class="section-header">New Products — Not Yet on Website</div>', unsafe_allow_html=True)
    st.caption("These products came through from suppliers but don't have a Shopify listing yet. Forward to Ricky to decide which to add.")

    if new_products.empty:
        st.info("No new products waiting for review.")
    else:
        np_df = new_products.copy()
        if "supplier" in np_df.columns:
            np_df["supplier"] = np_df["supplier"].map(lambda x: SUPPLIER_LABELS.get(x, x))

        # Group by supplier
        if "supplier" in np_df.columns:
            for sup, grp in np_df.groupby("supplier"):
                with st.expander(f"**{sup}** — {len(grp)} product(s)", expanded=True):
                    cols = [c for c in ["sku", "description", "selling_price", "stock_status", "date_found"] if c in grp.columns]
                    st.dataframe(grp[cols].reset_index(drop=True), use_container_width=True, hide_index=True)
        else:
            st.dataframe(np_df, use_container_width=True, hide_index=True)

# ── TAB 5: All Products ─────────────────────────────────────────────────────
with tab_master:
    st.markdown('<div class="section-header">All Tracked Products</div>', unsafe_allow_html=True)

    if master.empty:
        st.info("No products in master sheet yet.")
    else:
        m = master.copy()
        m["supplier_label"] = m["supplier"].map(lambda x: SUPPLIER_LABELS.get(x, x))
        m["on_website"] = m["shopify_variant_id"].astype(str).str.strip().ne("").map({True: "✅ Yes", False: "❌ No"})

        # Filters
        f1, f2, f3 = st.columns([2, 2, 2])
        with f1:
            sup_opts = ["All"] + sorted(m["supplier_label"].dropna().unique().tolist())
            sup_f = st.selectbox("Supplier", sup_opts, key="m_sup")
        with f2:
            web_opts = ["All", "✅ On website", "❌ Not on website"]
            web_f = st.selectbox("Website status", web_opts, key="m_web")
        with f3:
            search = st.text_input("Search SKU / description", key="m_search")

        mask = pd.Series([True] * len(m), index=m.index)
        if sup_f != "All":
            mask &= m["supplier_label"] == sup_f
        if web_f == "✅ On website":
            mask &= m["on_website"] == "✅ Yes"
        elif web_f == "❌ Not on website":
            mask &= m["on_website"] == "❌ No"
        if search:
            s = search.lower()
            mask &= (
                m["sku"].astype(str).str.lower().str.contains(s) |
                m["description"].astype(str).str.lower().str.contains(s)
            )

        filtered_m = m[mask]
        st.caption(f"Showing {len(filtered_m)} of {len(m)} products")

        cols = [c for c in ["sku", "supplier_label", "description", "selling_price", "rrp", "stock_status", "on_website", "last_updated"] if c in filtered_m.columns]
        display_m = filtered_m[cols].rename(columns={"supplier_label": "supplier"})

        def style_stock(val):
            if "In Stock" in str(val): return "color: #22c55e"
            if "Out" in str(val): return "color: #f87171"
            return "color: #6b7280"

        styled_m = display_m.style.applymap(style_stock, subset=["stock_status"])
        st.dataframe(styled_m, use_container_width=True, height=500, hide_index=True)

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
st.markdown("---")
now = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
st.caption(f"CFSA Price Sync Dashboard · Data refreshes every 2 minutes · Last page load: {now}")
