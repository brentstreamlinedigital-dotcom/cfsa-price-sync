"""
Google Sheets logger for the competitor_analysis_log tab.

Responsibilities
────────────────
• Append one row per product per run (never overwrites history).
• Read rows pending review for the dashboard.
• Update a specific row when a human approves, rejects, or overrides a price.

The tab is created automatically if it doesn't exist.
Column order follows the spec exactly; additional competitor columns are
derived from the competitors list sorted by priority.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import gspread
import pandas as pd
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials

log = logging.getLogger(__name__)

# Columns that are always present regardless of which competitors are configured
_FIXED_PREFIX_HEADERS = [
    "timestamp",
    "run_id",
    "sku",
    "product_name",
    "cfsa_current_price",
    "cost_price",
    "cost_source",    # "supplier" | "estimated" | "" (unknown)
    "margin_pct",
]
_FIXED_SUFFIX_HEADERS = [
    "cheapest_competitor",
    "cheapest_source",
    "discrepancy_rand",
    "ai_suggested_price",
    "human_override_price",
    "status",
    "approved_by",
    "applied_at",
    "shopify_variant_id",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
READONLY_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]


def _competitor_price_col(name: str) -> str:
    """Convert competitor name → sheet column name. e.g. 'safari_centre_ct' → 'safari_centre_ct_price'"""
    return f"{name}_price"


def _col_letter(n: int) -> str:
    """Convert a 1-based column index → A1 letter (1→A, 26→Z, 27→AA)."""
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def build_headers(competitors: list[dict]) -> list[str]:
    """
    Build the full ordered header list for competitor_analysis_log.
    Competitor columns sit between the fixed prefix and suffix, sorted by priority.
    """
    comp_cols = [_competitor_price_col(c["name"]) for c in competitors]
    return _FIXED_PREFIX_HEADERS + comp_cols + _FIXED_SUFFIX_HEADERS


class CompetitorSheetsLogger:
    """
    Reads and writes the competitor_analysis_log tab in the CFSA Master spreadsheet.

    Pass ``readonly=True`` to get a read-only client (used by the dashboard
    data-loading path).  The approval path requires the default write client.
    """

    TAB_NAME = "competitor_analysis_log"

    def __init__(
        self,
        spreadsheet_id: str,
        competitors: list[dict],
        service_account_file: Optional[str] = None,
        credentials: Optional[Credentials] = None,
        readonly: bool = False,
    ):
        self.spreadsheet_id = spreadsheet_id
        self.headers = build_headers(competitors)
        self.competitors = competitors

        scopes = READONLY_SCOPES if readonly else SCOPES

        if credentials:
            gc = gspread.authorize(credentials)
        elif service_account_file:
            gc = gspread.service_account(filename=service_account_file, scopes=scopes)
        else:
            import google.auth
            creds, _ = google.auth.default(scopes=scopes)
            gc = gspread.authorize(creds)

        self._spreadsheet = gc.open_by_key(spreadsheet_id)
        self._ws: Optional[gspread.Worksheet] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_ws(self) -> gspread.Worksheet:
        if self._ws is not None:
            return self._ws
        try:
            ws = self._spreadsheet.worksheet(self.TAB_NAME)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(
                title=self.TAB_NAME,
                rows=10_000,
                cols=len(self.headers) + 5,  # +5 buffer for future columns
            )
            ws.append_row(self.headers, value_input_option="USER_ENTERED")
            log.info("Created worksheet: %s", self.TAB_NAME)
        self._ws = ws
        return ws

    def _row_to_dict(self, header: list[str], row: list) -> dict:
        """Convert a raw gspread row list into a dict aligned to the header."""
        padded = row + [""] * (len(header) - len(row))
        return dict(zip(header, padded))

    # ------------------------------------------------------------------
    # Write — append run results
    # ------------------------------------------------------------------

    def append_rows(self, rows: list[dict]) -> None:
        """
        Append one dict per product to the log tab.
        Keys that match header names are used; missing keys default to "".

        Failsafes:
          1. Dedupe by (sku, run_id) — if upstream calls accidentally pass
             duplicate dicts within one batch, only one is written.
          2. Skip rows missing a SKU entirely.
          3. Header sync: if the on-sheet header row differs from the expected
             schema (new competitor added, column reordered), REPLACE row 1
             rather than insert-without-delete. The previous behaviour created
             duplicate header rows on every schema change, which silently broke
             gspread.get_all_records() on read.
        """
        if not rows:
            return

        # ── Failsafe 1+2: dedupe by (sku, run_id) and drop SKU-less rows ─
        seen: set[tuple[str, str]] = set()
        clean_rows: list[dict] = []
        dropped_dup = 0
        dropped_nosku = 0
        for r in rows:
            sku = str(r.get("sku", "")).strip()
            if not sku:
                dropped_nosku += 1
                continue
            run_id = str(r.get("run_id", ""))
            key = (sku, run_id)
            if key in seen:
                dropped_dup += 1
                continue
            seen.add(key)
            clean_rows.append(r)

        if dropped_dup or dropped_nosku:
            log.warning(
                "append_rows: dropped %d duplicate and %d SKU-less rows "
                "(kept %d of %d input rows)",
                dropped_dup, dropped_nosku, len(clean_rows), len(rows),
            )
        if not clean_rows:
            return

        ws = self._get_or_create_ws()

        # ── Failsafe 3: header sync (replace row 1, not insert duplicate) ─
        all_vals = ws.get_all_values()
        if not all_vals:
            ws.append_row(self.headers, value_input_option="USER_ENTERED")
        elif all_vals[0] != self.headers:
            # Use update() to overwrite row 1 in place — no shifting, no dupe.
            ws.update(
                values=[self.headers],
                range_name=f"A1:{_col_letter(len(self.headers))}1",
                value_input_option="USER_ENTERED",
            )

        values = [
            [str(r.get(h, "") or "") for h in self.headers]
            for r in clean_rows
        ]
        ws.append_rows(values, value_input_option="USER_ENTERED")
        log.info("Appended %d rows to %s", len(values), self.TAB_NAME)

    # ------------------------------------------------------------------
    # Read — for dashboard
    # ------------------------------------------------------------------

    def read_all(self) -> pd.DataFrame:
        """Return the full log as a DataFrame."""
        ws = self._get_or_create_ws()
        records = ws.get_all_records(expected_headers=self.headers)
        if not records:
            return pd.DataFrame(columns=self.headers)
        return pd.DataFrame(records, columns=self.headers)

    def read_pending(self) -> pd.DataFrame:
        """Return rows with status PENDING_REVIEW or MARGIN_FLOOR_HIT."""
        from .pricer import STATUS_PENDING, STATUS_MARGIN_FLOOR
        df = self.read_all()
        if df.empty or "status" not in df.columns:
            return df
        return df[df["status"].isin([STATUS_PENDING, STATUS_MARGIN_FLOOR])].copy()

    # ------------------------------------------------------------------
    # Update — approvals, rejections, overrides
    # ------------------------------------------------------------------

    def _find_row_num(self, run_id: str, sku: str) -> Optional[int]:
        """
        Return the 1-based sheet row number for the given run_id + sku pair.
        Returns None if not found.
        """
        ws = self._get_or_create_ws()
        all_values = ws.get_all_values()
        if len(all_values) <= 1:
            return None
        header = all_values[0]
        try:
            run_col = header.index("run_id")
            sku_col = header.index("sku")
        except ValueError:
            return None
        for i, row in enumerate(all_values[1:], start=2):
            if (
                len(row) > max(run_col, sku_col)
                and row[run_col] == run_id
                and row[sku_col] == sku
            ):
                return i
        return None

    def _update_cols(self, row_num: int, updates: dict[str, Any]) -> None:
        """Batch-update specific columns in a single row."""
        ws = self._get_or_create_ws()
        all_values = ws.get_all_values()
        if not all_values:
            return
        header = all_values[0]
        cells = []
        for col_name, value in updates.items():
            try:
                col_idx = header.index(col_name) + 1  # 1-based
                cells.append(gspread.Cell(row_num, col_idx, str(value) if value is not None else ""))
            except ValueError:
                log.warning("Column %r not found in header — skipping update", col_name)
        if cells:
            ws.update_cells(cells, value_input_option="USER_ENTERED")

    def approve_row(
        self,
        run_id: str,
        sku: str,
        override_price: Optional[float],
        approved_by: str = "Brent",
    ) -> bool:
        """
        Mark a row as APPROVED (or OVERRIDDEN if override_price differs from ai_suggested).
        Returns True if the row was found and updated.
        """
        from .pricer import STATUS_APPROVED, STATUS_OVERRIDDEN

        row_num = self._find_row_num(run_id, sku)
        if row_num is None:
            log.warning("approve_row: row not found for run_id=%r sku=%r", run_id, sku)
            return False

        # Determine if this is an approval or override
        ws = self._get_or_create_ws()
        all_values = ws.get_all_values()
        header = all_values[0]
        try:
            ai_col = header.index("ai_suggested_price")
            row_data = all_values[row_num - 1]
            ai_price_str = row_data[ai_col] if len(row_data) > ai_col else ""
            ai_price = float(ai_price_str) if ai_price_str else None
        except (ValueError, IndexError):
            ai_price = None

        status = STATUS_OVERRIDDEN if (
            override_price is not None and ai_price is not None
            and abs(override_price - ai_price) > 0.01
        ) else STATUS_APPROVED

        now = datetime.now(timezone.utc).isoformat()
        updates = {
            "status": status,
            "approved_by": approved_by,
            "applied_at": now,
        }
        if override_price is not None:
            updates["human_override_price"] = f"{override_price:.2f}"

        self._update_cols(row_num, updates)
        log.info("Row approved: run_id=%s sku=%s status=%s", run_id, sku, status)
        return True

    def reject_row(self, run_id: str, sku: str, rejected_by: str = "Brent") -> bool:
        """
        Mark a row as REJECTED.
        Returns True if the row was found and updated.
        """
        from .pricer import STATUS_REJECTED

        row_num = self._find_row_num(run_id, sku)
        if row_num is None:
            log.warning("reject_row: row not found for run_id=%r sku=%r", run_id, sku)
            return False

        now = datetime.now(timezone.utc).isoformat()
        self._update_cols(row_num, {
            "status": STATUS_REJECTED,
            "approved_by": rejected_by,
            "applied_at": now,
        })
        log.info("Row rejected: run_id=%s sku=%s", run_id, sku)
        return True
