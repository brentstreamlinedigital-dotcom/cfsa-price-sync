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
import urllib3
from playwright.async_api import Page, async_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

        # strategies that don't need a browser
        if strategy == "shopify_json":
            return self._fetch_shopify_json(url, scrape_cfg)
        if strategy == "pmw_json":
            return self._fetch_pmw_json(url, scrape_cfg)

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

                # dometic_lp and product_grid (Elementor) never reach networkidle
                # due to background JS fetches — use domcontentloaded instead.
                _no_idle = {"dometic_lp", "product_grid"}
                wait_event = "domcontentloaded" if strategy in _no_idle else "networkidle"
                await page.goto(url, wait_until=wait_event, timeout=30_000)

                if strategy == "table":
                    df = await self._extract_table(page)
                elif strategy == "pagination":
                    df = await self._extract_with_pagination(page)
                elif strategy == "product_grid":
                    df = await self._extract_product_grid(page, url, scrape_cfg)
                elif strategy == "product_pages":
                    df = self._fetch_product_pages(url, scrape_cfg)
                elif strategy == "dometic_lp":
                    df = await self._extract_dometic_lp(page, url, scrape_cfg)
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
    # PMW DataLayer JSON (WooCommerce — no browser needed)
    # ------------------------------------------------------------------

    def _fetch_pmw_json(self, brand_url: str, cfg: dict) -> pd.DataFrame:
        """
        Extract products from a WooCommerce brand/category page that embeds
        Google Analytics pmwDataLayer.products[N] = {...} JavaScript objects.

        Works on sites running the PixelYourSite / WooCommerce Google
        Analytics plugin (e.g. thr-outdoor.co.za/brand/dometic/).

        Returns a DataFrame with columns: sku, name, price
        """
        import json as _json
        import re as _re

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        # Collect all category pages (supports ?paged=N WordPress pagination)
        all_html: list[str] = []
        page_num = 1
        while page_num <= 20:
            page_url = brand_url if page_num == 1 else f"{brand_url.rstrip('/')}?paged={page_num}"
            try:
                resp = requests.get(page_url, timeout=20, verify=False, headers=headers)
                if resp.status_code == 404:
                    break
                all_html.append(resp.text)
                # Stop if no pmwDataLayer entries on this page
                if "pmwDataLayer.products[" not in resp.text:
                    break
            except Exception as e:
                log.error("[%s] pmw_json fetch failed page %d: %s",
                          self.config.get("supplier_key", "?"), page_num, e)
                break

            # If fewer than 24 entries, likely the last page
            count = resp.text.count("pmwDataLayer.products[")
            if count < 24:
                break
            page_num += 1

        rows = []
        seen_skus: set[str] = set()

        for html in all_html:
            # Use JSONDecoder.raw_decode to robustly parse each product object —
            # avoids regex issues with nested braces inside dyn_r_ids etc.
            for m in _re.finditer(r'pmwDataLayer\.products\[\d+\]\s*=\s*', html):
                pos = m.end()
                try:
                    obj, _ = _json.JSONDecoder().raw_decode(html, pos)
                    sku = str(obj.get("sku", "")).strip()
                    name = str(obj.get("name", "")).strip()
                    price = obj.get("price", "")
                    if sku and sku not in seen_skus:
                        seen_skus.add(sku)
                        rows.append({"sku": sku, "name": name, "price": str(price)})
                except (_json.JSONDecodeError, Exception):
                    pass

        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["sku", "name", "price"])
        log.info("[%s] pmw_json: %d products from %s",
                 self.config.get("supplier_key", "?"), len(df), brand_url)
        return df

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
                resp = requests.get(api_url, timeout=20, verify=False,
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
    # Product pages (WooCommerce: crawl category → fetch each product page)
    # ------------------------------------------------------------------

    def _fetch_product_pages(self, category_url: str, cfg: dict) -> pd.DataFrame:
        """
        Two-pass scrape:
        1. Collect all product page URLs from the category listing (static HTML)
        2. HTTP-GET each product page and extract SKU + price from static HTML
        Works well for WooCommerce stores where category pages load prices via JS
        but individual product pages serve price in static HTML.
        """
        import re as _re
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36"}
        brand_filter = cfg.get("brand_filter", "")
        rows = []
        visited = set()

        # Collect product URLs across all category pages
        product_urls = []
        page_url = category_url
        for _ in range(20):  # max 20 category pages
            try:
                resp = requests.get(page_url, timeout=20, verify=False, headers=headers)
                html = resp.text
            except Exception as e:
                log.error("[%s] product_pages: category fetch failed: %s",
                          self.config.get("supplier_key", "?"), e)
                break

            # Extract product links — try standard WooCommerce /product/ path first,
            # then fall back to woocommerce-LoopProduct-link anchors (custom permalinks)
            std_links = _re.findall(
                r'href="(https?://[^"]+/product/[^"?#]+)"', html
            )
            wc_links = _re.findall(
                r'href="(https?://[^"]+)"[^>]*class="[^"]*woocommerce-LoopProduct-link[^"]*"',
                html
            )
            # Also find links containing woocommerce-loop-product__link class
            wc_links2 = _re.findall(
                r'class="[^"]*woocommerce-LoopProduct-link[^"]*"[^>]*href="(https?://[^"?#]+)"',
                html
            )
            for link in (std_links or wc_links or wc_links2):
                if link not in visited:
                    visited.add(link)
                    product_urls.append(link)

            # Follow pagination
            next_page = _re.search(
                r'href="([^"]+)" [^>]*class="[^"]*next[^"]*"',
                html
            )
            if next_page:
                page_url = next_page.group(1)
            else:
                break

        log.info("[%s] product_pages: found %d product URLs",
                 self.config.get("supplier_key", "?"), len(product_urls))

        # Fetch each product page
        for product_url in product_urls:
            try:
                resp = requests.get(product_url, timeout=20, verify=False, headers=headers)
                html = resp.text

                # ── Primary: JSON-LD Product schema (most reliable across WC themes) ──
                ld_name = ld_sku = ld_price = ld_stock = ""
                for ld_m in _re.finditer(
                    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                    html, _re.DOTALL
                ):
                    try:
                        import json as _json
                        ld_data = _json.loads(ld_m.group(1))
                        items = (ld_data.get("@graph", [ld_data]) if isinstance(ld_data, dict)
                                 else (ld_data if isinstance(ld_data, list) else [ld_data]))
                        for item in items:
                            if item.get("@type") == "Product":
                                ld_name = item.get("name", "")
                                ld_sku  = str(item.get("sku", "") or item.get("mpn", "")).strip()
                                offers  = item.get("offers", {})
                                if isinstance(offers, list):
                                    offers = offers[0] if offers else {}
                                ld_price = str(offers.get("price", "")).replace(",", "")
                                avail    = str(offers.get("availability", ""))
                                ld_stock = ("In Stock" if "InStock" in avail
                                            else "Out of Stock" if "OutOfStock" in avail
                                            else "Unknown")
                                break
                    except Exception:
                        pass
                    if ld_name:
                        break

                # Clean up supplier-name suffixes from JSON-LD names (e.g. "SMDZ-TR42S - SnoMaster")
                if ld_name:
                    ld_name = _re.sub(r'\s*-\s*SnoMaster\s*$', '', ld_name, flags=_re.IGNORECASE).strip()

                # ── Fallback: regex title from h1 ──────────────────────────────────
                title = ld_name
                if not title:
                    title_m = _re.search(
                        r'<h1[^>]*class="[^"]*(?:product_title|entry-title|elementor-heading)[^"]*"[^>]*>(.*?)</h1>',
                        html, _re.DOTALL
                    )
                    title = _re.sub('<[^>]+>', '', title_m.group(1)).strip() if title_m else ""

                # Brand filter — applied to resolved title
                if brand_filter and brand_filter.lower() not in title.lower():
                    continue

                # ── SKU — JSON-LD → <span class="sku"> → model-code in title → URL slug ─
                sku = ld_sku
                # Strip "SKU: " label prefix that some themes put in the ld+json value
                sku = _re.sub(r'^[Ss][Kk][Uu]\s*:\s*', '', sku).strip()
                if not sku:
                    sku_m = _re.search(
                        r'<span class="sku"[^>]*>(.*?)</span>',
                        html, _re.DOTALL
                    )
                    if sku_m:
                        sku = _re.sub('<[^>]+>', '', sku_m.group(1)).strip()
                        sku = _re.sub(r'^[Ss][Kk][Uu]\s*:\s*', '', sku).strip()
                if not sku and title:
                    # Extract model code from title — look for patterns like
                    # "SMLS-100D", "SMDZ-LS25", "CFX3-45", "MR40F-G4NS" etc.
                    # Pattern: 2-6 uppercase letters, hyphen, 2-8 alphanumeric chars
                    code_m = _re.search(r'\b([A-Z]{2,6}-[A-Z0-9]{2,10})\b', title)
                    if code_m:
                        sku = code_m.group(1).upper()
                if not sku:
                    # Last resort: derive from URL slug (often descriptive, not a real SKU)
                    slug_m = _re.search(r'/product/([^/?#]+)/?', product_url)
                    if slug_m:
                        sku = slug_m.group(1).upper()

                # ── Price — JSON-LD → product-page-price CSS class → first bdi ──
                price = ld_price
                if not price:
                    main_m = _re.search(
                        r'class="[^"]*product-page-price[^"]*"[^>]*>.*?'
                        r'<bdi>\s*<span[^>]*>[^<]*</span>\s*([0-9,]+(?:\.[0-9]+)?)\s*</bdi>',
                        html, _re.DOTALL
                    )
                    if main_m:
                        price = main_m.group(1).replace(",", "")
                if not price:
                    price_m = _re.search(
                        r'<ins[^>]*>.*?<bdi>\s*<span[^>]*>[^<]*</span>\s*([0-9,]+(?:\.[0-9]+)?)\s*</bdi>|'
                        r'<bdi>\s*<span[^>]*>[^<]*</span>\s*([0-9,]+(?:\.[0-9]+)?)\s*</bdi>',
                        html, _re.DOTALL
                    )
                    if price_m:
                        price = (price_m.group(1) or price_m.group(2) or "").strip().replace(",", "")

                if title:
                    rows.append({
                        "sku":          sku,
                        "description":  title,
                        "price":        price,
                        "stock_status": ld_stock or "",
                        "url":          product_url,
                    })
            except Exception as e:
                log.warning("[%s] product_pages: failed to fetch %s: %s",
                            self.config.get("supplier_key", "?"), product_url, e)

        df = pd.DataFrame(rows) if rows else pd.DataFrame()
        log.info("[%s] product_pages: %d products scraped",
                 self.config.get("supplier_key", "?"), len(df))
        return df

    # ------------------------------------------------------------------
    # Dometic landing-page extractor (Next.js / React — Playwright needed)
    # ------------------------------------------------------------------

    async def _extract_dometic_lp(self, page: Page, url: str, cfg: dict) -> pd.DataFrame:
        """
        Extract products from a Dometic.com landing page (Next.js SSR).

        Strategy (in order of preference):
        1. JSON-LD <script type="application/ld+json"> Product schemas
        2. __NEXT_DATA__ embedded JSON parsed for product + price nodes
        3. Rendered DOM product cards (class-name heuristics)

        Returns DataFrame with columns: sku, name, price
        """
        import json as _json
        import re as _re

        # Use domcontentloaded + explicit wait — dometic.com App Router never reaches networkidle
        # via Playwright because it has persistent background fetches.
        try:
            await page.wait_for_selector('[data-slot="product-card-link"]', timeout=20_000)
        except Exception:
            log.warning("[%s] dometic_lp: timed out waiting for product cards on %s",
                        self.config.get("supplier_key", "?"), url)

        rows: list[dict] = []

        # ── Primary: [data-slot="product-card-link"] cards ─────────────
        # Dometic.com (Next.js App Router) renders product cards client-side.
        # Each card has an anchor with aria-label "Product Name (SKU) – …"
        # and a visible price in the card text.
        dom_rows: list[dict] = await page.evaluate(
            """
            () => {
                const results = [];
                const links = document.querySelectorAll('[data-slot="product-card-link"]');
                links.forEach(link => {
                    const ariaLabel = link.getAttribute('aria-label') || '';
                    // "Dometic CFX5 25 (97000050759) – View product details in Electric Coolers"
                    const skuM = ariaLabel.match(/\\(([0-9A-Z][0-9A-Za-z\\-]+)\\)/);
                    const sku  = skuM ? skuM[1] : '';
                    const name = ariaLabel.split('(')[0].trim().replace(/\\s*[\\u2013\\u2014-].*$/, '').trim();

                    // Price lives in the card container as visible text "R 9,575.00"
                    const card  = link.closest('li, article, [data-slot="product-card"]') || link.parentElement;
                    const cardText = card ? card.innerText : '';
                    const priceM = cardText.match(/R\\s*([0-9][0-9,]+(\\.[0-9]{2})?)/);
                    const price  = priceM ? priceM[1].replace(/,/g, '') : '';

                    if (name && price) {
                        results.push({ sku, name, price });
                    }
                });
                return results;
            }
            """
        )
        for item in dom_rows:
            rows.append({
                "sku":   item.get("sku", "").strip(),
                "name":  item.get("name", "").strip(),
                "price": item.get("price", "").strip(),
            })

        if rows:
            # Deduplicate by sku
            seen: set[str] = set()
            rows = [r for r in rows if not (r["sku"] in seen or seen.add(r["sku"]))]  # type: ignore[func-returns-value]
            log.info("[%s] dometic_lp: found %d products via product-card-link", self.config.get("supplier_key", "?"), len(rows))
            return pd.DataFrame(rows)

        # ── Fallback: JSON-LD (older Dometic pages / Pages Router sites) ──
        ld_texts: list[str] = await page.evaluate(
            "() => Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]'))"
            ".map(s => s.textContent || '')"
        )
        for text in ld_texts:
            try:
                data = _json.loads(text)
                items = data.get("@graph", [data]) if isinstance(data, dict) else (data if isinstance(data, list) else [data])
                for item in items:
                    if item.get("@type") == "Product":
                        name   = item.get("name", "")
                        sku    = item.get("sku") or item.get("mpn") or ""
                        offers = item.get("offers", {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        price = str(offers.get("price") or offers.get("lowPrice") or "")
                        if name and price:
                            rows.append({"sku": str(sku).strip(), "name": name.strip(), "price": price.replace(",", "")})
            except Exception:
                pass

        if not rows:
            log.warning("[%s] dometic_lp: no products found on %s",
                        self.config.get("supplier_key", "?"), url)

        log.info("[%s] dometic_lp: %d products scraped from %s",
                 self.config.get("supplier_key", "?"), len(rows), url)
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["sku", "name", "price"])

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
        """
        Scrape product cards from a WooCommerce storefront listing page.

        Handles two WooCommerce rendering modes:
        - Standard WC loop: products have .woocommerce-loop-product__title
        - Elementor loop builder (e.g. SnoMaster): products use .e-loop-item.product
          with prices loaded via JS after page render — requires Playwright.
        """
        brand_filter = cfg.get("brand_filter", "")
        all_rows = []
        page_num = 1
        max_pages = 20

        while page_num <= max_pages:
            # Wait for WooCommerce products — standard WC loop OR Elementor loop
            try:
                await page.wait_for_selector(
                    "ul.products li.product, .products li.product, "
                    ".woocommerce-loop-product__title, .product-title, "
                    ".e-loop-item.product, li.type-product",
                    timeout=25_000,
                )
            except Exception:
                log.warning(
                    "[%s] product_grid: timed out waiting for products on page %d",
                    self.config.get("supplier_key", "?"), page_num,
                )
                snippet = await page.evaluate("() => document.body.innerText.substring(0, 300)")
                log.debug("[%s] Page snippet: %s", self.config.get("supplier_key", "?"), snippet)
                break

            # Wait for prices to load — Elementor fetches them via AJAX after
            # the DOM is ready, so .woocommerce-Price-amount may appear late.
            # Timeout is non-fatal: if no prices load we still get titles + URLs.
            try:
                await page.wait_for_selector(
                    ".woocommerce-Price-amount bdi", timeout=8_000
                )
            except Exception:
                log.debug(
                    "[%s] product_grid: no price elements appeared on page %d "
                    "(JS prices may not be available)",
                    self.config.get("supplier_key", "?"), page_num,
                )

            rows = await page.evaluate(
                """
                () => {
                    const results = [];

                    // ── Standard WC loop ──────────────────────────────────────
                    // Works for most WooCommerce themes (Storefront, Flatsome, etc.)
                    const stdTitles = document.querySelectorAll(
                        '.woocommerce-loop-product__title, ' +
                        'h2.product-title, h3.product-title, ' +
                        '.product-name'
                    );
                    stdTitles.forEach(titleEl => {
                        const item = titleEl.closest('li, article, .product, [class*="product"]');
                        const priceEl = item ? item.querySelector(
                            '.price ins .woocommerce-Price-amount bdi, ' +
                            '.price .woocommerce-Price-amount bdi, ' +
                            '.woocommerce-Price-amount bdi'
                        ) : null;
                        const skuEl  = item ? item.querySelector('[data-sku], .sku') : null;
                        const linkEl = item ? item.querySelector(
                            'a.woocommerce-LoopProduct-link, a[href*="/product/"]'
                        ) : null;
                        results.push({
                            description: titleEl.innerText.trim(),
                            price: priceEl ? priceEl.innerText.replace(/[^0-9.,]/g, '').trim() : '',
                            sku:   skuEl  ? (skuEl.dataset.sku || skuEl.innerText.trim()) : '',
                            url:   linkEl ? linkEl.href : '',
                            _source: 'std',
                        });
                    });

                    // ── Elementor loop builder (e.g. SnoMaster) ───────────────
                    // Elementor replaces the WC loop template with its own Loop
                    // Builder widget. Each product is an <li class="e-loop-item product …">.
                    // The WC price widget still renders .woocommerce-Price-amount bdi
                    // once JS has executed, and the title widget renders an <h> tag
                    // inside .elementor-widget-woocommerce-product-title.
                    if (results.length === 0) {
                        const loopItems = document.querySelectorAll(
                            '.e-loop-item.product, li.type-product'
                        );
                        loopItems.forEach(item => {
                            // Title — inside Elementor's WC product title widget
                            const titleEl = item.querySelector(
                                '.elementor-widget-woocommerce-product-title h1, ' +
                                '.elementor-widget-woocommerce-product-title h2, ' +
                                '.elementor-widget-woocommerce-product-title h3, ' +
                                '.elementor-widget-woocommerce-product-title h4, ' +
                                '.elementor-heading-title, ' +
                                'h2, h3'
                            );
                            // Price — WC price widget renders standard .woocommerce-Price-amount
                            // bdi even inside Elementor (the widget is a WC shortcode wrapper)
                            const priceEl = item.querySelector(
                                '.woocommerce-Price-amount bdi'
                            );
                            // Link — product permalink is on the featured-image or title anchor
                            const linkEl = item.querySelector(
                                'a[href*="/product/"], a.woocommerce-LoopProduct-link'
                            );
                            // SKU — may be in a data attribute or .sku span (often not in loop)
                            const skuEl = item.querySelector('[data-sku], .sku');

                            if (!titleEl) return;
                            results.push({
                                description: titleEl.innerText.trim(),
                                price: priceEl ? priceEl.innerText.replace(/[^0-9.,]/g, '').trim() : '',
                                sku:   skuEl ? (skuEl.dataset.sku || skuEl.innerText.trim()) : '',
                                url:   linkEl ? linkEl.href : '',
                                _source: 'elementor',
                            });
                        });
                    }

                    return results;
                }
                """
            )

            if not rows:
                log.warning(
                    "[%s] product_grid: no products found on page %d",
                    self.config.get("supplier_key", "?"), page_num,
                )
                break

            for row in rows:
                if brand_filter and brand_filter.lower() not in row.get("description", "").lower():
                    continue
                row.pop("_source", None)
                all_rows.append(row)

            log.debug(
                "[%s] product_grid page %d: %d products",
                self.config.get("supplier_key", "?"), page_num, len(rows),
            )

            # Next page — handle both WC pagination and Elementor pagination widget
            next_link = page.locator(
                'a.next.page-numbers, '
                '.woocommerce-pagination a.next, '
                '.e-load-more-button, '
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
                await page.goto(next_href, wait_until="domcontentloaded", timeout=30_000)
                page_num += 1
            except Exception:
                break

        df = pd.DataFrame(all_rows) if all_rows else pd.DataFrame()
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
