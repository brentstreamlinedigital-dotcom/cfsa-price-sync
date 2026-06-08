"""
Shopify Admin GraphQL client.

Handles:
- Variant price updates
- Inventory quantity updates
- Bulk operations with rate limiting
- Retry logic with exponential backoff
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Shopify allows ~2 requests/sec on basic plans; stay conservative
_RATE_LIMIT_DELAY = 0.6   # seconds between GraphQL calls
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0   # seconds, doubles on each retry


class ShopifyClient:
    def __init__(self, shop_domain: str, access_token: str, api_version: str = "2024-10"):
        """
        Args:
            shop_domain:  e.g. "your-store.myshopify.com"
            access_token: Shopify Admin API access token (from Secret Manager)
            api_version:  Shopify API version string
        """
        self.graphql_url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
        self.headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_variant_price(
        self,
        variant_id: str,
        price: float,
        compare_at_price: Optional[float] = None,
        product_id: Optional[str] = None,
    ) -> bool:
        """
        Update the price (and optional compare-at price) for a single variant.

        Uses productVariantsBulkUpdate (required in Shopify API 2024-01+,
        replaces the removed productVariantUpdate mutation).

        Args:
            variant_id:        Shopify variant GID, e.g. "gid://shopify/ProductVariant/1234"
            price:             New selling price in ZAR
            compare_at_price:  Strike-through price (usually the RRP)
            product_id:        Parent product GID. If omitted it is fetched automatically.
        """
        # productVariantsBulkUpdate requires the parent product ID
        if not product_id:
            product_id = self._get_variant_info(variant_id).get("product_id")
        if not product_id:
            raise ShopifyAPIError(
                f"Could not resolve parent product ID for variant {variant_id}"
            )

        mutation = """
        mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants {
              id
              price
              compareAtPrice
            }
            userErrors {
              field
              message
            }
          }
        }
        """
        variant_input: dict = {
            "id": variant_id,
            "price": f"{price:.2f}",
        }
        if compare_at_price is not None:
            variant_input["compareAtPrice"] = f"{compare_at_price:.2f}"

        variables = {
            "productId": product_id,
            "variants": [variant_input],
        }

        data = self._graphql(mutation, variables)
        errors = data.get("productVariantsBulkUpdate", {}).get("userErrors", [])
        if errors:
            raise ShopifyAPIError(f"Variant price update errors: {errors}")
        return True

    def set_inventory_quantity(
        self,
        inventory_item_id: str,
        location_id: str,
        quantity: int,
    ) -> bool:
        """
        Set absolute on-hand inventory quantity at a location.

        Args:
            inventory_item_id: Shopify inventory item GID
            location_id:       Shopify location GID
            quantity:          New quantity (clamped to >= 0)
        """
        mutation = """
        mutation inventorySetOnHandQuantities($input: InventorySetOnHandQuantitiesInput!) {
          inventorySetOnHandQuantities(input: $input) {
            userErrors {
              field
              message
            }
          }
        }
        """
        variables = {
            "input": {
                "reason": "correction",
                "setQuantities": [
                    {
                        "inventoryItemId": inventory_item_id,
                        "locationId": location_id,
                        "quantity": max(0, quantity),
                    }
                ],
            }
        }
        data = self._graphql(mutation, variables)
        errors = data.get("inventorySetOnHandQuantities", {}).get("userErrors", [])
        if errors:
            raise ShopifyAPIError(f"Inventory update errors: {errors}")
        return True

    def get_variant_inventory_item_id(self, variant_id: str) -> Optional[str]:
        """Fetch the inventoryItem GID for a product variant."""
        return self._get_variant_info(variant_id).get("inventory_item_id")

    def get_sku_to_variant_map(self) -> dict[str, str]:
        """
        Fetch ALL product variants from the store and return a mapping of
        normalised SKU (uppercase, stripped) → variant GID.

        Used by the backfill step so the master sheet can be populated with
        shopify_variant_id values without needing a price change to trigger it.
        Paginates automatically if the store has >250 variants.
        """
        query = """
        query getAllVariants($cursor: String) {
          productVariants(first: 250, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            edges {
              node {
                id
                sku
              }
            }
          }
        }
        """
        sku_map: dict[str, str] = {}
        cursor: Optional[str] = None

        while True:
            variables: dict = {}
            if cursor:
                variables["cursor"] = cursor

            data = self._graphql(query, variables)
            variants_data = data.get("productVariants", {})

            for edge in variants_data.get("edges", []):
                node = edge.get("node", {})
                raw_sku = (node.get("sku") or "").strip()
                vid = node.get("id")
                if raw_sku and vid:
                    # Normalise to uppercase so lookups are case-insensitive
                    sku_map[raw_sku.upper()] = vid

            page_info = variants_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            time.sleep(_RATE_LIMIT_DELAY)

        log.info("[Shopify] Loaded %d variant SKUs from store", len(sku_map))
        return sku_map

    def get_sku_to_price_map(self) -> dict[str, float]:
        """
        Fetch ALL product variants from the store and return a mapping of
        normalised SKU (uppercase, stripped) → CURRENT selling price (float, ZAR).

        This is the live "what the customer sees on campingfridge.co.za" price,
        which can differ from master.selling_price if anyone has edited the
        price directly in Shopify admin. Used by competitor analysis so the
        Pending Review cards display real Shopify prices, not the derived
        rrp × formula value from the supplier sync.

        Paginates automatically if the store has >250 variants.
        """
        query = """
        query getAllVariantPrices($cursor: String) {
          productVariants(first: 250, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            edges {
              node {
                sku
                price
              }
            }
          }
        }
        """
        price_map: dict[str, float] = {}
        cursor: Optional[str] = None

        while True:
            variables: dict = {}
            if cursor:
                variables["cursor"] = cursor

            data = self._graphql(query, variables)
            variants_data = data.get("productVariants", {})

            for edge in variants_data.get("edges", []):
                node = edge.get("node", {})
                raw_sku = (node.get("sku") or "").strip()
                raw_price = node.get("price")
                if not raw_sku or raw_price is None:
                    continue
                try:
                    price_map[raw_sku.upper()] = float(raw_price)
                except (TypeError, ValueError):
                    continue

            page_info = variants_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            time.sleep(_RATE_LIMIT_DELAY)

        log.info("[Shopify] Loaded %d live SKU→price pairs from store", len(price_map))
        return price_map

    def bulk_sync(
        self,
        rows: list[dict],
        location_id: str,
        sync_price: bool = True,
        sync_inventory: bool = True,
        respect_manual_edits: bool = True,
        manual_edit_tolerance: float = 1.0,
    ) -> list[dict]:
        """
        Sync price and/or inventory for a list of master sheet rows.

        Each row must have:
          - shopify_variant_id
          - selling_price
          - rrp (used as compare_at_price)
          - stock_qty (optional)
          - sku (for logging)

        Manual-edit protection (respect_manual_edits=True, default):
          Before pushing a new price, the current live Shopify price is
          fetched. If it differs from the value we would push by more than
          `manual_edit_tolerance` ZAR, we assume an operator made a manual
          edit in Shopify admin and SKIP the push for that row (logged at
          WARNING level). The row's stock can still be synced.

          This prevents the calculated supplier-formula price from silently
          overwriting deliberate manual discounts or markups.

        Returns list of failed rows with their error details. Skipped rows
        (due to manual-edit protection) are NOT in the failed list — they
        are logged separately and reported in the caller's summary.
        """
        failed: list[dict] = []
        total = len(rows)
        skipped_manual = 0

        for i, row in enumerate(rows, start=1):
            variant_id = row.get("shopify_variant_id")
            sku = row.get("sku", "?")

            if not variant_id:
                log.warning("[Shopify] Skipping %s — no shopify_variant_id", sku)
                failed.append({**row, "error": "missing shopify_variant_id"})
                continue

            try:
                # Fetch product_id + inventory_item_id + current live price.
                # _get_variant_info doesn't return price; do a small extra
                # GraphQL hit when manual-edit protection is enabled.
                product_id: Optional[str] = None
                inv_id: Optional[str] = None
                if sync_price or sync_inventory:
                    info = self._get_variant_info(variant_id)
                    product_id = info.get("product_id")
                    inv_id = info.get("inventory_item_id")
                    time.sleep(_RATE_LIMIT_DELAY)

                if sync_price:
                    price = float(row.get("selling_price") or 0)
                    rrp = row.get("rrp")
                    compare_at = float(rrp) if rrp else None

                    # ── Manual-edit protection ─────────────────────────
                    if respect_manual_edits:
                        live_price = self._get_variant_price(variant_id)
                        if live_price is not None:
                            # If we'd be pushing a value that diverges from
                            # the live price by more than tolerance, the
                            # operator (or another process) has manually
                            # adjusted it. Don't clobber.
                            if abs(live_price - price) > manual_edit_tolerance:
                                log.warning(
                                    "[Shopify] SKU %s — live price R%.2f differs from "
                                    "calculated R%.2f by R%.2f. Preserving manual edit, "
                                    "skipping price push.",
                                    sku, live_price, price, live_price - price,
                                )
                                skipped_manual += 1
                                # Still allow stock sync below
                                price = None  # signal: don't push
                            time.sleep(_RATE_LIMIT_DELAY)

                    if price is not None:
                        self.update_variant_price(
                            variant_id, price, compare_at, product_id=product_id
                        )
                        time.sleep(_RATE_LIMIT_DELAY)

                if sync_inventory:
                    qty = row.get("stock_qty")
                    if qty is not None and inv_id:
                        self.set_inventory_quantity(inv_id, location_id, int(qty))
                        time.sleep(_RATE_LIMIT_DELAY)

                log.debug("[Shopify] Synced %s (%d/%d)", sku, i, total)

            except Exception as e:
                log.error("[Shopify] Failed to sync %s: %s", sku, e)
                failed.append({**row, "error": str(e)})

        if failed:
            log.warning("[Shopify] %d/%d rows failed to sync", len(failed), total)
        if skipped_manual:
            log.warning(
                "[Shopify] %d/%d rows had their price-push skipped due to manual "
                "edits on Shopify. Set respect_manual_edits=False to override.",
                skipped_manual, total,
            )

        return failed

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _get_variant_info(self, variant_id: str) -> dict:
        """
        Fetch parent product GID and inventoryItem GID for a variant in one query.

        Returns:
            {"product_id": str | None, "inventory_item_id": str | None}
        """
        query = """
        query getVariantInfo($id: ID!) {
          productVariant(id: $id) {
            id
            product { id }
            inventoryItem { id }
          }
        }
        """
        data = self._graphql(query, {"id": variant_id})
        variant = data.get("productVariant") or {}
        return {
            "product_id": (variant.get("product") or {}).get("id"),
            "inventory_item_id": (variant.get("inventoryItem") or {}).get("id"),
        }

    def _get_variant_price(self, variant_id: str) -> Optional[float]:
        """
        Fetch the CURRENT live price for a single variant from Shopify.

        Used by bulk_sync()'s manual-edit protection — we compare what we're
        about to push against what's actually live, and skip the push when
        an operator has manually edited the price in Shopify admin.

        Returns None if the variant isn't found or the price can't be parsed.
        """
        query = """
        query getVariantPrice($id: ID!) {
          productVariant(id: $id) { price }
        }
        """
        try:
            data = self._graphql(query, {"id": variant_id})
            variant = data.get("productVariant")
            if not variant:
                return None
            raw = variant.get("price")
            return float(raw) if raw is not None else None
        except (ValueError, TypeError, ShopifyAPIError):
            return None

    def _graphql(self, query: str, variables: dict) -> dict:
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    self.graphql_url,
                    json={"query": query, "variables": variables},
                    headers=self.headers,
                    timeout=30,
                )
                resp.raise_for_status()
                body = resp.json()

                if "errors" in body:
                    raise ShopifyAPIError(f"GraphQL errors: {body['errors']}")

                # Check Shopify cost/throttle extension
                ext = body.get("extensions", {}).get("cost", {})
                if ext.get("throttleStatus", {}).get("currentlyAvailable", 1000) < 10:
                    wait = ext.get("throttleStatus", {}).get("restoreRate", 50) / 50
                    log.warning("[Shopify] Throttled — waiting %.1fs", wait)
                    time.sleep(wait)

                return body.get("data", {})

            except requests.HTTPError as e:
                if resp.status_code == 429 or resp.status_code >= 500:
                    delay = _RETRY_BASE_DELAY ** attempt
                    log.warning(
                        "[Shopify] HTTP %d on attempt %d/%d — retrying in %.0fs",
                        resp.status_code, attempt, _MAX_RETRIES, delay,
                    )
                    time.sleep(delay)
                else:
                    raise

        raise ShopifyAPIError(f"Shopify request failed after {_MAX_RETRIES} retries")


class ShopifyAPIError(Exception):
    pass
