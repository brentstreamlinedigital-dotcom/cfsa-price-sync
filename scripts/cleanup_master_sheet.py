#!/usr/bin/env python3
"""
One-time cleanup: remove accessory / non-fridge rows from the master sheet
that were inserted before description_filter / brand_filter were active.

Run from the project root:
    python scripts/cleanup_master_sheet.py [--dry-run]

This script:
1. Reads the current master sheet
2. Loads the description_filter + brand_filter for each supplier
3. Identifies rows that would now be filtered out
4. Removes them from the master (and optionally from new_products too)

It does NOT touch the price_changes sheet (that's an audit trail).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow importing from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config_loader import load_all_supplier_configs
from src.sheets_client import SheetsClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def row_passes_filters(row: dict, cfg) -> bool:
    """Return True if this master-sheet row would pass the supplier's current filters."""
    description = str(row.get("description", "") or "").lower()
    sku = str(row.get("sku", "") or "").strip().upper()

    # SKU prefix filter
    if cfg.sku_prefix_filter:
        if not any(sku.startswith(p.upper()) for p in cfg.sku_prefix_filter):
            return False

    # Description filter
    if cfg.description_filter:
        df = cfg.description_filter
        if df.include and not any(kw.lower() in description for kw in df.include):
            return False
        if df.exclude and any(kw.lower() in description for kw in df.exclude):
            return False

    return True


def main(dry_run: bool = False) -> None:
    import yaml
    from pathlib import Path

    # Load app config for spreadsheet ID
    with open("config/app.yaml") as f:
        import yaml as _yaml
        app_cfg = _yaml.safe_load(f)

    spreadsheet_id = app_cfg["google"]["sheets"]["spreadsheet_id"]
    sa_file = app_cfg.get("google", {}).get("service_account_file")

    configs = load_all_supplier_configs()
    sheets = SheetsClient(spreadsheet_id, service_account_file=sa_file)

    import gspread
    import pandas as pd

    # ── Read master sheet ────────────────────────────────────────────────
    from src.normalizer import MASTER_FIELDS
    ws = sheets._get_or_create_worksheet("master", headers=MASTER_FIELDS)
    all_values = ws.get_all_values()

    if len(all_values) <= 1:
        log.info("Master sheet is empty — nothing to clean.")
        return

    header = all_values[0]
    data_rows = all_values[1:]

    # Column indices
    try:
        sku_col     = header.index("sku")
        supplier_col = header.index("supplier")
        desc_col    = header.index("description")
    except ValueError as e:
        log.error("Header missing expected column: %s", e)
        return

    log.info("Master sheet: %d data rows across all suppliers", len(data_rows))

    # ── Identify rows to delete ──────────────────────────────────────────
    rows_to_delete: list[int] = []  # 1-based sheet row numbers

    for i, row in enumerate(data_rows, start=2):
        row_supplier = row[supplier_col] if len(row) > supplier_col else ""
        row_sku      = row[sku_col]      if len(row) > sku_col      else ""
        row_desc     = row[desc_col]     if len(row) > desc_col     else ""

        if not row_supplier or row_supplier not in configs:
            continue  # unknown supplier, leave it alone

        cfg = configs[row_supplier]
        row_dict = {"sku": row_sku, "description": row_desc, "supplier": row_supplier}

        if not row_passes_filters(row_dict, cfg):
            if dry_run:
                log.info("  [DRY RUN] Would delete row %d: [%s] %s — %s",
                         i, row_supplier, row_sku, row_desc[:60])
            else:
                log.info("  Queuing row %d for deletion: [%s] %s — %s",
                         i, row_supplier, row_sku, row_desc[:60])
            rows_to_delete.append(i)

    if not rows_to_delete:
        log.info("✅ Master sheet is clean — no stale rows found.")
        return

    log.info("Found %d stale rows to remove.", len(rows_to_delete))

    if dry_run:
        log.info("DRY RUN mode — no changes written.")
        return

    # ── Delete stale rows (reverse order to preserve row numbers) ────────
    log.info("Deleting %d rows from master sheet…", len(rows_to_delete))
    for row_num in sorted(rows_to_delete, reverse=True):
        ws.delete_rows(row_num)
        log.debug("  Deleted row %d", row_num)

    log.info("✅ Cleanup complete — removed %d stale rows.", len(rows_to_delete))

    # ── Clean new_products sheet too ─────────────────────────────────────
    log.info("Checking new_products sheet…")
    try:
        np_ws = sheets._spreadsheet.worksheet("new_products")
        np_values = np_ws.get_all_values()
        if len(np_values) > 1:
            np_header = np_values[0]
            try:
                np_sup_col  = np_header.index("supplier")
                np_sku_col  = np_header.index("sku")
                np_desc_col = np_header.index("description")
            except ValueError:
                log.warning("new_products header mismatch — skipping")
                return

            np_delete: list[int] = []
            for i, row in enumerate(np_values[1:], start=2):
                sup  = row[np_sup_col]  if len(row) > np_sup_col  else ""
                sku  = row[np_sku_col]  if len(row) > np_sku_col  else ""
                desc = row[np_desc_col] if len(row) > np_desc_col else ""
                if sup in configs:
                    rd = {"sku": sku, "description": desc, "supplier": sup}
                    if not row_passes_filters(rd, configs[sup]):
                        log.info("  Queuing new_products row %d: [%s] %s", i, sup, desc[:60])
                        np_delete.append(i)

            for row_num in sorted(np_delete, reverse=True):
                np_ws.delete_rows(row_num)
            log.info("new_products: removed %d stale rows.", len(np_delete))
    except Exception as e:
        log.warning("Could not clean new_products sheet: %s", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Remove stale accessory rows from master sheet")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be deleted without making changes")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
