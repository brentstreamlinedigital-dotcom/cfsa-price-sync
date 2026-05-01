"""
Google Sheets client — reads and writes the master spreadsheet.

Sheets used:
  master         — all supplier products, one row per SKU
  supplier_log   — one row per sync run per supplier
  error_flags    — rows that failed mapping or need human review
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import gspread
import pandas as pd
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials

from .normalizer import MASTER_FIELDS

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Rows beyond this threshold trigger a warning (sheet may need archiving)
MASTER_ROW_WARN_THRESHOLD = 50_000


class SheetsClient:
    def __init__(
        self,
        spreadsheet_id: str,
        service_account_file: Optional[str] = None,
        credentials: Optional[Credentials] = None,
    ):
        """
        Args:
            spreadsheet_id:      Google Sheets ID from the URL.
            service_account_file: Path to service account JSON (local dev).
            credentials:          Pre-built credentials (Cloud Run ADC).
        """
        self.spreadsheet_id = spreadsheet_id

        if credentials:
            gc = gspread.authorize(credentials)
        elif service_account_file:
            gc = gspread.service_account(filename=service_account_file, scopes=SCOPES)
        else:
            # Application Default Credentials (Cloud Run)
            import google.auth
            creds, _ = google.auth.default(scopes=SCOPES)
            gc = gspread.authorize(creds)

        self._spreadsheet = gc.open_by_key(spreadsheet_id)
        self._master_ws: Optional[gspread.Worksheet] = None
        self._log_ws: Optional[gspread.Worksheet] = None
        self._error_ws: Optional[gspread.Worksheet] = None

    # ------------------------------------------------------------------
    # Master sheet
    # ------------------------------------------------------------------

    def read_master(self) -> pd.DataFrame:
        """Read the full master sheet into a DataFrame."""
        ws = self._get_or_create_worksheet("master", headers=MASTER_FIELDS)
        records = ws.get_all_records(expected_headers=MASTER_FIELDS)
        if not records:
            return pd.DataFrame(columns=MASTER_FIELDS)
        df = pd.DataFrame(records, columns=MASTER_FIELDS)
        log.info("Read %d rows from master sheet", len(df))
        if len(df) > MASTER_ROW_WARN_THRESHOLD:
            log.warning("Master sheet has %d rows — consider archiving old data", len(df))
        return df

    def upsert_rows(self, df: pd.DataFrame, supplier: str) -> int:
        """
        Upsert rows into the master sheet.
        - Rows with existing SKU (same supplier) are updated in place.
        - New SKUs are appended.

        Returns the number of rows written.
        """
        if df.empty:
            return 0

        ws = self._get_or_create_worksheet("master", headers=MASTER_FIELDS)

        # Read current sheet to find existing row positions
        all_values = ws.get_all_values()
        if len(all_values) <= 1:
            # Sheet is empty or header only — just append
            rows_to_append = self._df_to_rows(df)
            ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")
            return len(rows_to_append)

        header = all_values[0]
        sku_col_idx = header.index("sku") if "sku" in header else None
        supplier_col_idx = header.index("supplier") if "supplier" in header else None

        # Build index: (sku, supplier) → 1-based row number in sheet
        existing_positions: dict[tuple, int] = {}
        if sku_col_idx is not None and supplier_col_idx is not None:
            for i, row in enumerate(all_values[1:], start=2):
                row_sku = row[sku_col_idx] if len(row) > sku_col_idx else ""
                row_supplier = row[supplier_col_idx] if len(row) > supplier_col_idx else ""
                if row_sku and row_supplier:
                    existing_positions[(row_sku, row_supplier)] = i

        updates: list[dict] = []  # {row_num, values}
        appends: list[list] = []

        for _, inc_row in df.iterrows():
            sku = str(inc_row.get("sku", "") or "").strip()
            values = self._row_to_list(inc_row)
            key = (sku, supplier)

            if key in existing_positions:
                updates.append({"row_num": existing_positions[key], "values": values})
            else:
                appends.append(values)

        # Batch update existing rows
        if updates:
            cell_updates = []
            for upd in updates:
                for col_idx, val in enumerate(upd["values"], start=1):
                    cell_updates.append(
                        gspread.Cell(upd["row_num"], col_idx, val)
                    )
            ws.update_cells(cell_updates, value_input_option="USER_ENTERED")
            log.info("[%s] Updated %d existing rows in master", supplier, len(updates))

        # Append new rows
        if appends:
            ws.append_rows(appends, value_input_option="USER_ENTERED")
            log.info("[%s] Appended %d new rows to master", supplier, len(appends))

        return len(updates) + len(appends)

    def update_shopify_sync_timestamp(
        self, sku: str, supplier: str, timestamp: str
    ) -> None:
        """Mark shopify_last_synced after a successful Shopify push."""
        ws = self._get_or_create_worksheet("master", headers=MASTER_FIELDS)
        all_values = ws.get_all_values()
        if len(all_values) <= 1:
            return
        header = all_values[0]
        try:
            sku_col = header.index("sku")
            supplier_col = header.index("supplier")
            synced_col = header.index("shopify_last_synced")
        except ValueError:
            return

        for i, row in enumerate(all_values[1:], start=2):
            if (
                len(row) > max(sku_col, supplier_col)
                and row[sku_col] == sku
                and row[supplier_col] == supplier
            ):
                ws.update_cell(i, synced_col + 1, timestamp)
                return

    # ------------------------------------------------------------------
    # Supplier log sheet
    # ------------------------------------------------------------------

    SUPPLIER_LOG_HEADERS = [
        "run_id", "supplier", "source", "rows_parsed",
        "rows_new", "rows_changed", "rows_unchanged", "rows_errored",
        "alerts", "duration_seconds", "timestamp",
    ]

    def append_supplier_log(self, row: dict[str, Any]) -> None:
        ws = self._get_or_create_worksheet(
            "supplier_log", headers=self.SUPPLIER_LOG_HEADERS
        )
        values = [str(row.get(h, "")) for h in self.SUPPLIER_LOG_HEADERS]
        ws.append_row(values, value_input_option="USER_ENTERED")

    # ------------------------------------------------------------------
    # Error flags sheet
    # ------------------------------------------------------------------

    ERROR_FLAG_HEADERS = [
        "run_id", "supplier", "sku", "error_type", "detail",
        "raw_row", "flagged_at", "resolved",
    ]

    PRICE_CHANGES_HEADERS = [
        "date", "supplier", "sku", "description",
        "old_price", "new_price", "change_amt", "change_pct",
        "old_stock_status", "new_stock_status", "alerted",
    ]

    def append_price_changes(self, rows: list[dict[str, Any]]) -> None:
        """Log every price/stock change to the price_changes sheet for audit trail."""
        if not rows:
            return
        ws = self._get_or_create_worksheet(
            "price_changes", headers=self.PRICE_CHANGES_HEADERS
        )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        values = []
        for r in rows:
            old_p = r.get("old_price")
            new_p = r.get("new_price")
            try:
                change_amt = round(float(new_p) - float(old_p), 2) if old_p and new_p else ""
                change_pct = round((float(new_p) - float(old_p)) / float(old_p) * 100, 1) if old_p and float(old_p) != 0 and new_p else ""
            except (TypeError, ValueError):
                change_amt = ""
                change_pct = ""
            values.append([
                now,
                str(r.get("supplier", "")),
                str(r.get("sku", "")),
                str(r.get("description", "")),
                str(old_p) if old_p is not None else "",
                str(new_p) if new_p is not None else "",
                str(change_amt),
                f"{change_pct}%" if change_pct != "" else "",
                str(r.get("old_stock_status", "")),
                str(r.get("new_stock_status", "")),
                "YES" if r.get("alerted") else "no",
            ])
        if values:
            ws.append_rows(values, value_input_option="USER_ENTERED")
            log.info("Logged %d price changes to price_changes sheet", len(values))

    NEW_PRODUCTS_HEADERS = [
        "date_found", "supplier", "sku", "description",
        "cost_inc", "selling_price", "stock_status", "stock_qty", "source",
    ]

    def append_new_products(self, rows: list[dict[str, Any]]) -> None:
        """
        Write new products (no shopify_variant_id) to the 'new_products' sheet
        so they can be reviewed and potentially added to the Shopify store.
        Skips rows already present (matched by supplier+sku).
        """
        if not rows:
            return
        ws = self._get_or_create_worksheet(
            "new_products", headers=self.NEW_PRODUCTS_HEADERS
        )
        # Build set of existing supplier+sku combos to avoid duplicates
        existing = ws.get_all_values()
        existing_keys: set[str] = set()
        if len(existing) > 1:  # first row is header
            for r in existing[1:]:
                if len(r) >= 3:
                    existing_keys.add(f"{r[1]}|{r[2]}")  # supplier|sku

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        values = []
        for r in rows:
            key = f"{r.get('supplier','')}|{r.get('sku','')}"
            if key in existing_keys:
                continue
            values.append([
                now,
                str(r.get("supplier", "")),
                str(r.get("sku", "")),
                str(r.get("description", "")),
                str(r.get("cost_inc", "")),
                str(r.get("selling_price", "")),
                str(r.get("stock_status", "")),
                str(r.get("stock_qty", "")),
                str(r.get("source", "")),
            ])

        if values:
            ws.append_rows(values, value_input_option="USER_ENTERED")
            log.info("Added %d new products to new_products sheet", len(values))

    def purge_stale_rows(self, supplier: str, keep_skus: set[str]) -> int:
        """
        Remove master sheet rows for *supplier* whose SKU is no longer in
        the current scrape results (i.e. products that have been filtered
        out or discontinued).

        Called after upsert_rows for scrape-only suppliers so the master
        sheet stays in sync with the live scrape rather than accumulating
        stale / filtered-out accessories.

        Args:
            supplier:  Supplier key to clean up.
            keep_skus: Set of SKUs that should be KEPT (from current scrape).

        Returns:
            Number of rows deleted.
        """
        if not keep_skus:
            log.warning("[%s] purge_stale_rows called with empty keep_skus — skipping (safety guard)", supplier)
            return 0

        ws = self._get_or_create_worksheet("master", headers=MASTER_FIELDS)
        all_values = ws.get_all_values()
        if len(all_values) <= 1:
            return 0

        header = all_values[0]
        try:
            sku_col = header.index("sku")
            supplier_col = header.index("supplier")
        except ValueError:
            return 0

        # Collect 1-based row numbers that should be deleted (in order)
        rows_to_delete: list[int] = []
        for i, row in enumerate(all_values[1:], start=2):
            row_supplier = row[supplier_col] if len(row) > supplier_col else ""
            row_sku = row[sku_col] if len(row) > sku_col else ""
            if row_supplier == supplier and row_sku and row_sku not in keep_skus:
                rows_to_delete.append(i)

        if not rows_to_delete:
            return 0

        log.info("[%s] Purging %d stale rows from master sheet", supplier, len(rows_to_delete))

        # Send all deletions in a single batch_update request (avoids the
        # 60 writes/min quota that would be hit by calling delete_rows()
        # individually for large purges).  Rows must still be in reverse
        # order so each deletion doesn't invalidate subsequent indices.
        batch_requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "ROWS",
                        "startIndex": row_num - 1,  # 0-indexed, inclusive
                        "endIndex": row_num,         # exclusive
                    }
                }
            }
            for row_num in sorted(rows_to_delete, reverse=True)
        ]
        ws.spreadsheet.batch_update({"requests": batch_requests})

        return len(rows_to_delete)

    def append_error_flags(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        ws = self._get_or_create_worksheet(
            "error_flags", headers=self.ERROR_FLAG_HEADERS
        )
        values = [
            [str(r.get(h, "")) for h in self.ERROR_FLAG_HEADERS]
            for r in rows
        ]
        ws.append_rows(values, value_input_option="USER_ENTERED")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_worksheet(
        self, name: str, headers: list[str]
    ) -> gspread.Worksheet:
        try:
            ws = self._spreadsheet.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(
                title=name, rows=10000, cols=len(headers)
            )
            ws.append_row(headers, value_input_option="USER_ENTERED")
            log.info("Created worksheet: %s", name)
        return ws

    def _df_to_rows(self, df: pd.DataFrame) -> list[list]:
        return [self._row_to_list(row) for _, row in df.iterrows()]

    @staticmethod
    def _row_to_list(row) -> list:
        return [
            "" if v is None or (isinstance(v, float) and v != v) else str(v)
            for v in row
        ]
