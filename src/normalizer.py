"""
Normalize a raw supplier DataFrame into the master schema.

Steps:
1. Apply column_map: rename supplier columns → master field names
2. Normalize SKU (uppercase, strip prefix, remove spaces)
3. Cast numerics (cost_inc, rrp, delivery_cost, etc.)
4. Map stock_status to canonical values
5. Calculate selling_price from price_formula
6. Compute row_hash for diff engine
7. Fill missing master fields with None
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from .config_loader import SupplierConfig

log = logging.getLogger(__name__)

# The canonical column order for the master Google Sheet
MASTER_FIELDS: list[str] = [
    "sku",
    "supplier",
    "description",
    "cost_inc",
    "rrp",
    "selling_price",
    "delivery_charged",
    "delivery_cost",
    "stock_status",
    "stock_qty",
    "last_updated",
    "source",
    "shopify_product_id",
    "shopify_variant_id",
    "shopify_last_synced",
    "price_formula",
    "competitor_price",
    "notes",
    "raw_sku",
    "row_hash",
    "cost_source",    # "supplier" (from pricelist) | "estimated" (rrp × ratio)
]

NUMERIC_FIELDS = ("cost_inc", "rrp", "delivery_cost", "competitor_price", "selling_price")
INT_FIELDS = ("stock_qty",)


def normalize(
    df: pd.DataFrame,
    config: SupplierConfig,
    source: str = "email",
) -> pd.DataFrame:
    """
    Transform a raw supplier DataFrame into master schema rows.

    Args:
        df:     Raw DataFrame from parsers.
        config: Validated supplier config.
        source: "email" | "scrape" | "manual"

    Returns:
        DataFrame with exactly MASTER_FIELDS columns.
    """
    col_map = config.column_map
    status_map = config.stock_status_map
    sku_norm = config.sku_normalization
    formula = config.price_formula

    out_rows: list[dict[str, Any]] = []
    skipped_no_sku = 0
    skipped_filter = 0

    for _, row in df.iterrows():
        mapped: dict[str, Any] = {}

        # 1. Apply column_map
        for master_field, supplier_col in col_map.items():
            if isinstance(supplier_col, int):
                try:
                    mapped[master_field] = row.iloc[supplier_col]
                except IndexError:
                    mapped[master_field] = None
            else:
                mapped[master_field] = row.get(supplier_col)

        # 2. Normalize SKU
        raw_sku = _str(mapped.get("sku"))
        if not raw_sku:
            skipped_no_sku += 1
            continue

        # 2a. SKU prefix filter — if set, skip rows whose SKU doesn't start with an allowed prefix
        if config.sku_prefix_filter:
            sku_upper = raw_sku.upper()
            if not any(sku_upper.startswith(p.upper()) for p in config.sku_prefix_filter):
                skipped_filter += 1
                continue

        # 2b. Description keyword filter
        if config.description_filter:
            desc_lower = _str(mapped.get("description")).lower()
            df_cfg = config.description_filter
            if df_cfg.include and not any(kw.lower() in desc_lower for kw in df_cfg.include):
                skipped_filter += 1
                continue
            if df_cfg.exclude and any(kw.lower() in desc_lower for kw in df_cfg.exclude):
                skipped_filter += 1
                continue

        sku = raw_sku
        if sku_norm.uppercase:
            sku = sku.upper()
        if sku_norm.remove_spaces:
            sku = sku.replace(" ", "")
        prefix = sku_norm.strip_prefix
        if prefix and sku.startswith(prefix):
            sku = sku[len(prefix):]

        mapped["raw_sku"] = raw_sku
        mapped["sku"] = sku
        mapped["supplier"] = config.supplier_key
        mapped["source"] = source
        mapped["last_updated"] = datetime.now(timezone.utc).isoformat()

        # 3. Cast numerics
        for field in NUMERIC_FIELDS:
            mapped[field] = _to_float(mapped.get(field))

        for field in INT_FIELDS:
            mapped[field] = _to_int(mapped.get(field))

        # 3b. Convert cost_ex_vat → cost_inc using supplier vat_rate
        if mapped.get("cost_inc") is None and mapped.get("cost_ex_vat") is not None:
            vat_rate = getattr(config, "vat_rate", 1.15)
            cost_ex = _to_float(mapped.get("cost_ex_vat"))
            if cost_ex:
                mapped["cost_inc"] = round(cost_ex * vat_rate, 2)

        # 3c. ESTIMATE cost_inc from rrp when no real cost is available.
        # Only fires when:
        #   • cost_inc is still empty after steps 3 + 3b, AND
        #   • supplier YAML has a `cost_estimation` block with enabled=true.
        # Real cost from a supplier pricelist (extracted in step 3) ALWAYS wins.
        # The estimated value is tagged via `cost_source = "estimated"` so the
        # UI can mark it differently and the operator knows to replace it once
        # the real supplier pricelist arrives.
        if mapped.get("cost_inc") is None:
            est_cfg = getattr(config, "cost_estimation", None)
            # Support both pydantic-model and plain-dict configs
            if est_cfg is None and isinstance(getattr(config, "__dict__", None), dict):
                est_cfg = config.__dict__.get("cost_estimation")
            if isinstance(est_cfg, dict) and est_cfg.get("enabled"):
                ratio = _to_float(est_cfg.get("ratio"))
                rrp_val = _to_float(mapped.get("rrp"))
                if ratio and rrp_val and ratio > 0 and rrp_val > 0:
                    mapped["cost_inc"] = round(rrp_val * ratio, 2)
                    mapped["cost_source"] = "estimated"
        # If we DID get a real cost above, tag the source explicitly
        if mapped.get("cost_inc") is not None and not mapped.get("cost_source"):
            mapped["cost_source"] = "supplier"

        # 4. Map stock status
        raw_status = _str(mapped.get("stock_status"))
        mapped["stock_status"] = status_map.get(raw_status, raw_status or "Unknown")

        # 5. Calculate selling_price
        if mapped.get("selling_price") is None:
            mapped["selling_price"] = _calculate_price(mapped, formula.expression)
        mapped["price_formula"] = formula.key

        # 6. Row hash (gates diff engine)
        mapped["row_hash"] = _row_hash(
            sku=mapped["sku"],
            cost_inc=mapped.get("cost_inc"),
            rrp=mapped.get("rrp"),
            stock_status=mapped.get("stock_status"),
        )

        # 7. Fill missing master fields
        for field in MASTER_FIELDS:
            mapped.setdefault(field, None)

        out_rows.append({f: mapped.get(f) for f in MASTER_FIELDS})

    if skipped_no_sku:
        log.warning("[%s] Skipped %d rows with no SKU", config.supplier_key, skipped_no_sku)
    if skipped_filter:
        log.info("[%s] Filtered out %d non-fridge rows (description/SKU prefix filter)", config.supplier_key, skipped_filter)

    if not out_rows:
        return pd.DataFrame(columns=MASTER_FIELDS)

    return pd.DataFrame(out_rows, columns=MASTER_FIELDS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str(val: Any) -> str:
    if val is None or (isinstance(val, float) and val != val):
        return ""
    return str(val).strip()


def _to_float(val: Any) -> Optional[float]:
    s = _str(val)
    if not s:
        return None
    # Remove currency symbols and thousand separators common in ZAR prices
    s = s.replace("R", "").replace(",", "").replace(" ", "").replace("\xa0", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _to_int(val: Any) -> Optional[int]:
    f = _to_float(val)
    if f is None:
        return None
    try:
        return int(f)
    except (ValueError, OverflowError):
        return None


def _calculate_price(row: dict[str, Any], expression: str) -> Optional[float]:
    if not expression:
        return None
    try:
        scope = {
            "cost_inc": row.get("cost_inc") or 0.0,
            "rrp": row.get("rrp") or 0.0,
        }
        result = eval(  # noqa: S307 — safe: only arithmetic, no builtins
            compile(expression, "<price_formula>", "eval"),
            {"__builtins__": {}},
            scope,
        )
        return round(float(result), 2)
    except Exception as e:
        log.debug("Price formula error (%r): %s", expression, e)
        return None


def _row_hash(
    sku: str,
    cost_inc: Optional[float],
    rrp: Optional[float],
    stock_status: Optional[str],
) -> str:
    payload = "|".join(
        [
            sku,
            str(cost_inc or ""),
            str(rrp or ""),
            str(stock_status or ""),
        ]
    )
    return hashlib.md5(payload.encode()).hexdigest()
