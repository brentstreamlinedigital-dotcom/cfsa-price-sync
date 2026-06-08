"""
Competitor Analysis — main orchestrator.

Run modes
─────────
  python -m scrapers.competitor_analysis.main              # all linked products
  python -m scrapers.competitor_analysis.main --sku MD60F  # single product
  python -m scrapers.competitor_analysis.main --dry-run    # no sheet writes

This module NEVER writes prices to Shopify. All suggestions land in
competitor_analysis_log with status=PENDING_REVIEW. A human approves
via the Streamlit dashboard, which then calls the Shopify API.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Ensure the repo root is on sys.path so we can import src.*
_REPO_ROOT = Path(__file__).parent.parent.parent
import sys
sys.path.insert(0, str(_REPO_ROOT))

from src.automation_status import StatusWriter
from src.config_loader import load_app_config
from src.sheets_client import SheetsClient
from .pricer import (
    STATUS_ALREADY_COMP, STATUS_MARGIN_FLOOR, STATUS_NO_MATCH,
    STATUS_PENDING, STATUS_SCRAPE_FAILED,
    calculate_suggested_price, compute_discrepancy, compute_margin_pct,
)
from .scraper import load_competitors, scrape_all_products
from .sheets_logger import CompetitorSheetsLogger
from .obsidian_logger import write_obsidian_log

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _load_active_products(sheets: SheetsClient) -> list[dict]:
    """
    Read the master sheet and return products that are live on the website
    (i.e. have a shopify_variant_id) — these are the ones worth tracking.
    Returns list of dicts with at least: sku, description, selling_price, cost_inc.
    """
    master_df = sheets.read_master()
    if master_df.empty:
        return []
    linked = master_df[
        master_df["shopify_variant_id"].astype(str).str.strip().ne("")
    ]
    return linked.to_dict("records")


def _build_log_row(
    *,
    run_id: str,
    product: dict,
    outcome,            # ProductScrapeOutcome
    competitors: list[dict],
    suggested_price: Optional[float],
    status: str,
    now_str: str,
) -> dict:
    """Assemble one competitor_analysis_log row dict."""
    sku = str(product.get("sku", ""))
    cfsa_price = float(product.get("selling_price") or 0)
    cost_price_raw = product.get("cost_inc")
    cost_price = float(cost_price_raw) if cost_price_raw else None
    margin = compute_margin_pct(cfsa_price, cost_price)

    prices_by_comp = outcome.prices_by_competitor()
    cheapest_price, cheapest_source = outcome.cheapest()
    discrepancy = compute_discrepancy(cfsa_price, cheapest_price)

    row: dict = {
        "timestamp":          now_str,
        "run_id":             run_id,
        "sku":                sku,
        "product_name":       str(product.get("description", "") or ""),
        "cfsa_current_price": f"{cfsa_price:.2f}",
        "cost_price":         f"{cost_price:.2f}" if cost_price else "",
        "margin_pct":         f"{margin:.1f}%" if margin is not None else "",
        "cheapest_competitor":f"{cheapest_price:.2f}" if cheapest_price else "",
        "cheapest_source":    cheapest_source or "",
        "discrepancy_rand":   f"{discrepancy:.2f}" if discrepancy is not None else "",
        "ai_suggested_price": f"{suggested_price:.2f}" if suggested_price else "",
        "human_override_price": "",
        "status":             status,
        "approved_by":        "",
        "applied_at":         "",
        "shopify_variant_id": str(product.get("shopify_variant_id", "") or ""),
    }

    # Per-competitor price columns
    for comp in competitors:
        col = f"{comp['name']}_price"
        price = prices_by_comp.get(comp["name"])
        row[col] = f"{price:.2f}" if price else ""

    return row


async def run(
    sku_filter: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    app_cfg = load_app_config()

    # ── Config ────────────────────────────────────────────────────────
    ca_cfg = app_cfg.get("competitor_analysis", {})
    pricing_cfg = app_cfg.get("pricing", {})
    obsidian_vault = app_cfg.get("obsidian", {}).get("vault_path", "")

    match_threshold = float(ca_cfg.get("match_threshold", 65))
    max_results = int(ca_cfg.get("results_per_product", 5))
    inter_product_delay = float(ca_cfg.get("inter_product_delay", 0.2))
    min_margin_pct = float(pricing_cfg.get("min_margin_pct", 15.0))

    # ── Clients ───────────────────────────────────────────────────────
    sa_file = app_cfg.get("google", {}).get("service_account_file") or os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    spreadsheet_id = (
        os.getenv("SHEETS_SPREADSHEET_ID")
        or app_cfg["google"]["sheets"]["spreadsheet_id"]
    )
    if not spreadsheet_id:
        raise RuntimeError("SHEETS_SPREADSHEET_ID env var is not set")

    sheets = SheetsClient(spreadsheet_id, service_account_file=sa_file)
    competitors = load_competitors()
    logger = CompetitorSheetsLogger(
        spreadsheet_id=spreadsheet_id,
        competitors=competitors,
        service_account_file=sa_file,
    )

    # ── Load products ─────────────────────────────────────────────────
    products = _load_active_products(sheets)
    if sku_filter:
        products = [p for p in products if str(p.get("sku", "")).upper() == sku_filter.upper()]
        if not products:
            log.warning("No active product found for SKU: %s", sku_filter)
            return

    if not products:
        log.info("No active (linked) products found — nothing to analyse")
        return

    log.info(
        "Starting competitor analysis: %d products × %d competitors (dry_run=%s)",
        len(products), len(competitors), dry_run,
    )

    # ── Scrape ────────────────────────────────────────────────────────
    run_id  = str(uuid.uuid4())
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    status_writer = StatusWriter("competitor_analysis", total=len(products))
    status_writer.__enter__()
    status_writer.tick(done=0, stage=f"Scraping {len(products)} products across {len(competitors)} competitors")

    outcomes = await scrape_all_products(
        products=products,
        competitors=competitors,
        match_threshold=match_threshold,
        max_results=max_results,
        inter_product_delay=inter_product_delay,
    )

    # ── Price + log ───────────────────────────────────────────────────
    log_rows: list[dict] = []
    stats = {
        "discrepancies":    0,
        "already_comp":     0,
        "no_match":         0,
        "scrape_fail_total":0,
        "pending":          0,
        "margin_floor":     0,
    }
    top_discrepancies: list[dict] = []

    for _prod_idx, (product, outcome) in enumerate(zip(products, outcomes)):
        cfsa_price = float(product.get("selling_price") or 0)
        cost_raw   = product.get("cost_inc")
        cost_price = float(cost_raw) if cost_raw else None

        # Count individual scrape failures
        fail_count = sum(1 for r in outcome.results if r.status == "SCRAPE_FAILED")
        stats["scrape_fail_total"] += fail_count

        # All prices across all competitors
        valid_prices = [r.price for r in outcome.results if r.price is not None]

        # Takealot is the dominant ZA retailer — when they have a price, treat
        # it as the primary benchmark (we want to be positioned vs Takealot, not
        # vs the absolute cheapest of an obscure store).
        takealot_price = next(
            (r.price for r in outcome.results
             if r.competitor == "takealot" and r.price is not None),
            None,
        )

        if not valid_prices and fail_count == len(outcome.results):
            # Every competitor failed — treat as scrape failure
            status = STATUS_SCRAPE_FAILED
            suggested = None
        else:
            suggested, status = calculate_suggested_price(
                competitor_prices=valid_prices,
                cost_price=cost_price,
                cfsa_current_price=cfsa_price,
                min_margin_pct=min_margin_pct,
                anchor_price=takealot_price,
            )

        # Update stats
        if status == STATUS_ALREADY_COMP:
            stats["already_comp"] += 1
        elif status in (STATUS_PENDING, STATUS_MARGIN_FLOOR):
            stats["pending"] += 1
            cheapest_price, _ = outcome.cheapest()
            if cheapest_price and cfsa_price > cheapest_price:
                stats["discrepancies"] += 1
                top_discrepancies.append({
                    "sku": product.get("sku", ""),
                    "product": str(product.get("description", ""))[:40],
                    "cfsa_price": cfsa_price,
                    "cheapest_competitor_price": cheapest_price,
                    "cheapest_source": outcome.cheapest()[1] or "",
                    "difference": cfsa_price - cheapest_price,
                })
            if status == STATUS_MARGIN_FLOOR:
                stats["margin_floor"] += 1
        elif status in (STATUS_NO_MATCH, STATUS_SCRAPE_FAILED):
            stats["no_match"] += 1

        row = _build_log_row(
            run_id=run_id,
            product=product,
            outcome=outcome,
            competitors=competitors,
            suggested_price=suggested,
            status=status,
            now_str=now_str,
        )
        log_rows.append(row)

        log.info(
            "[%s] status=%-22s  cfsa=R%.2f  cheapest=R%.2f  suggested=%s",
            product.get("sku", "?"),
            status,
            cfsa_price,
            outcome.cheapest()[0] or 0,
            f"R{suggested:.2f}" if suggested else "N/A",
        )
        status_writer.tick(
            done=_prod_idx + 1,
            current=str(product.get("sku", "")),
            stage=f"Pricing {_prod_idx + 1}/{len(products)} — {product.get('sku', '')}",
        )

    # ── No-data guard ─────────────────────────────────────────────────
    total_prices_found = sum(
        1 for o in outcomes for r in o.results if r.price is not None
    )
    if total_prices_found == 0:
        log.warning(
            "COMPETITOR ANALYSIS RETURNED NO DATA — "
            "0 competitor prices found across %d products and %d competitors. "
            "All scrapes may have failed or no fuzzy matches exceeded the threshold (%.0f). "
            "Check your network, Playwright setup, and competitor site availability "
            "before treating these results as meaningful.",
            len(products), len(competitors), match_threshold,
        )
    else:
        fully_failed = sum(
            1 for o in outcomes
            if all(r.status == "SCRAPE_FAILED" for r in o.results)
        )
        if fully_failed:
            log.warning(
                "%d/%d products had ALL competitors fail to scrape — "
                "no pricing data for those products.",
                fully_failed, len(products),
            )

    # ── Write to sheet ────────────────────────────────────────────────
    if not dry_run:
        logger.append_rows(log_rows)
        log.info("Wrote %d rows to competitor_analysis_log", len(log_rows))
    else:
        log.info("DRY RUN — would write %d rows to competitor_analysis_log", len(log_rows))

    # ── Refresh suppliers view so the dashboard's per-supplier catalog
    # stays current after a competitor run too (master_df already loaded).
    if not dry_run:
        try:
            n = sheets.rebuild_suppliers_view(master_df=sheets.read_master())
            log.info("Suppliers view refreshed: %d rows", n)
        except Exception as exc:
            log.warning("Suppliers view refresh failed (non-fatal): %s", exc)

    # ── Obsidian log ──────────────────────────────────────────────────
    # Build competitor coverage summary
    comp_coverage = []
    for comp in competitors:
        matched  = sum(
            1 for o in outcomes
            for r in o.results
            if r.competitor == comp["name"] and r.price is not None
        )
        failures = sum(
            1 for o in outcomes
            for r in o.results
            if r.competitor == comp["name"] and r.status == "SCRAPE_FAILED"
        )
        comp_coverage.append({
            "name": comp.get("display_name", comp["name"]),
            "products_matched": matched,
            "scrape_failures": failures,
        })

    # Sort top discrepancies descending by difference
    top_discrepancies.sort(key=lambda x: x["difference"], reverse=True)

    if not dry_run and obsidian_vault:
        write_obsidian_log(
            obsidian_vault,
            products_analysed=len(products),
            discrepancies_found=stats["discrepancies"],
            already_competitive=stats["already_comp"],
            no_match_found=stats["no_match"],
            scrape_failures=stats["scrape_fail_total"],
            pending_review=stats["pending"],
            top_discrepancies=top_discrepancies,
            competitor_coverage=comp_coverage,
        )

    log.info(
        "Competitor analysis complete: %d products | %d discrepancies | "
        "%d already competitive | %d no match | %d scrape failures",
        len(products),
        stats["discrepancies"],
        stats["already_comp"],
        stats["no_match"],
        stats["scrape_fail_total"],
    )
    status_writer.__exit__(None, None, None)


def main() -> None:
    parser = argparse.ArgumentParser(description="CFSA Competitor Price Analysis")
    parser.add_argument("--sku",      help="Analyse a single product by SKU")
    parser.add_argument("--dry-run",  action="store_true", help="No sheet writes")
    args = parser.parse_args()

    asyncio.run(run(sku_filter=args.sku, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
