"""
Playwright-based scraper for supplier websites.

Supports:
- table        — extract the largest/first HTML table on the page
- pagination   — follow next-page links until exhausted
- shopify_json — fetch Shopify collection products.json (no browser needed)
- product_grid — scrape WooCommerce/Shopify product cards from listing pages
- login        — form-based auth with credentials from Secret Manager

Usage:
    scraper = PlaywrightScraper(config.model_dump())
    df = asyncio.run(scraper.scrape())
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import pandas as pd
import requests
from playwright.async_api import Page, async_playwright

from .base_scraper import BaseScraper

log = logging.getLogger(__name__)


class PlaywrightScraper(BaseScraper):

    async def scrape(self) -> pd.DataFrame:
        scrape_cfg = self.scrape_config
        url = scrape_cfg.get("url", "")
        strategy = scrape_cfg.get("strategy", "table")
        auth = scrape_cfg.get("auth")

        if not url:
            raise ValueError("scrape_fallback.url is required")

        # shopify_json doesn't need a browser
        if strategy == "shopify_json":
            return self._fetch_shopify_json(url, scrape_cfg)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            try:
                if auth:
                    await self._login(page, auth)

                await page.goto(url, wait_until="networkidle", timeout=30_000)

                if strategy == "table":
                    df = await self._extract_table(page)
                elif strategy == "pagination":
                    df = await self._extract_with_pagination(page)
                elif strategy == "product_grid":
                    df = await self._extract_product_grid(page, url, scrape_cfg)
                else:
                    raise ValueError(f"Unknown scrape strategy: {strategy!r}")

                log.info(
                    "[%s] Scraped %d rows from %s",
                    self.config.get("supplier_key", "?"), len(df), url,
                )
                return df

            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # Shopify JSON (no browser)
    # ------------------------------------------------------------------

    def _fetch_shopify_json(self, collection_url: str, cfg: dict) -> pd.DataFrame:
        """Fetch all products from a Shopify store's collection products.json endpoint."""
        # Strip trailing slash and query params from collection URL
        base = re.sub(r'\?.*$', '', collection_url.rstrip('/'))
        brand_filter = cfg.get("brand_filter", "")  # optional: only keep products matching brand
        rows = []
        page = 1

        while True:
            api_url = f"{base}/products.json?limit=250&page={page}"
            try:
                resp = requests.get(api_url, timeout=20,
                                    headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.error("[%s] shopify_json fetch failed page %d: %s",
                          self.config.get("supplier_key", "?"), page, e)
                break

            products = data.get("products", [])
            if not products:
                break

            for product in products:
                title = product.get("title", "")
                # Optional brand filter — skip products not matching
                if brand_filter and brand_filter.lower() not in title.lower():
                    continue
                for variant in product.get("variants", []):
                    rows.append({
                        "sku": variant.get("sku", ""),
                        "description": title,
                        "price": variant.get("price", ""),
                        "compare_at_price": variant.get("compare_at_price", ""),
                        "available": variant.get("available", True),
                        "inventory_quantity": variant.get("inventory_quantity", 0),
                        "variant_title": variant.get("title", ""),
                    })

            # Shopify returns < 250 items on last page
            if len(products) < 250:
                break
            page += 1

        df = pd.DataFrame(rows)
        log.info("[%s] shopify_json: %d variants from %s",
                 self.config.get("supplier_key", "?"), len(df), collection_url)
        return df

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _login(self, page: Page, auth: dict) -> None:
        """Form-based login. Credentials fetched from Google Secret Manager."""
        username = self._get_secret(auth["username_secret"])
        password = self._get_secret(auth["password_secret"])

        await page.goto(auth["login_url"], wait_until="networkidle", timeout=20_000)
        await page.fill(auth["username_field"], username)
        await page.fill(auth["password_field"], password)
        await page.click('[type=submit]')
        await page.wait_for_load_state("networkidle", timeout=15_000)
        log.debug("Login successful for %s", auth.get("login_url"))

    @staticmethod
    def _get_secret(secret_key: str) -> str:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/-/secrets/{secret_key}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8")

    # ------------------------------------------------------------------
    # Table extraction
    # ------------------------------------------------------------------

    async def _extract_table(self, page: Page) -> pd.DataFrame:
        await page.wait_for_selector("table", timeout=15_000)

        # Extract via JS for speed — grabs the table with the most rows
        data: list[list[str]] = await page.evaluate(
            """
            () => {
                const tables = Array.from(document.querySelectorAll('table'));
                if (!tables.length) return [];
                const target = tables.reduce(
                    (best, t) => t.rows.length > best.rows.length ? t : best
                );
                return Array.from(target.rows).map(row =>
                    Array.from(row.cells).map(c => c.innerText.trim())
                );
            }
            """
        )

        return self._rows_to_df(data)

    async def _extract_with_pagination(self, page: Page) -> pd.DataFrame:
        all_dfs: list[pd.DataFrame] = []
        page_num = 1

        while True:
            df = await self._extract_table(page)
            if not df.empty:
                all_dfs.append(df)
                log.debug("Scraped page %d (%d rows)", page_num, len(df))

            # Look for common next-page patterns
            next_btn = page.locator(
                '[aria-label="Next page"], '
                '.pagination-next, '
                'a:text("Next"), '
                'a:text("»"), '
                'button:text("Next")'
            ).first

            try:
                if not await next_btn.count() or await next_btn.is_disabled():
                    break
                await next_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)
                page_num += 1
            except Exception:
                break

        if not all_dfs:
            return pd.DataFrame()

        combined = pd.concat(all_dfs, ignore_index=True)
        return combined

    # ------------------------------------------------------------------
    # Product grid (WooCommerce / Shopify storefront)
    # ------------------------------------------------------------------

    async def _extract_product_grid(self, page: Page, base_url: str, cfg: dict) -> pd.DataFrame:
        """Scrape product cards from a WooCommerce or Shopify storefront listing page."""
        brand_filter = cfg.get("brand_filter", "")
        all_rows = []
        page_num = 1
        max_pages = 20  # safety cap

        while page_num <= max_pages:
            await page.wait_for_load_state("networkidle", timeout=30_000)

            rows = await page.evaluate(
                """
                () => {
                    const results = [];
                    // WooCommerce: ul.products li.product
                    const wc = document.querySelectorAll('ul.products li.product, .products-grid .product-item');
                    if (wc.length > 0) {
                        wc.forEach(item => {
                            const titleEl = item.querySelector(
                                '.woocommerce-loop-product__title, h2.product-title, h3, .product-name a'
                            );
                            const priceEl = item.querySelector(
                                '.price ins .woocommerce-Price-amount bdi, ' +
                                '.price .woocommerce-Price-amount bdi, ' +
                                '.woocommerce-Price-amount bdi'
                            );
                            const skuEl = item.querySelector('[data-sku], .sku');
                            const linkEl = item.querySelector('a.woocommerce-LoopProduct-link, a');
                            results.push({
                                description: titleEl ? titleEl.innerText.trim() : '',
                                price: priceEl ? priceEl.innerText.replace(/[^0-9.,]/g, '').trim() : '',
                                sku: skuEl ? (skuEl.dataset.sku || skuEl.innerText.trim()) : '',
                                url: linkEl ? linkEl.href : ''
                            });
                        });
                    }
                    return results;
                }
                """
            )

            if not rows:
                log.warning("[%s] product_grid: no products found on page %d",
                            self.config.get("supplier_key", "?"), page_num)
                break

            for row in rows:
                if brand_filter and brand_filter.lower() not in row.get("description", "").lower():
                    continue
                all_rows.append(row)

            log.debug("[%s] product_grid page %d: %d products",
                      self.config.get("supplier_key", "?"), page_num, len(rows))

            # Try to navigate to next page
            next_link = page.locator(
                'a.next.page-numbers, '
                '.woocommerce-pagination a:text("→"), '
                '[aria-label="Next page"], '
                'a:text("Next →"), '
                'a:text("Next")'
            ).first

            try:
                count = await next_link.count()
                if not count:
                    break
                next_href = await next_link.get_attribute("href")
                if not next_href:
                    break
                await page.goto(next_href, wait_until="networkidle", timeout=30_000)
                page_num += 1
            except Exception:
                break

        df = pd.DataFrame(all_rows) if all_rows else pd.DataFrame()
        # Clean prices — remove commas e.g. "8,495.00" → "8495.00"
        if "price" in df.columns:
            df["price"] = df["price"].str.replace(",", "", regex=False)
        return df

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rows_to_df(rows: list[list[str]]) -> pd.DataFrame:
        if not rows or len(rows) < 2:
            return pd.DataFrame()

        headers = rows[0]
        data = rows[1:]

        # Normalise column count
        col_count = len(headers)
        padded = [r + [""] * col_count for r in data]
        padded = [r[:col_count] for r in padded]

        df = pd.DataFrame(padded, columns=headers)
        df = df.replace("", None)
        df = df.dropna(how="all")
        return df.reset_index(drop=True)


def run_scraper(config: dict) -> pd.DataFrame:
    """Synchronous entry point for use from main.py."""
    scraper = PlaywrightScraper(config)
    return asyncio.run(scraper.scrape())
