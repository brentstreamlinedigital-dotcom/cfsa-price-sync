"""
Playwright-based scraper for supplier websites.

Supports:
- table    — extract the largest/first HTML table on the page
- pagination — follow next-page links until exhausted
- login    — form-based auth with credentials from Secret Manager

Usage:
    scraper = PlaywrightScraper(config.model_dump())
    df = asyncio.run(scraper.scrape())
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import pandas as pd
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

        # Stack pages, keeping headers from first page only
        combined = pd.concat(all_dfs, ignore_index=True)
        return combined

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
