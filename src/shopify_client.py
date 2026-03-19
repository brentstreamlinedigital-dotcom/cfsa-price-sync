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
    ) -> bool:
        """
        Update the price (and optional compare-at price) for a single variant.

        Args:
            variant_id:        Shopify variant GID, e.g. "gid://shopify/ProductVariant/1234"
            price:             New selling price in ZAR
            compare_at_price:  Strike-through price (usually the RRP)
        """
        mutation = """
        mutation productVariantUpdate($input: ProductVariantInput!) {
          productVariantUpdate(input: $input) {
            productVariant {
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
        variables: dict = {
            "input": {
                "id": variant_id,
                "price": f"{price:.2f}",
            }
        }
        if compare_at_price is not None:
            variables["input"]["compareAtPrice"] = f"{compare_at_price:.2f}"

        data = self._graphql(mutation, variables)
        errors = data.get("productVariantUpdate", {}).get("userErrors", [])
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
        query = """
        query getVariant($id: ID!) {
          productVariant(id: $id) {
            id
            inventoryItem {
              id
            }
          }
        }
        """
        data = self._graphql(query, {"id": variant_id})
        variant = data.get("productVariant")
        if not variant:
            return None
        return variant.get("inventoryItem", {}).get("id")

    def bulk_sync(
        self,
        rows: list[dict],
        location_id: str,
        sync_price: bool = True,
        sync_inventory: bool = True,
    ) -> list[dict]:
        """
        Sync price and/or inventory for a list of master sheet rows.

        Each row must have:
          - shopify_variant_id
          - selling_price
          - rrp (used as compare_at_price)
          - stock_qty (optional)
          - sku (for logging)

        Returns list of failed rows with their error details.
        """
        failed: list[dict] = []
        total = len(rows)

        for i, row in enumerate(rows, start=1):
            variant_id = row.get("shopify_variant_id")
            sku = row.get("sku", "?")

            if not variant_id:
                log.warning("[Shopify] Skipping %s — no shopify_variant_id", sku)
                failed.append({**row, "error": "missing shopify_variant_id"})
                continue

            try:
                if sync_price:
                    price = float(row.get("selling_price") or 0)
                    rrp = row.get("rrp")
                    compare_at = float(rrp) if rrp else None
                    self.update_variant_price(variant_id, price, compare_at)

                if sync_inventory:
                    qty = row.get("stock_qty")
                    if qty is not None:
                        inv_id = self.get_variant_inventory_item_id(variant_id)
                        if inv_id:
                            self.set_inventory_quantity(inv_id, location_id, int(qty))
                            time.sleep(_RATE_LIMIT_DELAY)

                log.debug("[Shopify] Synced %s (%d/%d)", sku, i, total)
                time.sleep(_RATE_LIMIT_DELAY)

            except Exception as e:
                log.error("[Shopify] Failed to sync %s: %s", sku, e)
                failed.append({**row, "error": str(e)})

        if failed:
            log.warning("[Shopify] %d/%d rows failed to sync", len(failed), total)

        return failed

    # ------------------------------------------------------------------
    # Low-level GraphQL with retry
    # ------------------------------------------------------------------

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
