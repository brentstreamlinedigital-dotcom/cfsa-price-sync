"""
CFSA Price Sync — Dashboard
Run with: python3 -m streamlit run dashboard/app.py
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials
import gspread
import yaml

# Ensure repo root is importable (needed for src.* and scrapers.*)
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ─────────────────────────────────────────────────────────────
# Config — resolved from Streamlit secrets → env var → local file
# ─────────────────────────────────────────────────────────────
SA_KEY       = ROOT / "sa-key.json"
SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.readonly"]

# Spreadsheet ID: set via st.secrets["spreadsheet_id"] on Streamlit Cloud
# or SHEETS_SPREADSHEET_ID env var for local dev
SPREADSHEET_ID = (
    st.secrets.get("spreadsheet_id", "")
    or os.getenv("SHEETS_SPREADSHEET_ID", "")
)

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

# ── Competitor config ──────────────────────────────────────────────────────
def _load_competitor_config() -> list[dict]:
    try:
        cfg_path = ROOT / "config" / "competitors.yaml"
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}
        comps = [c for c in data.get("competitors", []) if c.get("enabled", True)]
        return sorted(comps, key=lambda c: c.get("priority", 99))
    except Exception:
        return []

COMPETITORS = _load_competitor_config()
# Column names in the Google Sheet for each competitor's price
COMP_PRICE_COLS = [f"{c['name']}_price" for c in COMPETITORS]
COMP_DISPLAY    = {c["name"]: c.get("display_name", c["name"]) for c in COMPETITORS}

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

/* ══════════════════════════════════════════
   MOBILE  ≤ 768 px
   ══════════════════════════════════════════ */
@media (max-width: 768px) {{

    /* ── Reduce outer padding ──────────────── */
    .block-container {{
        padding: 1.2rem 1rem 3rem !important;
        max-width: 100% !important;
    }}

    /* ── Nav: stack brand + meta ───────────── */
    .nav {{
        flex-direction: column;
        align-items: flex-start;
        gap: 10px;
        padding-bottom: 18px;
        margin-bottom: 22px;
    }}
    .nav-right {{
        width: 100%;
        justify-content: space-between;
    }}
    .nav-time {{ text-align: left; }}

    /* ── KPI grid: 2-up ────────────────────── */
    .kpi-row {{
        grid-template-columns: repeat(2, 1fr);
        gap: 10px;
        margin-bottom: 24px;
    }}
    /* 5th card spans full width so nothing is orphaned */
    .kpi:last-child {{ grid-column: 1 / -1; }}
    .kpi {{ padding: 14px 14px 12px; border-radius: 11px; }}
    .kpi-num {{ font-size: 1.65rem; }}
    .kpi-label {{ font-size: 0.63rem; margin-bottom: 7px; }}
    .kpi-sub {{ font-size: 0.66rem; }}

    /* ── Tabs: full-width + horizontal scroll ─ */
    .stTabs [data-baseweb="tab-list"] {{
        width: 100% !important;
        overflow-x: auto !important;
        flex-wrap: nowrap !important;
        scrollbar-width: none !important;
        -webkit-overflow-scrolling: touch !important;
        border-radius: 10px !important;
    }}
    .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar {{ display: none; }}
    .stTabs [data-baseweb="tab"] {{
        padding: 7px 11px !important;
        font-size: 0.74rem !important;
        flex-shrink: 0 !important;
    }}
    .stTabs [data-baseweb="tab-panel"] {{ padding-top: 20px !important; }}

    /* ── Supplier cards: wrap right side ──────── */
    .card {{ padding: 12px 14px; }}
    .card-active  {{ padding-left: 12px; }}
    .card-amber, .card-red {{ padding-left: 12px; }}
    .row-between {{ flex-wrap: wrap; gap: 8px; align-items: flex-start; }}
    .card-title {{ font-size: 0.84rem; }}
    .card-sub {{ font-size: 0.7rem; }}
    .cov-num {{ font-size: 0.95rem; }}

    /* ── Streamlit columns → stack vertically ─ */
    [data-testid="stHorizontalBlock"] {{
        flex-wrap: wrap !important;
        gap: 0 !important;
    }}
    [data-testid="column"] {{
        min-width: min(100%, 260px) !important;
        flex: 1 1 260px !important;
    }}

    /* ── Inputs: bigger tap targets ───────────── */
    .stSelectbox > div > div > div,
    .stTextInput > div > div > input {{
        font-size: 0.9rem !important;
        padding: 10px 12px !important;
        min-height: 44px !important;
    }}
    label {{ font-size: 0.72rem !important; }}

    /* ── Refresh button: full-width, easy tap ─── */
    .stButton > button {{
        width: 100% !important;
        min-height: 44px !important;
        font-size: 0.85rem !important;
    }}

    /* ── Data tables: horizontal scroll ────────── */
    [data-testid="stDataFrame"] > div {{
        overflow-x: auto !important;
        -webkit-overflow-scrolling: touch !important;
    }}
    [data-testid="stDataFrame"] {{
        min-width: 0 !important;
    }}
    .stDataFrame thead tr th {{
        font-size: 0.62rem !important;
        padding: 8px 8px !important;
        white-space: nowrap;
    }}
    .stDataFrame tbody tr td {{
        font-size: 0.76rem !important;
        padding: 7px 8px !important;
    }}

    /* ── Metrics ──────────────────────────────── */
    [data-testid="stMetricValue"] {{ font-size: 1.3rem !important; }}

    /* ── Plotly chart: limit height on mobile ─── */
    .js-plotly-plot {{ max-height: 320px !important; }}

    /* ── Section label ─────────────────────────── */
    .slabel {{ font-size: 0.63rem; margin-bottom: 12px; }}
}}

/* ══════════════════════════════════════════
   SMALL PHONES  ≤ 400 px
   ══════════════════════════════════════════ */
@media (max-width: 400px) {{
    .block-container {{ padding: 1rem 0.75rem 2.5rem !important; }}
    .kpi-num {{ font-size: 1.45rem; }}
    .kpi {{ padding: 12px 12px 10px; }}
    .nav-name {{ font-size: 0.92rem; }}
    .stTabs [data-baseweb="tab"] {{ padding: 6px 9px !important; font-size: 0.7rem !important; }}
    /* 1-column KPI grid for very small screens */
    .kpi-row {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
}}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# Subprocess env helper
# ─────────────────────────────────────────────────────────────
def _subprocess_env() -> dict:
    """
    Build an env dict for child processes that inherits the current process
    environment and layers in any credentials/secrets that may only exist in
    Streamlit secrets (not in the OS environment).
    """
    import os, tempfile, json
    env = os.environ.copy()

    # Spreadsheet ID
    if SPREADSHEET_ID and not env.get("SHEETS_SPREADSHEET_ID"):
        env["SHEETS_SPREADSHEET_ID"] = SPREADSHEET_ID

    # Shopify credentials
    for key, secret_key in [
        ("SHOPIFY_SHOP_DOMAIN",  "shopify_shop_domain"),
        ("SHOPIFY_ACCESS_TOKEN", "shopify_access_token"),
    ]:
        if not env.get(key):
            val = st.secrets.get(secret_key, "")
            if val:
                env[key] = val

    # GCP service account — write to a temp file if coming from st.secrets
    if not env.get("GOOGLE_APPLICATION_CREDENTIALS"):
        if "gcp_service_account" in st.secrets:
            sa_info = dict(st.secrets["gcp_service_account"])
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            )
            json.dump(sa_info, tmp)
            tmp.close()
            env["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
        elif SA_KEY.exists():
            env["GOOGLE_APPLICATION_CREDENTIALS"] = str(SA_KEY)

    return env


# ─────────────────────────────────────────────────────────────
# Automation status helpers
# ─────────────────────────────────────────────────────────────
_LOGS_DIR = ROOT / "logs"

def _pid_alive(pid: int) -> bool:
    """Return True if the process with this PID is still running."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)   # signal 0 = check only, no kill
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False

def _read_automation_status(automation: str) -> dict:
    """Read the status JSON for an automation. Returns idle dict if not present."""
    path = _LOGS_DIR / f"{automation}_status.json"
    if not path.exists():
        return {"automation": automation, "status": "idle", "total": 0, "done": 0, "stage": ""}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"automation": automation, "status": "idle", "total": 0, "done": 0, "stage": ""}

def _is_running(automation: str) -> tuple[bool, dict]:
    """Return (is_running, status_data). Running means status=running|starting AND PID alive."""
    s = _read_automation_status(automation)
    if s.get("status") in ("running", "starting") and _pid_alive(s.get("pid", 0)):
        return True, s
    return False, s

def _render_progress(status: dict, placeholder) -> None:
    """Render a live progress bar into a Streamlit placeholder."""
    total   = int(status.get("total", 0))
    done    = int(status.get("done", 0))
    stage   = status.get("stage", "Working…")
    current = status.get("current", "")
    pct     = done / total if total > 0 else 0.0
    label   = f"{stage}  ·  {done}/{total}" if total else stage
    if current:
        label += f"  ·  {current}"
    with placeholder.container():
        st.progress(pct, text=label)
        st.caption(f"Last updated {status.get('last_updated', '')[:19].replace('T', ' ')} UTC  ·  PID {status.get('pid', '—')}")


# ─────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=120, show_spinner=False)
def load_sheets():
    # Streamlit Cloud: credentials come from st.secrets["gcp_service_account"]
    # Local dev:       fall back to sa-key.json file
    if "gcp_service_account" in st.secrets:
        creds = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]),
            scopes=SHEET_SCOPES,
        )
    else:
        creds = Credentials.from_service_account_file(
            str(SA_KEY),
            scopes=SHEET_SCOPES,
        )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    def to_df(name):
        """
        Read a worksheet into a DataFrame.

        We avoid gspread's get_all_records() because it raises when the header
        row contains duplicate or empty strings (which happens whenever a
        write adds trailing blank columns). Instead we read raw values and
        build the DataFrame ourselves — robust to trailing blanks, duplicate
        headers, and ragged rows.
        """
        try:
            ws = sh.worksheet(name)
        except Exception:
            return pd.DataFrame()
        try:
            values = ws.get_all_values()
        except Exception:
            return pd.DataFrame()
        if not values:
            return pd.DataFrame()
        headers = values[0]
        # Trim trailing blank header columns
        while headers and not headers[-1].strip():
            headers.pop()
        # De-duplicate any remaining empty / repeated headers
        seen = {}
        clean_headers = []
        for i, h in enumerate(headers):
            h = h.strip() or f"col_{i}"
            if h in seen:
                seen[h] += 1
                h = f"{h}_{seen[h]}"
            else:
                seen[h] = 0
            clean_headers.append(h)
        width = len(clean_headers)
        rows = []
        for raw_row in values[1:]:
            # Pad / truncate to header width so ragged rows don't break the frame
            row = (raw_row + [""] * width)[:width]
            # Failsafe: drop "header-as-data" rows — these occur when an
            # older writer inserted a duplicate header row (which we have
            # since fixed, but historical sheets may still contain them).
            # Detect: every cell equals its column name (case-insensitive).
            if all(
                str(cell).strip().lower() == str(col).strip().lower()
                for cell, col in zip(row, clean_headers)
            ):
                continue
            rows.append(row)
        return pd.DataFrame(rows, columns=clean_headers)

    return {k: to_df(k) for k in [
        "master", "price_changes", "supplier_log", "error_flags",
        "new_products", "price_sync_log", "competitor_analysis_log",
        "suppliers",
    ]}

with st.spinner(""):
    try:
        sheets               = load_sheets()
        master               = sheets["master"]
        price_changes        = sheets["price_changes"]
        supplier_log         = sheets["supplier_log"]
        error_flags          = sheets["error_flags"]
        new_products         = sheets["new_products"]
        price_sync_log       = sheets["price_sync_log"]
        comp_analysis_log    = sheets["competitor_analysis_log"]
        suppliers_view       = sheets["suppliers"]
        load_error           = None
    except Exception as e:
        load_error = str(e)
        master = price_changes = supplier_log = error_flags = new_products = price_sync_log = comp_analysis_log = suppliers_view = pd.DataFrame()

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
_, rbtn = st.columns([6, 1])
with rbtn:
    if st.button("↻ Refresh", use_container_width=True):
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
# Live progress fragments — auto-refresh every 3 s, no page reload
# ─────────────────────────────────────────────────────────────
def _maybe_autorefresh_on_complete(automation: str, status: dict) -> bool:
    """
    If the automation just transitioned to 'completed' (a completed_at we haven't
    seen yet), clear the Sheets cache and trigger a full-app rerun so the latest
    data shows up without the user having to click Refresh.

    Returns True if a rerun was triggered (caller should stop rendering).
    """
    if status.get("status") != "completed":
        return False
    completed_at = status.get("completed_at") or ""
    if not completed_at:
        return False
    key = f"_seen_completed_{automation}"
    if st.session_state.get(key) == completed_at:
        return False
    # New completion → mark seen, clear cache, full app rerun
    st.session_state[key] = completed_at
    st.cache_data.clear()
    st.rerun(scope="app")
    return True


@st.fragment(run_every=3)
def _sync_progress_fragment():
    running, s = _is_running("price_sync")
    st_val = s.get("status", "idle")
    if running:
        total = int(s.get("total", 0))
        done  = int(s.get("done", 0))
        pct   = done / total if total > 0 else 0.05
        stage = s.get("stage", "Working…")
        lbl   = stage + (f"  ({done}/{total} suppliers)" if total else "")
        if s.get("current"):
            lbl += f"  —  {s['current']}"
        st.progress(pct, text=lbl)
        st.caption(f"PID {s.get('pid','—')}  ·  started {s.get('started_at','')[:19].replace('T',' ')} UTC")
    elif st_val == "completed":
        if _maybe_autorefresh_on_complete("price_sync", s):
            return  # full-app rerun in progress
        total = s.get("total", 0)
        st.success(f"✓ Sync completed — {total} supplier{'s' if total != 1 else ''} processed.")
    elif st_val == "failed":
        st.error(f"✗ Sync failed: {s.get('error','unknown error')}")


@st.fragment(run_every=3)
def _ca_progress_fragment():
    running, s = _is_running("competitor_analysis")
    st_val = s.get("status", "idle")
    if running:
        total = int(s.get("total", 0))
        done  = int(s.get("done", 0))
        pct   = done / total if total > 0 else 0.02
        stage = s.get("stage", "Working…")
        lbl   = stage + (f"  ({done}/{total} products)" if total else "")
        if s.get("current"):
            lbl += f"  —  {s['current']}"
        st.progress(pct, text=lbl)
        st.caption(f"PID {s.get('pid','—')}  ·  started {s.get('started_at','')[:19].replace('T',' ')} UTC")
    elif st_val == "completed":
        if _maybe_autorefresh_on_complete("competitor_analysis", s):
            return  # full-app rerun in progress
        total = s.get("total", 0)
        st.success(f"✓ Analysis completed — {total} product{'s' if total != 1 else ''} analysed.")
    elif st_val == "failed":
        st.error(f"✗ Analysis failed: {s.get('error','unknown error')}")


# ─────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────
t1, t2, t3, t4, t5, t6, t7, t8 = st.tabs([
    "Price Changes", "Suppliers", "Alerts", "New Products",
    "All Products", "Price Sync", "Competitor Analysis", "Held for Review",
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

        # Filters — 2×2 grid so each row is readable on mobile
        fa, fb = st.columns(2)
        with fa:
            sup_opts = ["All suppliers"] + sorted(pc["supplier"].dropna().unique().tolist())
            sup_f = st.selectbox("Supplier", sup_opts, key="pc_s",
                                  format_func=lambda x: SUPPLIER_LABELS.get(x, x))
        with fb:
            dir_f = st.selectbox("Direction", ["All", "Price up ↑", "Price down ↓", "Alerts only"], key="pc_d")
        fc, fd = st.columns(2)
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
            barmode="stack", height=max(220, len(cdf)*34),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=T3, family="Inter", size=11),
            margin=dict(l=0,r=0,t=4,b=0),
            legend=dict(orientation="h", x=1, xanchor="right", y=1.08,
                        bgcolor="rgba(0,0,0,0)", font_size=11),
            xaxis=dict(gridcolor="rgba(255,255,255,.04)", zeroline=False),
            yaxis=dict(gridcolor="rgba(0,0,0,0)", tickfont=dict(size=10)),
        )
        st.plotly_chart(fig2, use_container_width=True, config={"responsive": True})

    # ── Per-supplier catalog (denormalized view) ─────────────────────
    # Source: the `suppliers` tab, rebuilt by both automations on every run.
    # This is the same data both the price-sync and competitor automations
    # see — keeps "what we think the catalog is" and "what the UI shows"
    # always identical.
    st.markdown(f"<div style='margin-top:28px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="slabel">Catalog by supplier — costs, prices, margin & stock</div>', unsafe_allow_html=True)

    if suppliers_view.empty:
        st.info(
            "The `suppliers` tab hasn't been built yet. Run the price sync or "
            "competitor analysis once — both automations refresh this view "
            "on every run."
        )
    else:
        # ── Cost coverage health banner ───────────────────────────────
        # Three-state breakdown: real supplier cost / estimated (rrp×ratio)
        # / completely missing. Operators need to know at a glance how
        # trustworthy the margin numbers are across the catalog.
        sv_check = suppliers_view.copy()
        _empty = pd.Series("", index=sv_check.index, dtype=str)
        sv_check["_cost_str"] = sv_check["cost_inc"].astype(str).str.strip() if "cost_inc" in sv_check.columns else _empty
        sv_check["_src"] = sv_check["cost_source"].astype(str).str.strip().str.lower() if "cost_source" in sv_check.columns else _empty
        n_total      = len(sv_check)
        n_supplier   = int(((sv_check["_cost_str"] != "") & (sv_check["_src"] == "supplier")).sum())
        n_estimated  = int(((sv_check["_cost_str"] != "") & (sv_check["_src"] == "estimated")).sum())
        # cost present but source missing = legacy real cost from prior runs
        n_legacy     = int(((sv_check["_cost_str"] != "") & (~sv_check["_src"].isin(["supplier", "estimated"]))).sum())
        n_missing    = int((sv_check["_cost_str"] == "").sum())
        n_real       = n_supplier + n_legacy

        # Always-on summary banner (green when complete, amber when gaps)
        complete = (n_missing == 0 and n_estimated == 0)
        banner_color = G if complete else AMBER
        banner_bg = "rgba(0,200,150,.06)" if complete else "rgba(255,170,0,.08)"
        icon = "✓" if complete else "⚠"
        st.markdown(
            f"<div style='background:{banner_bg};border:1px solid {banner_color};"
            f"border-radius:6px;padding:10px 14px;margin-bottom:14px;font-size:.85rem'>"
            f"<b style='color:{banner_color}'>{icon} Cost data:</b> "
            f"<span style='color:{T1}'>"
            f"<b>{n_real}</b> from supplier pricelist  ·  "
            f"<b>{n_estimated}</b> estimated (RRP × ratio)  ·  "
            f"<b>{n_missing}</b> missing"
            f"</span>"
            f"<span style='color:{T3}'>  &nbsp;of {n_total} products total</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Detailed call-out only when there are completely-missing suppliers
        sup_cost_status = (
            sv_check.assign(_has_cost=sv_check["_cost_str"] != "")
                    .groupby("supplier")["_has_cost"].agg(["sum", "count"])
        )
        suppliers_no_cost = sup_cost_status[sup_cost_status["sum"] == 0]
        if not suppliers_no_cost.empty:
            no_cost_names = ", ".join(
                SUPPLIER_LABELS.get(s, s) for s in suppliers_no_cost.index
            )
            st.markdown(
                f"<div style='background:rgba(255,170,0,.04);border:1px solid {BDR};"
                f"border-radius:6px;padding:8px 14px;margin-bottom:14px;font-size:.78rem;"
                f"color:{T3}'>"
                f"<b>Zero-cost suppliers:</b> {no_cost_names}. "
                f"These feeds only expose retail prices — add <code>cost_inc</code> "
                f"manually in master (preserved across syncs), or send the dealer-pricelist "
                f"request templates in <code>docs/supplier_pricelist_requests.md</code>."
                f"</div>",
                unsafe_allow_html=True,
            )
        # Last-refreshed timestamp from the first row (all rows share the value)
        rebuilt_at = ""
        if "rebuilt_at" in suppliers_view.columns and not suppliers_view.empty:
            rebuilt_at = str(suppliers_view["rebuilt_at"].iloc[0])
        if rebuilt_at:
            st.caption(f"View last rebuilt: **{rebuilt_at}**  ·  Source: `master` sheet")

        # Group by supplier for the catalog
        sv = suppliers_view.copy()
        # Numeric coercion for sorting/aggregation only — display stays as strings
        for col in ("cost_inc", "selling_price", "rrp"):
            if col in sv.columns:
                sv[f"_{col}_num"] = pd.to_numeric(sv[col], errors="coerce")

        for sup_key in sorted(sv["supplier"].dropna().unique()):
            grp = sv[sv["supplier"] == sup_key]
            if grp.empty:
                continue
            n_total  = len(grp)
            n_linked = (grp["live_on_site"] == "✓").sum()
            avg_cost = grp.get("_cost_inc_num", pd.Series(dtype=float)).mean()
            avg_sell = grp.get("_selling_price_num", pd.Series(dtype=float)).mean()
            display_name = SUPPLIER_LABELS.get(sup_key, sup_key.replace("_", " ").title())

            header = (
                f"**{display_name}**  ·  "
                f"{n_total} products  ·  {n_linked} live on site"
            )
            if pd.notna(avg_cost) and pd.notna(avg_sell):
                header += f"  ·  avg cost R{avg_cost:,.0f}  ·  avg sell R{avg_sell:,.0f}"

            with st.expander(header, expanded=False):
                # Show the columns that matter, in friendly order
                cols_to_show = [
                    c for c in (
                        "sku", "description", "cost_inc", "cost_source",
                        "selling_price", "margin_pct", "rrp", "stock_status",
                        "stock_qty", "live_on_site", "shopify_variant_id",
                        "source", "last_updated",
                    ) if c in grp.columns
                ]
                display_df = grp[cols_to_show].copy()

                # Decorate the cost_source column with friendly tags so the
                # operator can scan which costs are real (from a supplier
                # pricelist) vs estimated (rrp × ratio) at a glance.
                if "cost_source" in display_df.columns:
                    def _tag(v: str) -> str:
                        v = (v or "").strip().lower()
                        if v == "supplier":  return "✓ supplier"
                        if v == "estimated": return "≈ estimated"
                        return "—"
                    display_df["cost_source"] = display_df["cost_source"].astype(str).map(_tag)

                display_df = display_df.rename(columns={
                    "sku": "SKU",
                    "description": "Description",
                    "cost_inc": "Cost (incl)",
                    "cost_source": "Cost source",
                    "selling_price": "Selling price",
                    "margin_pct": "Margin",
                    "rrp": "RRP",
                    "stock_status": "Stock",
                    "stock_qty": "Qty",
                    "live_on_site": "Live",
                    "shopify_variant_id": "Variant ID",
                    "source": "Source",
                    "last_updated": "Last updated",
                })
                st.dataframe(
                    display_df, use_container_width=True, hide_index=True,
                )


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

        fa, fb = st.columns(2)
        with fa:
            sopts = ["All"] + sorted(m["_sup"].dropna().unique())
            sf = st.selectbox("Supplier", sopts, key="ms")
        with fb:
            wf = st.selectbox("On website", ["All","✓ Yes","✗ No"], key="mw")
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 6 — Price Sync
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with t6:
    # ── Summary KPIs from latest run ──────────────────────────────────
    st.markdown('<div class="slabel">Last run summary</div>', unsafe_allow_html=True)

    psl = price_sync_log.copy() if not price_sync_log.empty else pd.DataFrame()

    if not psl.empty and "timestamp" in psl.columns:
        psl["_ts"] = pd.to_datetime(psl["timestamp"], errors="coerce")
        last_ts = psl["_ts"].dropna().max()
        last_run_rows = psl[psl["_ts"] == last_ts] if pd.notna(last_ts) else psl.head(0)

        total_processed = len(last_run_rows)
        n_applied       = (last_run_rows["applied"].astype(str).str.lower() == "true").sum() if "applied" in last_run_rows.columns else 0
        n_skipped       = (
            last_run_rows[
                (last_run_rows["applied"].astype(str).str.lower() == "false") &
                (last_run_rows["skip_reason"].astype(str).str.lower().str.contains("threshold", na=False))
            ].shape[0]
            if "skip_reason" in last_run_rows.columns else 0
        )
        n_errors        = (
            last_run_rows[
                (last_run_rows["applied"].astype(str).str.lower() == "false") &
                (last_run_rows["skip_reason"].astype(str).str.lower().str.contains("alert", na=False))
            ].shape[0]
            if "skip_reason" in last_run_rows.columns else 0
        )
        last_run_str = last_ts.strftime("%d %b %Y  %H:%M UTC") if pd.notna(last_ts) else "—"
    else:
        total_processed = n_applied = n_skipped = n_errors = 0
        last_run_str = "No runs yet"

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Last Run", last_run_str)
    m2.metric("Processed", total_processed)
    m3.metric("Applied", n_applied, help="Price changes pushed to Shopify or written to master sheet")
    m4.metric("Skipped", n_skipped, help="Price increases below the 2% threshold — not written")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Manual trigger button ─────────────────────────────────────────
    st.markdown('<div class="slabel">Manual trigger</div>', unsafe_allow_html=True)

    _sync_running, _sync_status = _is_running("price_sync")

    col_btn, col_out = st.columns([2, 5])
    with col_btn:
        if st.button(
            "⏸ Running…" if _sync_running else "▶ Trigger Sync Now",
            use_container_width=True,
            disabled=_sync_running,
        ) and not _sync_running:
            try:
                sync_log = ROOT / "price_sync.log"
                proc = subprocess.Popen(
                    [sys.executable, "-m", "src.main", "--trigger=manual"],
                    cwd=str(ROOT),
                    env=_subprocess_env(),
                    stdout=open(sync_log, "w"),
                    stderr=subprocess.STDOUT,
                )
                with col_out:
                    st.info(f"**Sync started** (PID {proc.pid})  ·  log: `{sync_log}`")
            except Exception as exc:
                st.error(f"Failed to start sync: {exc}")

    _sync_progress_fragment()

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Price sync log table ──────────────────────────────────────────
    st.markdown('<div class="slabel">Price sync log</div>', unsafe_allow_html=True)

    if psl.empty:
        st.info("No price sync log data yet. The log is populated on each sync run.")
    else:
        # Filters
        fa, fb, fc = st.columns(3)
        with fa:
            sup_opts_psl = ["All suppliers"] + sorted(
                psl["supplier"].dropna().unique().tolist()
                if "supplier" in psl.columns else []
            )
            psl_sup = st.selectbox("Supplier", sup_opts_psl, key="psl_s",
                                    format_func=lambda x: SUPPLIER_LABELS.get(x, x))
        with fb:
            dir_opts = ["All", "increase", "decrease", "unchanged", "new"]
            psl_dir = st.selectbox("Direction", dir_opts, key="psl_d")
        with fc:
            app_opts = ["All", "Applied", "Skipped / Not applied"]
            psl_app = st.selectbox("Status", app_opts, key="psl_a")

        fd, fe = st.columns(2)
        with fd:
            days_psl = st.selectbox("Period", [1, 7, 14, 30], key="psl_p",
                                     format_func=lambda x: f"Last {x} day{'s' if x>1 else ''}")
        with fe:
            psl_q = st.text_input("Search SKU / product", placeholder="MD60F …", key="psl_q")

        # Apply filters
        psl_f = psl.copy()
        if "_ts" not in psl_f.columns:
            psl_f["_ts"] = pd.to_datetime(psl_f.get("timestamp", pd.Series()), errors="coerce")

        cutoff_psl = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_psl)
        msk_psl = psl_f["_ts"].dt.tz_localize("UTC", ambiguous="NaT", nonexistent="NaT").fillna(
            psl_f["_ts"]
        ) >= cutoff_psl

        if psl_sup != "All suppliers" and "supplier" in psl_f.columns:
            msk_psl &= psl_f["supplier"] == psl_sup
        if psl_dir != "All" and "direction" in psl_f.columns:
            msk_psl &= psl_f["direction"].astype(str).str.lower() == psl_dir
        if psl_app == "Applied" and "applied" in psl_f.columns:
            msk_psl &= psl_f["applied"].astype(str).str.lower() == "true"
        elif psl_app == "Skipped / Not applied" and "applied" in psl_f.columns:
            msk_psl &= psl_f["applied"].astype(str).str.lower() == "false"
        if psl_q and "sku" in psl_f.columns:
            sq_lower = psl_q.lower()
            msk_psl &= (
                psl_f["sku"].astype(str).str.lower().str.contains(sq_lower, na=False) |
                psl_f.get("product_name", pd.Series("", index=psl_f.index)).astype(str).str.lower().str.contains(sq_lower, na=False)
            )

        psl_filtered = psl_f[msk_psl].copy()

        if psl_filtered.empty:
            st.markdown(f"""
            <div class="card" style="text-align:center;padding:24px;margin-top:8px">
              <div style="color:{T3};font-size:.83rem">No log entries match these filters</div>
            </div>""", unsafe_allow_html=True)
        else:
            # Select display columns
            disp_cols = [c for c in [
                "timestamp", "sku", "product_name",
                "old_supplier_price", "new_supplier_price", "pct_change",
                "direction", "applied", "skip_reason",
            ] if c in psl_filtered.columns]

            disp_psl = psl_filtered[disp_cols].copy()
            if "timestamp" in disp_psl.columns:
                disp_psl["timestamp"] = pd.to_datetime(
                    disp_psl["timestamp"], errors="coerce"
                ).dt.strftime("%d %b %Y  %H:%M")
            disp_psl = disp_psl.sort_values("timestamp", ascending=False) if "timestamp" in disp_psl.columns else disp_psl

            def _psl_row_style(row):
                s = [""] * len(row)
                idx = list(row.index)
                applied_val = str(row.get("applied", "")).lower()
                skip_reason = str(row.get("skip_reason", "")).lower()

                if applied_val == "false":
                    # Errors (alerts) → red tint; skipped (threshold / unchanged) → amber tint
                    bg = f"background-color:rgba(240,92,110,.08)" if "alert" in skip_reason else f"background-color:rgba(240,168,74,.07)"
                    s = [bg] * len(s)

                # Colour the applied cell itself
                if "applied" in idx:
                    ai = idx.index("applied")
                    if applied_val == "true":
                        s[ai] = f"color:{G};font-weight:600"
                    else:
                        colour = RED if "alert" in skip_reason else AMBER
                        s[ai] = f"color:{colour};font-weight:600"

                # Colour pct_change cell
                if "pct_change" in idx:
                    pi = idx.index("pct_change")
                    pct_str = str(row.get("pct_change", ""))
                    if pct_str.startswith("+") or (pct_str and pct_str[0].isdigit() and float(pct_str.replace("%","") or 0) > 0):
                        s[pi] = f"color:{RED};font-family:{MONO};font-size:.78rem"
                    elif pct_str.startswith("-"):
                        s[pi] = f"color:{G};font-family:{MONO};font-size:.78rem"
                    else:
                        s[pi] = f"font-family:{MONO};font-size:.78rem"

                return s

            st.dataframe(
                disp_psl.style.apply(_psl_row_style, axis=1).set_properties(
                    **{"background-color": C1, "color": T1, "font-size": "0.81rem"}
                ),
                use_container_width=True,
                height=480,
                hide_index=True,
            )
            st.caption(
                f"{len(psl_filtered):,} entries shown  ·  "
                f"green = applied  ·  amber = skipped (below threshold or unchanged)  ·  red = held for review"
            )


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities from a string."""
    import re, html
    clean = re.sub(r"<[^>]+>", " ", str(text))
    clean = html.unescape(clean)
    return " ".join(clean.split())   # collapse whitespace


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 7 — Competitor Analysis
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with t7:
    cal = comp_analysis_log.copy() if not comp_analysis_log.empty else pd.DataFrame()

    # ── Summary KPIs ──────────────────────────────────────────────────
    st.markdown('<div class="slabel">Last run summary</div>', unsafe_allow_html=True)

    if not cal.empty and "timestamp" in cal.columns:
        cal["_ts"] = pd.to_datetime(cal["timestamp"], errors="coerce")
        last_cal_ts   = cal["_ts"].dropna().max()
        last_cal_rows = cal[cal["_ts"] == last_cal_ts] if pd.notna(last_cal_ts) else cal.head(0)
        last_cal_str  = last_cal_ts.strftime("%d %b %Y  %H:%M UTC") if pd.notna(last_cal_ts) else "—"

        def _count_status(df, *statuses):
            if "status" not in df.columns:
                return 0
            return int(df["status"].isin(list(statuses)).sum())

        n_analysed   = len(last_cal_rows)
        n_pending    = _count_status(last_cal_rows, "PENDING_REVIEW", "MARGIN_FLOOR_HIT")
        n_comp       = _count_status(last_cal_rows, "ALREADY_COMPETITIVE")
        n_no_match   = _count_status(last_cal_rows, "NO_MATCH_FOUND", "SCRAPE_FAILED")

        # discrepancies: rows where cfsa > cheapest_competitor
        n_disc = 0
        if "discrepancy_rand" in last_cal_rows.columns:
            n_disc = (
                pd.to_numeric(last_cal_rows["discrepancy_rand"], errors="coerce")
                .fillna(0) > 0
            ).sum()
    else:
        last_cal_str = "No runs yet"
        n_analysed = n_pending = n_comp = n_no_match = n_disc = 0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Last Run",           last_cal_str)
    m2.metric("Analysed",           n_analysed)
    m3.metric("Discrepancies",      n_disc,    help="CFSA price > cheapest competitor")
    m4.metric("Pending Review",     n_pending, help="Needs human approval before Shopify update")
    m5.metric("Already Competitive",n_comp)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Manual trigger ────────────────────────────────────────────────
    st.markdown('<div class="slabel">Manual trigger</div>', unsafe_allow_html=True)

    _ca_running, _ca_status = _is_running("competitor_analysis")

    cb_btn, cb_out = st.columns([2, 5])
    with cb_btn:
        if st.button(
            "⏸ Running…" if _ca_running else "▶ Run Analysis Now",
            use_container_width=True,
            key="ca_trigger",
            disabled=_ca_running,
        ) and not _ca_running:
            try:
                log_file = ROOT / "competitor_analysis.log"
                proc = subprocess.Popen(
                    [sys.executable, "-m", "scrapers.competitor_analysis.main"],
                    cwd=str(ROOT),
                    env=_subprocess_env(),
                    stdout=open(log_file, "w"),
                    stderr=subprocess.STDOUT,
                )
                with cb_out:
                    st.info(
                        f"**Analysis started** (PID {proc.pid}) — typically 5–10 min  \n"
                        f"Log: `{log_file}`"
                    )
            except Exception as exc:
                st.error(f"Failed to start analysis: {exc}")

    _ca_progress_fragment()

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Helper: write-capable gspread client ──────────────────────────
    def _write_gspread():
        if "gcp_service_account" in st.secrets:
            creds = Credentials.from_service_account_info(
                dict(st.secrets["gcp_service_account"]),
                scopes=WRITE_SCOPES,
            )
        else:
            creds = Credentials.from_service_account_file(str(SA_KEY), scopes=WRITE_SCOPES)
        return gspread.authorize(creds)

    def _shopify_client():
        """Lazy-load ShopifyClient from repo src — only called on approval."""
        from src.shopify_client import ShopifyClient
        domain = st.secrets.get("shopify_shop_domain", "") or os.getenv("SHOPIFY_SHOP_DOMAIN", "")
        token  = st.secrets.get("shopify_access_token", "") or os.getenv("SHOPIFY_ACCESS_TOKEN", "")
        if not domain or not token:
            return None
        return ShopifyClient(shop_domain=domain, access_token=token)

    # ── Pending review banner (full UI lives in t8) ──────────────────
    if not cal.empty and "status" in cal.columns:
        _cal_pending_count = int(
            cal.sort_values("_ts", ascending=False)
               .drop_duplicates(subset=["sku"], keep="first")["status"]
               .isin(["PENDING_REVIEW", "MARGIN_FLOOR_HIT"])
               .sum()
        ) if "_ts" in cal.columns and "sku" in cal.columns else 0
        if _cal_pending_count:
            st.markdown(
                f"<div style='background:rgba(240,168,74,.08);border:1px solid {AMBER}33;"
                f"border-radius:8px;padding:12px 16px;margin-bottom:16px;display:flex;"
                f"align-items:center;gap:12px'>"
                f"<span style='font-size:1.2rem'>⚠</span>"
                f"<span style='color:{T2};font-size:.88rem'>"
                f"<b style='color:{AMBER}'>{_cal_pending_count} product{'s' if _cal_pending_count!=1 else ''} held for review.</b> "
                f"Open the <b>Held for Review</b> tab to approve or reject price changes.</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div style='background:{G_10};border:1px solid {G}33;"
                f"border-radius:8px;padding:10px 16px;margin-bottom:16px'>"
                f"<span style='color:{G};font-size:.88rem'>✅ No items pending review</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── All Results table ─────────────────────────────────────────────
    st.markdown('<div class="slabel">All results — full history</div>', unsafe_allow_html=True)

    if cal.empty:
        st.info("No competitor analysis history yet.")
    else:
        # Filters
        fa, fb, fc = st.columns(3)
        with fa:
            status_opts = ["All statuses"] + sorted(cal["status"].dropna().unique().tolist()) if "status" in cal.columns else ["All statuses"]
            cal_status  = st.selectbox("Status", status_opts, key="cal_st")
        with fb:
            days_cal = st.selectbox("Period", [1, 7, 30, 90], key="cal_d",
                                     format_func=lambda x: f"Last {x} day{'s' if x>1 else ''}")
        with fc:
            cal_q = st.text_input("Search SKU", placeholder="MD60F …", key="cal_q")

        cal_f = cal.copy()
        if "_ts" not in cal_f.columns:
            cal_f["_ts"] = pd.to_datetime(cal_f.get("timestamp", pd.Series()), errors="coerce")
        cutoff_cal = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_cal)

        try:
            ts_aware = cal_f["_ts"].dt.tz_localize("UTC", ambiguous="NaT", nonexistent="NaT")
        except Exception:
            ts_aware = cal_f["_ts"]

        msk_cal = ts_aware.fillna(pd.NaT) >= cutoff_cal
        if cal_status != "All statuses" and "status" in cal_f.columns:
            msk_cal &= cal_f["status"] == cal_status
        if cal_q and "sku" in cal_f.columns:
            msk_cal &= cal_f["sku"].astype(str).str.lower().str.contains(cal_q.lower(), na=False)

        cal_filtered = cal_f[msk_cal].copy()

        if cal_filtered.empty:
            st.markdown(f"""
            <div class="card" style="text-align:center;padding:24px">
              <div style="color:{T3};font-size:.83rem">No results match these filters</div>
            </div>""", unsafe_allow_html=True)
        else:
            # Columns to show in the table
            all_cols = (
                ["timestamp", "sku", "product_name", "cfsa_current_price",
                 "cost_price", "margin_pct"]
                + COMP_PRICE_COLS
                + ["cheapest_competitor", "cheapest_source", "discrepancy_rand",
                   "ai_suggested_price", "human_override_price", "status"]
            )
            show_cols = [c for c in all_cols if c in cal_filtered.columns]
            cal_display = cal_filtered[show_cols].copy()
            if "timestamp" in cal_display.columns:
                cal_display["timestamp"] = pd.to_datetime(
                    cal_display["timestamp"], errors="coerce"
                ).dt.strftime("%d %b %Y  %H:%M")
            cal_display = cal_display.sort_values("timestamp", ascending=False) if "timestamp" in cal_display.columns else cal_display

            # Rename competitor price cols for display
            rename_map = {f"{c['name']}_price": c.get("display_name", c["name"]) for c in COMPETITORS}
            cal_display.rename(columns=rename_map, inplace=True)

            def _cal_row_style(row):
                s = [""] * len(row)
                idx = list(row.index)
                status_v = str(row.get("status", ""))
                if status_v in ("PENDING_REVIEW", "MARGIN_FLOOR_HIT"):
                    s = [f"background-color:rgba(240,168,74,.07)"] * len(s)
                elif status_v == "APPROVED":
                    s = [f"background-color:rgba(0,232,122,.05)"] * len(s)
                elif status_v == "REJECTED":
                    s = [f"background-color:rgba(240,92,110,.06)"] * len(s)
                if "status" in idx:
                    si = idx.index("status")
                    colour_map = {
                        "PENDING_REVIEW": AMBER, "MARGIN_FLOOR_HIT": AMBER,
                        "APPROVED": G, "ALREADY_COMPETITIVE": G,
                        "REJECTED": RED, "SCRAPE_FAILED": RED,
                        "NO_MATCH_FOUND": T3,
                    }
                    s[si] = f"color:{colour_map.get(status_v, T2)};font-weight:600"
                if "discrepancy_rand" in idx:
                    di = idx.index("discrepancy_rand")
                    try:
                        dv = float(str(row.get("discrepancy_rand", "")).replace(",", ""))
                        col = RED if dv > 500 else (AMBER if dv > 100 else (G if dv < 0 else T2))
                        s[di] = f"color:{col};font-family:{MONO};font-size:.78rem"
                    except (ValueError, TypeError):
                        pass
                return s

            st.dataframe(
                cal_display.style.apply(_cal_row_style, axis=1).set_properties(
                    **{"background-color": C1, "color": T1, "font-size": "0.8rem"}
                ),
                use_container_width=True,
                height=480,
                hide_index=True,
            )
            st.caption(
                f"{len(cal_filtered):,} entries  ·  "
                f"amber = pending review  ·  green = approved / competitive  ·  red = rejected / failed"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 8 — Held for Review
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with t8:
    cal8 = comp_analysis_log.copy() if not comp_analysis_log.empty else pd.DataFrame()

    if not cal8.empty and "timestamp" in cal8.columns:
        cal8["_ts"] = pd.to_datetime(cal8["timestamp"], errors="coerce")

    def _write_gspread8():
        if "gcp_service_account" in st.secrets:
            creds8 = Credentials.from_service_account_info(
                dict(st.secrets["gcp_service_account"]), scopes=WRITE_SCOPES)
        else:
            creds8 = Credentials.from_service_account_file(str(SA_KEY), scopes=WRITE_SCOPES)
        return gspread.authorize(creds8)

    def _shopify_client8():
        from src.shopify_client import ShopifyClient
        domain = st.secrets.get("shopify_shop_domain", "") or os.getenv("SHOPIFY_SHOP_DOMAIN", "")
        token  = st.secrets.get("shopify_access_token", "") or os.getenv("SHOPIFY_ACCESS_TOKEN", "")
        if not domain or not token:
            return None
        return ShopifyClient(shop_domain=domain, access_token=token)

    if cal8.empty or "status" not in cal8.columns:
        st.markdown(f"""
        <div class="card" style="text-align:center;padding:40px">
          <div style="font-size:1.4rem;margin-bottom:10px">📭</div>
          <div style="color:{T2};font-weight:600;margin-bottom:6px">No competitor analysis data yet</div>
          <div style="color:{T3};font-size:.85rem">Run the competitor analysis from the <b>Competitor Analysis</b> tab first.</div>
        </div>""", unsafe_allow_html=True)
    else:
        # Most recent status per SKU
        cal8_latest = cal8.copy()
        if "_ts" in cal8_latest.columns and "sku" in cal8_latest.columns:
            cal8_latest = (
                cal8_latest.sort_values("_ts", ascending=False)
                           .drop_duplicates(subset=["sku"], keep="first")
            )
        pending8 = cal8_latest[
            cal8_latest["status"].isin(["PENDING_REVIEW", "MARGIN_FLOOR_HIT"])
        ].copy()

        if not pending8.empty and "_ts" in pending8.columns:
            pending8 = pending8.sort_values(["status", "discrepancy_rand"], ascending=[True, False])

        # ── Cost-data warning ─────────────────────────────────────────
        if not pending8.empty and "cost_price" in pending8.columns:
            no_cost8 = pending8["cost_price"].astype(str).str.strip().eq("")
            n_missing8 = int(no_cost8.sum())
            if n_missing8 > 0:
                st.markdown(
                    f"<div style='background:rgba(255,170,0,.08);border:1px solid {AMBER};"
                    f"border-radius:6px;padding:10px 14px;margin-bottom:14px;font-size:.85rem'>"
                    f"<b style='color:{AMBER}'>⚠ {n_missing8} of {len(pending8)} products lack wholesale cost data.</b> "
                    f"<span style='color:{T3}'>Margin floor cannot be enforced. Add <code>cost_inc</code> in the master sheet before approving.</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

        if pending8.empty:
            st.markdown(f"""
            <div class="card" style="text-align:center;padding:40px;border-color:{G_10}">
              <div style="font-size:1.4rem;margin-bottom:8px">✅</div>
              <div style="color:{T2};font-weight:600">All clear — no items held for review</div>
              <div style="color:{T3};font-size:.83rem;margin-top:6px">
                Products appear here when a competitor undercuts CFSA and a decision is needed.
              </div>
            </div>""", unsafe_allow_html=True)
        else:
            # Section headers
            _section_shown = set()

            for _, row8 in pending8.iterrows():
                sku8          = str(row8.get("sku", ""))
                product_name8 = _strip_html(str(row8.get("product_name", "")))
                cfsa_price8   = row8.get("cfsa_current_price", "")
                cost_price8   = row8.get("cost_price", "")
                margin8       = row8.get("margin_pct", "")
                ai_suggested8 = row8.get("ai_suggested_price", "")
                disc_raw8     = row8.get("discrepancy_rand", "")
                status_val8   = str(row8.get("status", ""))
                run_id8       = str(row8.get("run_id", ""))
                variant_id8   = str(row8.get("shopify_variant_id", "")) if "shopify_variant_id" in row8 else ""
                cost_source8  = str(row8.get("cost_source", "") or "").strip()

                # Section divider
                if status_val8 not in _section_shown:
                    _section_shown.add(status_val8)
                    if status_val8 == "PENDING_REVIEW":
                        st.markdown(
                            f"<div style='font-size:.7rem;font-weight:700;letter-spacing:.1em;"
                            f"text-transform:uppercase;color:{AMBER};margin:18px 0 10px'>"
                            f"⚠ Pending Review — competitor is cheaper, suggest reducing price</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            f"<div style='font-size:.7rem;font-weight:700;letter-spacing:.1em;"
                            f"text-transform:uppercase;color:{RED};margin:18px 0 10px'>"
                            f"🚫 Margin Floor Hit — cannot match without going below cost floor</div>",
                            unsafe_allow_html=True,
                        )

                try:
                    disc_val8 = float(str(disc_raw8).replace(",", ""))
                    disc_colour8 = RED if disc_val8 > 500 else (AMBER if disc_val8 > 100 else G)
                    disc_str8 = f"R{disc_val8:,.0f}"
                except (ValueError, TypeError):
                    disc_colour8 = T3
                    disc_str8 = "—"

                floor_badge8 = (
                    f"<span class='badge' style='background:rgba(240,168,74,.1);"
                    f"color:{AMBER};border:1px solid rgba(240,168,74,.2);margin-left:8px'>"
                    f"MARGIN FLOOR</span>"
                    if status_val8 == "MARGIN_FLOOR_HIT" else ""
                )

                row_key8 = f"t8_{run_id8}_{sku8}"

                with st.container():
                    col_info8, col_disc8 = st.columns([3, 1])
                    with col_info8:
                        st.markdown(
                            f"<div style='font-family:{MONO};font-size:.95rem;font-weight:600;"
                            f"color:{T1}'>{sku8}{floor_badge8}</div>"
                            f"<div style='font-size:.82rem;color:{T2};margin-top:2px;"
                            f"margin-bottom:6px'>{product_name8}</div>",
                            unsafe_allow_html=True,
                        )
                    with col_disc8:
                        st.markdown(
                            f"<div style='text-align:right'>"
                            f"<div style='font-size:.62rem;color:{T3};text-transform:uppercase;"
                            f"letter-spacing:.08em'>Discrepancy</div>"
                            f"<div style='font-size:1.05rem;font-weight:700;font-family:{MONO};"
                            f"color:{disc_colour8}'>{disc_str8}</div></div>",
                            unsafe_allow_html=True,
                        )

                    # Competitor price chips
                    chip_cols8 = st.columns(len(COMPETITORS))
                    for ci8, comp8 in enumerate(COMPETITORS):
                        col_key8 = f"{comp8['name']}_price"
                        val8 = str(row8.get(col_key8, "")) if col_key8 in row8.index else ""
                        price_str8 = f"R{val8}" if val8 else "—"
                        price_colour8 = G if val8 else T3
                        with chip_cols8[ci8]:
                            st.markdown(
                                f"<div style='background:{C2};border-radius:6px;padding:5px 8px;"
                                f"text-align:center'>"
                                f"<div style='font-size:.6rem;color:{T3};text-transform:uppercase;"
                                f"letter-spacing:.06em;margin-bottom:2px'>{COMP_DISPLAY[comp8['name']]}</div>"
                                f"<div style='font-family:{MONO};font-size:.8rem;font-weight:600;"
                                f"color:{price_colour8}'>{price_str8}</div></div>",
                                unsafe_allow_html=True,
                            )

                    # Price summary
                    if cost_price8 and cost_source8 == "estimated":
                        cost_html8 = (
                            f"<span>Cost&nbsp;<b style='color:{T1};font-family:{MONO}'>R{cost_price8}</b>"
                            f"&nbsp;<span style='color:{AMBER};font-size:.7rem'>(est.)</span></span>"
                        )
                    elif cost_price8:
                        cost_html8 = f"<span>Cost&nbsp;<b style='color:{T1};font-family:{MONO}'>R{cost_price8}</b></span>"
                    else:
                        cost_html8 = f"<span style='color:{AMBER}'>⚠ Cost not in master</span>"

                    margin_html8 = (
                        f"<span>Margin&nbsp;<b style='color:{T1}'>{margin8}</b></span>"
                        if margin8 else f"<span style='color:{AMBER}'>Margin unknown</span>"
                    )

                    st.markdown(
                        f"<div style='display:flex;gap:24px;font-size:.75rem;color:{T2};"
                        f"margin-top:8px;margin-bottom:4px;padding:8px 0;border-top:1px solid {BDR}'>"
                        f"<span>CFSA&nbsp;<b style='color:{T1};font-family:{MONO}'>R{cfsa_price8}</b></span>"
                        f"{cost_html8}{margin_html8}"
                        f"<span>AI Suggested&nbsp;<b style='color:{G};font-family:{MONO}'>"
                        f"{'R'+str(ai_suggested8) if ai_suggested8 else '—'}</b></span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"<div style='height:1px;background:{BDR};margin-bottom:8px'></div>",
                                unsafe_allow_html=True)

                    col_inp8, col_approve8, col_reject8 = st.columns([3, 1, 1])
                    with col_inp8:
                        try:
                            default_val8 = float(str(ai_suggested8).replace(",", "")) if ai_suggested8 else 0.0
                        except ValueError:
                            default_val8 = 0.0
                        override8 = st.number_input(
                            "Override price (R)",
                            value=default_val8,
                            min_value=0.0,
                            step=10.0,
                            format="%.2f",
                            key=f"ca_override_{row_key8}",
                            label_visibility="collapsed",
                        )

                    with col_approve8:
                        if st.button("✓ Approve", key=f"ca_approve_{row_key8}", use_container_width=True):
                            try:
                                cfsa_p8 = float(row8.get("cfsa_current_price", 0) or 0)
                            except (TypeError, ValueError):
                                cfsa_p8 = 0.0
                            err8: Optional[str] = None
                            if override8 <= 0:
                                err8 = "Override price must be greater than 0"
                            elif override8 > 1_000_000:
                                err8 = f"R{override8:,.2f} looks like a typo — refusing to push"
                            elif cfsa_p8 > 0 and (override8 > cfsa_p8 * 3 or override8 < cfsa_p8 * 0.3):
                                err8 = (
                                    f"R{override8:,.2f} is wildly off current price R{cfsa_p8:,.2f}. "
                                    "If intentional, update Shopify manually first."
                                )
                            elif variant_id8 and not variant_id8.isdigit():
                                err8 = f"Variant ID '{variant_id8}' doesn't look valid. Refusing to push."
                            if err8:
                                st.error(f"✗ {err8}")
                                st.stop()
                            try:
                                gc_write8 = _write_gspread8()
                                sh_write8 = gc_write8.open_by_key(SPREADSHEET_ID)
                                now_iso8  = datetime.now(timezone.utc).isoformat()

                                # ── Update competitor_analysis_log ────────
                                ws_cal8  = sh_write8.worksheet("competitor_analysis_log")
                                cal_vals = ws_cal8.get_all_values()
                                cal_hdr  = cal_vals[0] if cal_vals else []
                                run_col8 = cal_hdr.index("run_id") if "run_id" in cal_hdr else -1
                                sku_col8 = cal_hdr.index("sku")    if "sku"    in cal_hdr else -1
                                for i8, r8 in enumerate(cal_vals[1:], start=2):
                                    if (run_col8 >= 0 and len(r8) > run_col8 and r8[run_col8] == run_id8
                                            and sku_col8 >= 0 and len(r8) > sku_col8 and r8[sku_col8] == sku8):
                                        upd8 = []
                                        for col_name8, val8 in [
                                            ("human_override_price", f"{override8:.2f}"),
                                            ("status", "APPROVED"),
                                            ("approved_by", "Brent"),
                                            ("applied_at", now_iso8),
                                        ]:
                                            if col_name8 in cal_hdr:
                                                upd8.append(gspread.Cell(i8, cal_hdr.index(col_name8) + 1, val8))
                                        if upd8:
                                            ws_cal8.update_cells(upd8, value_input_option="USER_ENTERED")
                                        break

                                # ── Update master sheet selling_price ─────
                                ws_master8 = sh_write8.worksheet("master")
                                mst_vals   = ws_master8.get_all_values()
                                mst_hdr    = mst_vals[0] if mst_vals else []
                                mst_sku_c  = mst_hdr.index("sku")           if "sku"           in mst_hdr else -1
                                mst_prc_c  = mst_hdr.index("selling_price") if "selling_price" in mst_hdr else -1
                                if mst_sku_c >= 0 and mst_prc_c >= 0:
                                    for mi8, mr8 in enumerate(mst_vals[1:], start=2):
                                        if len(mr8) > mst_sku_c and mr8[mst_sku_c].strip() == sku8:
                                            ws_master8.update_cell(mi8, mst_prc_c + 1, f"{override8:.2f}")
                                            break

                                # ── Push to Shopify ───────────────────────
                                if variant_id8:
                                    shopify8 = _shopify_client8()
                                    if shopify8 and override8 > 0:
                                        shopify8.update_variant_price(variant_id8, override8)
                                        st.success(f"✓ {sku8} approved at R{override8:,.2f} — Shopify + master sheet updated")
                                    else:
                                        st.warning(f"✓ {sku8} approved in sheet but Shopify credentials not configured")
                                else:
                                    st.success(f"✓ {sku8} approved at R{override8:,.2f} — master sheet updated (no Shopify variant linked)")

                                st.cache_data.clear()
                            except Exception as exc8:
                                st.error(f"Approval failed: {exc8}")

                    with col_reject8:
                        if st.button("✕ Reject", key=f"ca_reject_{row_key8}", use_container_width=True):
                            try:
                                gc_write8 = _write_gspread8()
                                sh_write8 = gc_write8.open_by_key(SPREADSHEET_ID)
                                ws_cal8   = sh_write8.worksheet("competitor_analysis_log")
                                cal_vals  = ws_cal8.get_all_values()
                                cal_hdr   = cal_vals[0] if cal_vals else []
                                run_col8  = cal_hdr.index("run_id") if "run_id" in cal_hdr else -1
                                sku_col8  = cal_hdr.index("sku")    if "sku"    in cal_hdr else -1
                                now_iso8  = datetime.now(timezone.utc).isoformat()

                                for i8, r8 in enumerate(cal_vals[1:], start=2):
                                    if (run_col8 >= 0 and len(r8) > run_col8 and r8[run_col8] == run_id8
                                            and sku_col8 >= 0 and len(r8) > sku_col8 and r8[sku_col8] == sku8):
                                        upd8 = []
                                        for col_name8, val8 in [
                                            ("status", "REJECTED"),
                                            ("approved_by", "Brent"),
                                            ("applied_at", now_iso8),
                                        ]:
                                            if col_name8 in cal_hdr:
                                                upd8.append(gspread.Cell(i8, cal_hdr.index(col_name8) + 1, val8))
                                        if upd8:
                                            ws_cal8.update_cells(upd8, value_input_option="USER_ENTERED")
                                        break

                                st.info(f"✕ {sku8} rejected — no price changes made")
                                st.cache_data.clear()
                            except Exception as exc8:
                                st.error(f"Rejection failed: {exc8}")

                st.markdown("<div style='margin-bottom:4px'></div>", unsafe_allow_html=True)


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
