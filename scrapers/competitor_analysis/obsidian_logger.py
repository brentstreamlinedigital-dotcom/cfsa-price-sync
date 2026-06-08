"""
Writes a markdown summary of each competitor analysis run to the Obsidian vault.

Path pattern: {vault_path}/CFSA/Competitor Analysis/YYYY-MM-DD_HH-MM_competitor-analysis.md

Skips silently if vault_path is empty or not writable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def write_obsidian_log(
    vault_path: str,
    *,
    products_analysed: int,
    discrepancies_found: int,
    already_competitive: int,
    no_match_found: int,
    scrape_failures: int,
    pending_review: int,
    top_discrepancies: list[dict],
    competitor_coverage: list[dict],
) -> Optional[Path]:
    """
    Write a markdown price-comparison summary to the Obsidian vault.

    Args:
        vault_path:          Root path to the Obsidian vault.
        products_analysed:   Total products processed in this run.
        discrepancies_found: Products where CFSA price > cheapest competitor.
        already_competitive: Products where CFSA is already competitive.
        no_match_found:      Products where no competitor returned a match.
        scrape_failures:     Total individual competitor scrape failures.
        pending_review:      Products marked PENDING_REVIEW or MARGIN_FLOOR_HIT.
        top_discrepancies:   List of dicts: {sku, product, cfsa_price,
                              cheapest_competitor_price, cheapest_source, difference}
        competitor_coverage: List of dicts: {name, products_matched, scrape_failures}

    Returns:
        Path to the written file, or None if writing was skipped/failed.
    """
    if not vault_path:
        return None

    try:
        try:
            import pytz
            sast = pytz.timezone("Africa/Johannesburg")
            now = datetime.now(sast)
        except ImportError:
            now = datetime.now(timezone.utc)

        now_str  = now.strftime("%Y-%m-%d %H:%M SAST")
        file_ts  = now.strftime("%Y-%m-%d_%H-%M")

        # ── Top discrepancies table ───────────────────────────────────
        if top_discrepancies:
            disc_header = "| SKU | Product | CFSA Price | Cheapest Competitor | Source | Difference |"
            disc_sep    = "|-----|---------|-----------|-------------------|--------|------------|"
            disc_rows = [
                f"| {d.get('sku','')} | {d.get('product','')} "
                f"| R{d.get('cfsa_price',0):,.2f} "
                f"| R{d.get('cheapest_competitor_price',0):,.2f} "
                f"| {d.get('cheapest_source','')} "
                f"| R{d.get('difference',0):,.2f} |"
                for d in top_discrepancies[:5]
            ]
            top5_block = "\n".join([disc_header, disc_sep] + disc_rows)
        else:
            top5_block = "_No discrepancies found this run._"

        # ── Competitor coverage table ─────────────────────────────────
        if competitor_coverage:
            cov_header = "| Competitor | Products Matched | Scrape Failures |"
            cov_sep    = "|-----------|-----------------|-----------------|"
            cov_rows = [
                f"| {c.get('name','')} | {c.get('products_matched',0)} | {c.get('scrape_failures',0)} |"
                for c in competitor_coverage
            ]
            cov_block = "\n".join([cov_header, cov_sep] + cov_rows)
        else:
            cov_block = "_No coverage data available._"

        content = (
            f"## Competitor Analysis Run — {now_str}\n"
            f"- Products analysed: {products_analysed}\n"
            f"- Discrepancies found: {discrepancies_found} (CFSA more expensive than cheapest competitor)\n"
            f"- Already competitive: {already_competitive}\n"
            f"- No match found: {no_match_found}\n"
            f"- Scrape failures: {scrape_failures}\n"
            f"- Pending review: {pending_review}\n"
            f"\n### Top 5 Discrepancies\n{top5_block}\n"
            f"\n### Competitor Coverage This Run\n{cov_block}\n"
        )

        vault  = Path(vault_path)
        out_dir = vault / "CFSA" / "Competitor Analysis"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{file_ts}_competitor-analysis.md"
        out_file.write_text(content, encoding="utf-8")
        log.info("Obsidian competitor log written to %s", out_file)
        return out_file

    except Exception as exc:
        log.warning("Obsidian competitor log write failed (non-fatal): %s", exc)
        return None
