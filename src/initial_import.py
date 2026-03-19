"""
One-time initial import: read existing Excel pricelist → write to Google Sheets master.

This script should be run ONCE after the Google Sheets spreadsheet is created and
the service account has been granted editor access.

It:
1. Reads all 11 supplier sheets from the existing Excel file
2. Normalizes them through the same pipeline as the live system
3. Writes all rows to the master Google Sheet
4. Generates row_hashes so the first live sync only updates actual changes

Usage:
    python -m src.initial_import \
        --excel /path/to/Camping\ Fridge\ SA\ Pricelist.xlsx \
        --spreadsheet-id YOUR_SPREADSHEET_ID

Dry run (no writes):
    python -m src.initial_import --excel ... --spreadsheet-id ... --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import pandas as pd

from .config_loader import load_all_supplier_configs
from .normalizer import normalize
from .parsers.xlsx_parser import parse_xlsx
from .sheets_client import SheetsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Map supplier_key → Excel sheet name
SUPPLIER_SHEET_MAP = {
    "engel":               "Engel",
    "arb":                 "ARB",
    "flex":                "Flex",
    "snomaster":           "Snomaster",
    "dag":                 "DAG",
    "dometic_frontrunner": "Dometic Frontrunner",
    "dometic_thrsa":       "Dometic THRSA",
    "frozen":              "Frozen",
    "tsunami":             "Tsunami Coolers",
    "coldfactor":          "ColdFactor",
    "highon":              "HighOn",
}


def run_import(
    excel_path: str,
    spreadsheet_id: str,
    service_account_file: str | None = None,
    dry_run: bool = False,
    supplier_filter: str | None = None,
) -> None:
    configs = load_all_supplier_configs()
    xl_bytes = Path(excel_path).read_bytes()

    sa_file = service_account_file or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    sheets = None if dry_run else SheetsClient(spreadsheet_id, service_account_file=sa_file)

    total_rows = 0
    results = []

    for key, cfg in configs.items():
        if supplier_filter and key != supplier_filter:
            continue

        sheet_name = SUPPLIER_SHEET_MAP.get(key)
        if not sheet_name:
            log.warning("[%s] No Excel sheet mapping — skipping", key)
            continue

        log.info("[%s] Reading sheet: %s", key, sheet_name)
        try:
            raw_df = parse_xlsx(xl_bytes, sheet_name=sheet_name, skip_rows=0)
            norm_df = normalize(raw_df, cfg, source="manual")

            valid = norm_df[
                norm_df["sku"].str.strip().ne("") &
                norm_df["selling_price"].gt(0)
            ]

            log.info(
                "[%s] %d raw rows → %d normalized → %d with valid price",
                key, len(raw_df), len(norm_df), len(valid),
            )

            if not dry_run and not valid.empty:
                written = sheets.upsert_rows(valid, supplier=key)
                log.info("[%s] Wrote %d rows to master sheet", key, written)
            elif dry_run:
                log.info("[%s] DRY RUN — would write %d rows", key, len(valid))

            total_rows += len(valid)
            results.append({
                "supplier": key,
                "raw": len(raw_df),
                "normalized": len(norm_df),
                "written": len(valid),
            })

        except Exception as e:
            log.error("[%s] Failed: %s", key, e)
            results.append({"supplier": key, "raw": 0, "normalized": 0, "written": 0, "error": str(e)})

    # Print summary table
    print("\n" + "=" * 65)
    print(f"{'Supplier':<25} {'Raw':>6} {'Normalized':>11} {'Written':>8}")
    print("-" * 65)
    for r in results:
        err = f"  ERROR: {r.get('error', '')[:25]}" if "error" in r else ""
        print(f"{r['supplier']:<25} {r['raw']:>6} {r['normalized']:>11} {r['written']:>8}{err}")
    print("-" * 65)
    print(f"{'TOTAL':<25} {'':>6} {'':>11} {total_rows:>8}")
    print("=" * 65)

    if dry_run:
        print("\nDRY RUN — no data was written.")
    else:
        print(f"\nImport complete. {total_rows} rows written to Google Sheets.")
        print("Next step: export your Shopify products CSV and paste")
        print("shopify_product_id + shopify_variant_id into the master sheet columns M & N.")


def export_csv(excel_path: str, output_path: str, supplier_filter: str | None = None) -> None:
    """
    Export the normalized master data to a CSV file.
    Useful for manually uploading to Google Sheets or review.
    """
    configs = load_all_supplier_configs()
    xl_bytes = Path(excel_path).read_bytes()
    all_frames = []

    for key, cfg in configs.items():
        if supplier_filter and key != supplier_filter:
            continue
        sheet_name = SUPPLIER_SHEET_MAP.get(key)
        if not sheet_name:
            continue
        try:
            raw_df = parse_xlsx(xl_bytes, sheet_name=sheet_name, skip_rows=0)
            norm_df = normalize(raw_df, cfg, source="manual")
            valid = norm_df[norm_df["sku"].str.strip().ne("") & norm_df["selling_price"].gt(0)]
            all_frames.append(valid)
        except Exception as e:
            log.error("[%s] %s", key, e)

    if all_frames:
        combined = pd.concat(all_frames, ignore_index=True)
        combined.to_csv(output_path, index=False)
        log.info("Exported %d rows to %s", len(combined), output_path)
        print(f"\nExported {len(combined)} rows → {output_path}")
    else:
        print("No data exported.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CFSA Initial Data Import")
    parser.add_argument("--excel", required=True, help="Path to Camping Fridge SA Pricelist.xlsx")
    parser.add_argument("--spreadsheet-id", help="Google Sheets spreadsheet ID")
    parser.add_argument("--service-account", help="Path to GCP service account JSON")
    parser.add_argument("--supplier", help="Import a single supplier only")
    parser.add_argument("--dry-run", action="store_true", help="Parse + show counts, no writes")
    parser.add_argument(
        "--export-csv",
        metavar="OUTPUT_PATH",
        help="Export normalized data to CSV instead of writing to Sheets",
    )
    args = parser.parse_args()

    if args.export_csv:
        export_csv(args.excel, args.export_csv, supplier_filter=args.supplier)
    else:
        if not args.spreadsheet_id and not args.dry_run:
            parser.error("--spreadsheet-id is required unless --dry-run or --export-csv")
        run_import(
            excel_path=args.excel,
            spreadsheet_id=args.spreadsheet_id or "DRY_RUN",
            service_account_file=args.service_account,
            dry_run=args.dry_run,
            supplier_filter=args.supplier,
        )
