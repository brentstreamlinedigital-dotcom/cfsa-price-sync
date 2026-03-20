"""
Diff engine: compare incoming supplier rows against the current master sheet.

Rules:
- NEW row:     SKU not in master → insert
- CHANGED row: SKU in master but row_hash differs → update
- UNCHANGED:   row_hash identical → skip (no Shopify API call)
- ALERT:       selling_price changed by more than threshold_pct → flag for review
                instead of auto-syncing to Shopify

Returns structured diff results consumed by main.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

PRICE_ALERT_THRESHOLD_PCT = 15.0  # override via app config


@dataclass
class DiffResult:
    supplier: str
    new_rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    changed_rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    unchanged_count: int = 0
    alerts: list[dict] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return not self.new_rows.empty or not self.changed_rows.empty

    @property
    def total_changes(self) -> int:
        return len(self.new_rows) + len(self.changed_rows)


def compute_diff(
    incoming: pd.DataFrame,
    master: pd.DataFrame,
    supplier: str,
    price_alert_threshold_pct: float = PRICE_ALERT_THRESHOLD_PCT,
) -> DiffResult:
    """
    Compare incoming normalized rows against the current master sheet.

    Args:
        incoming:  Normalized DataFrame from normalizer.normalize()
        master:    Current master sheet DataFrame (all suppliers)
        supplier:  Supplier key (used to filter master to this supplier's rows)
        price_alert_threshold_pct: Alert if selling_price moves by more than this %

    Returns:
        DiffResult with new_rows, changed_rows, unchanged_count, alerts.
    """
    result = DiffResult(supplier=supplier)

    if incoming.empty:
        log.warning("[%s] No incoming rows to diff", supplier)
        return result

    # Filter master to this supplier only
    supplier_master = master[master["supplier"] == supplier].copy() if not master.empty else pd.DataFrame()

    # Build lookup: sku → {row_hash, selling_price, shopify_product_id, shopify_variant_id, shopify_last_synced}
    master_index: dict[str, dict] = {}
    if not supplier_master.empty:
        for _, row in supplier_master.iterrows():
            sku = str(row.get("sku", "") or "").strip()
            if sku:
                master_index[sku] = {
                    "row_hash": row.get("row_hash"),
                    "selling_price": _safe_float(row.get("selling_price")),
                    "shopify_product_id": row.get("shopify_product_id"),
                    "shopify_variant_id": row.get("shopify_variant_id"),
                    "shopify_last_synced": row.get("shopify_last_synced"),
                }

    new_rows: list[dict] = []
    changed_rows: list[dict] = []
    unchanged = 0
    alerts: list[dict] = []

    for _, inc_row in incoming.iterrows():
        sku = str(inc_row.get("sku", "") or "").strip()
        if not sku:
            continue

        inc_hash = inc_row.get("row_hash")
        inc_price = _safe_float(inc_row.get("selling_price"))

        # Skip rows with no/zero price — scraping likely failed for this product
        if not inc_price or inc_price <= 0:
            log.debug("[%s] Skipping %s — no price scraped", supplier, sku)
            continue

        # Margin floor: if we have a cost price, ensure selling_price >= cost_inc * (1 + floor/100)
        inc_cost = _safe_float(inc_row.get("cost_inc"))
        if inc_cost and inc_cost > 0 and price_alert_threshold_pct:
            min_selling = inc_cost * (1 + price_alert_threshold_pct / 100)
            if inc_price < min_selling:
                log.warning(
                    "[%s] MARGIN ALERT %s: selling R%.2f < cost R%.2f + %.0f%% floor (min R%.2f) — blocked",
                    supplier, sku, inc_price, inc_cost, price_alert_threshold_pct, min_selling,
                )
                continue  # Block this row entirely — don't write to sheet or Shopify

        if sku not in master_index:
            # Brand new product
            new_rows.append(inc_row.to_dict())
        else:
            existing = master_index[sku]

            if existing["row_hash"] == inc_hash:
                unchanged += 1
                continue

            # Something changed — check if it's a large price move
            old_price = existing["selling_price"]
            price_delta_pct = _price_delta_pct(old_price, inc_price)

            if price_delta_pct is not None and abs(price_delta_pct) >= price_alert_threshold_pct:
                alerts.append({
                    "sku": sku,
                    "supplier": supplier,
                    "old_price": old_price,
                    "new_price": inc_price,
                    "price_delta_pct": round(price_delta_pct, 1),
                    "reason": f"Price changed {price_delta_pct:+.1f}% — flagged for review",
                })
                log.warning(
                    "[%s] PRICE ALERT %s: R%.2f → R%.2f (%.1f%%)",
                    supplier, sku, old_price or 0, inc_price or 0, price_delta_pct,
                )
                # Still update master sheet but skip Shopify auto-sync
                row_dict = inc_row.to_dict()
                row_dict["_price_alerted"] = True
                row_dict["shopify_product_id"] = existing.get("shopify_product_id")
                row_dict["shopify_variant_id"] = existing.get("shopify_variant_id")
                row_dict["shopify_last_synced"] = existing.get("shopify_last_synced")
                changed_rows.append(row_dict)
            else:
                row_dict = inc_row.to_dict()
                row_dict["_price_alerted"] = False
                row_dict["shopify_product_id"] = existing.get("shopify_product_id")
                row_dict["shopify_variant_id"] = existing.get("shopify_variant_id")
                row_dict["shopify_last_synced"] = existing.get("shopify_last_synced")
                changed_rows.append(row_dict)

    result.new_rows = pd.DataFrame(new_rows) if new_rows else pd.DataFrame()
    result.changed_rows = pd.DataFrame(changed_rows) if changed_rows else pd.DataFrame()
    result.unchanged_count = unchanged
    result.alerts = alerts

    log.info(
        "[%s] Diff: %d new, %d changed, %d unchanged, %d alerts",
        supplier, len(new_rows), len(changed_rows), unchanged, len(alerts),
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _price_delta_pct(old: Optional[float], new: Optional[float]) -> Optional[float]:
    if old is None or new is None or old == 0:
        return None
    return (new - old) / old * 100
