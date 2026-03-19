"""
Base scraper interface.
All supplier scrapers must implement this contract.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class BaseScraper(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.scrape_config = config.get("scrape_fallback", {})

    @abstractmethod
    async def scrape(self) -> pd.DataFrame:
        """
        Scrape the supplier site and return a raw DataFrame.
        Column names should match the supplier's config column_map keys.
        """
        ...
