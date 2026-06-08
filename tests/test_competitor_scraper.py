"""
Unit tests for scrapers/competitor_analysis/scraper.py

All Playwright calls are mocked — no browser is launched, no network requests made.
Tests cover: fuzzy matching gate, price extraction, failure isolation,
concurrent execution, and the competitor config loader.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapers.competitor_analysis.scraper import (
    ProductScrapeOutcome,
    ScrapeResult,
    _extract_key_terms,
    _fuzzy_score,
    _parse_price,
    _scrape_one_competitor,
    build_search_queries,
    load_competitors,
    scrape_product,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_competitor(name="takealot", priority=1, enabled=True) -> dict:
    return {
        "name": name,
        "display_name": name.title(),
        "base_url": f"https://www.{name}.com",
        "search_url_pattern": f"https://www.{name}.com/search?q={{query}}",
        "priority": priority,
        "enabled": enabled,
    }


def _make_page_mock(evaluate_return=None, goto_side_effect=None) -> AsyncMock:
    """Return a mock Playwright page object."""
    page = AsyncMock()
    if goto_side_effect:
        page.goto.side_effect = goto_side_effect
    else:
        page.goto.return_value = None
    page.evaluate.return_value = evaluate_return or []
    page.route.return_value = None
    page.close.return_value = None
    return page


def _make_browser_mock(page: AsyncMock) -> AsyncMock:
    browser = AsyncMock()
    browser.new_page.return_value = page
    browser.close.return_value = None
    return browser


# ---------------------------------------------------------------------------
# _parse_price
# ---------------------------------------------------------------------------

class TestParsePrice:
    def test_standard_zar(self):
        assert _parse_price("R1,299.00") == pytest.approx(1299.0)

    def test_no_cents(self):
        assert _parse_price("R2500") == pytest.approx(2500.0)

    def test_space_after_r(self):
        assert _parse_price("R 999.99") == pytest.approx(999.99)

    def test_embedded_in_text(self):
        assert _parse_price("Price: R5,999.00 (incl VAT)") == pytest.approx(5999.0)

    def test_no_price_returns_none(self):
        assert _parse_price("No price here") is None

    def test_zero_price_is_parsed(self):
        # parse works — callers are responsible for filtering zero
        assert _parse_price("R0.00") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _extract_key_terms / build_search_queries
# ---------------------------------------------------------------------------

class TestExtractKeyTerms:
    def test_known_brand_kept(self):
        result = _extract_key_terms("Engel 60L Portable Fridge Freezer", "MD60F")
        assert "Engel" in result

    def test_model_code_kept(self):
        result = _extract_key_terms("Engel 60L Portable Fridge Freezer MD60F White", "MD60F")
        assert "MD60F" in result or "md60f" in result.lower()

    def test_capacity_kept(self):
        result = _extract_key_terms("Dometic CFX3 55 Litre Dual Zone Fridge", "CFX3-55")
        assert "55" in result

    def test_noise_words_stripped(self):
        result = _extract_key_terms("Portable Camping Freezer Fridge Unit 45L", "SL45")
        # Should not contain generic noise words but should keep capacity and model code
        lower = result.lower()
        assert "portable" not in lower
        assert "camping" not in lower

    def test_empty_description_returns_sku(self):
        result = _extract_key_terms("", "MD60F")
        assert "MD60F" in result

    def test_dometic_cfx3_extracts_correctly(self):
        result = _extract_key_terms("Dometic CFX3 55IM Dual Zone Portable Fridge Freezer", "CFX3-55IM")
        assert "Dometic" in result
        assert "cfx3" in result.lower() or "CFX3" in result

    def test_snomaster_model_extracted(self):
        result = _extract_key_terms("SnoMaster 45L Stainless Steel Fridge BD-45", "BD-45")
        assert "45" in result


class TestBuildSearchQueries:
    def test_returns_list(self):
        result = build_search_queries("Engel MD60F 60L Fridge", "MD60F")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_no_duplicates(self):
        result = build_search_queries("Engel MD60F 60L Fridge", "MD60F")
        assert len(result) == len(set(result))

    def test_sku_appears_as_fallback(self):
        # SKU should always be somewhere in the query list as a fallback
        result = build_search_queries("Engel 60L Portable Fridge Freezer", "MD60F")
        combined = " ".join(result)
        assert "MD60F" in combined

    def test_empty_sku_doesnt_crash(self):
        result = build_search_queries("Engel MD60F 60L Fridge", "")
        assert len(result) >= 1

    def test_fallback_queries_for_obscure_name(self):
        # A product with no recognised brand should still get multiple queries
        result = build_search_queries("Generic 40L Camping Fridge XR40-B", "XR40-B")
        assert len(result) >= 2


# ---------------------------------------------------------------------------
# _fuzzy_score
# ---------------------------------------------------------------------------

class TestFuzzyScore:
    def test_identical_strings_score_100(self):
        assert _fuzzy_score("Engel MD60F Fridge", "Engel MD60F Fridge") == pytest.approx(100.0)

    def test_very_different_strings_score_low(self):
        score = _fuzzy_score("Engel MD60F Fridge", "Toyota Hilux Bakkie")
        assert score < 30

    def test_partial_match_scores_well(self):
        # "Engel 60L" vs "Engel MD60F 60 Litre Fridge Freezer"
        score = _fuzzy_score("Engel 60L Fridge MD60F", "Engel MD60F 60 Litre Fridge Freezer")
        assert score >= 60

    def test_case_insensitive(self):
        lower = _fuzzy_score("engel md60f", "Engel MD60F Fridge")
        upper = _fuzzy_score("ENGEL MD60F", "Engel MD60F Fridge")
        assert lower == upper

    def test_empty_query_returns_zero(self):
        assert _fuzzy_score("", "some product") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _scrape_one_competitor — unit-level with mocked browser
# ---------------------------------------------------------------------------

class TestScrapeOneCompetitor:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_good_match_returns_price(self):
        """JS returns a strong match → ScrapeResult with price."""
        candidates = [
            {"title": "Engel MD60F 60L Fridge Freezer", "price": 12999.0,
             "url": "https://takealot.com/engel-60l", "source": "json-ld"},
        ]
        page = _make_page_mock(evaluate_return=candidates)
        browser = _make_browser_mock(page)
        comp = _make_competitor("takealot")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = self._run(
                _scrape_one_competitor(
                    competitor=comp,
                    queries=["Engel MD60F 60L Fridge"],
                    match_threshold=70.0,
                    max_results=5,
                    browser=browser,
                )
            )

        assert result.status == "OK"
        assert result.price == pytest.approx(12999.0)
        assert result.competitor == "takealot"

    def test_below_threshold_returns_no_match(self):
        """JS returns a product with low fuzzy score → NO_MATCH_FOUND."""
        candidates = [
            {"title": "Totally Unrelated Product Name XYZ", "price": 500.0,
             "url": "https://takealot.com/xyz", "source": "css-card"},
        ]
        page = _make_page_mock(evaluate_return=candidates)
        browser = _make_browser_mock(page)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = self._run(
                _scrape_one_competitor(
                    competitor=_make_competitor("takealot"),
                    queries=["Engel MD60F Fridge Freezer 60L"],
                    match_threshold=80.0,
                    max_results=5,
                    browser=browser,
                )
            )

        assert result.status == "NO_MATCH_FOUND"
        assert result.price is None

    def test_empty_results_returns_no_match(self):
        """JS returns no candidates → NO_MATCH_FOUND."""
        page = _make_page_mock(evaluate_return=[])
        browser = _make_browser_mock(page)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = self._run(
                _scrape_one_competitor(
                    competitor=_make_competitor("trailed"),
                    queries=["Dometic CFX3 55"],
                    match_threshold=80.0,
                    max_results=5,
                    browser=browser,
                )
            )

        assert result.status == "NO_MATCH_FOUND"

    def test_goto_exception_returns_scrape_failed(self):
        """Page.goto raises every attempt → SCRAPE_FAILED after all retries."""
        page = _make_page_mock(goto_side_effect=Exception("Connection refused"))
        browser = _make_browser_mock(page)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = self._run(
                _scrape_one_competitor(
                    competitor=_make_competitor("futurama"),
                    queries=["Snomaster 45L"],
                    match_threshold=80.0,
                    max_results=5,
                    browser=browser,
                )
            )

        assert result.status == "SCRAPE_FAILED"
        assert result.price is None
        # All retry attempts must have been tried (_MAX_RETRIES = 2)
        assert browser.new_page.call_count == 2

    def test_retries_on_transient_failure_succeeds(self):
        """Fails once then succeeds on second attempt → returns OK result."""
        good_candidates = [
            {"title": "Engel MD60F Fridge Freezer 60L", "price": 11999.0,
             "url": "https://site.com/engel", "source": "css-card"},
        ]
        # new_page is called once per attempt; first page raises, second succeeds
        fail_page = _make_page_mock(goto_side_effect=Exception("Timeout"))
        good_page = _make_page_mock(evaluate_return=good_candidates)

        browser = AsyncMock()
        browser.new_page.side_effect = [fail_page, good_page]
        browser.close.return_value = None

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = self._run(
                _scrape_one_competitor(
                    competitor=_make_competitor("futurama"),
                    queries=["Engel MD60F Fridge"],
                    match_threshold=70.0,
                    max_results=5,
                    browser=browser,
                )
            )

        assert result.status == "OK"
        assert result.price == pytest.approx(11999.0)
        # _MAX_RETRIES = 2 — one failed attempt, one successful attempt
        assert browser.new_page.call_count == 2

    def test_missing_search_url_returns_scrape_failed(self):
        """Competitor with no search_url_pattern → SCRAPE_FAILED immediately."""
        comp = {"name": "bad_comp", "enabled": True}  # no search_url_pattern
        browser = _make_browser_mock(_make_page_mock())

        result = self._run(
            _scrape_one_competitor(
                competitor=comp,
                queries=["anything"],
                match_threshold=80.0,
                max_results=5,
                browser=browser,
            )
        )
        assert result.status == "SCRAPE_FAILED"

    def test_zero_price_candidate_returns_no_match(self):
        """Candidate with price=0 should be treated as no usable price."""
        candidates = [
            {"title": "Engel MD60F Fridge Freezer", "price": 0,
             "url": "https://site.com/product", "source": "css-card"},
        ]
        page = _make_page_mock(evaluate_return=candidates)
        browser = _make_browser_mock(page)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = self._run(
                _scrape_one_competitor(
                    competitor=_make_competitor(),
                    queries=["Engel MD60F Fridge"],
                    match_threshold=70.0,
                    max_results=5,
                    browser=browser,
                )
            )

        assert result.status == "NO_MATCH_FOUND"
        assert result.price is None

    def test_best_match_selected_from_multiple_candidates(self):
        """
        With multiple candidates, the one with the highest fuzzy score wins.
        Uses a clearly-winning candidate vs a clearly-losing one so the test
        is stable regardless of fuzzy library version.
        """
        candidates = [
            {"title": "Plastic Garden Chair Green Outdoor Furniture",
             "price": 299.0, "url": "https://site.com/chair", "source": "css-card"},
            {"title": "Engel MD60F 60 Litre Portable Fridge Freezer",
             "price": 12999.0, "url": "https://site.com/engel60", "source": "json-ld"},
        ]
        page = _make_page_mock(evaluate_return=candidates)
        browser = _make_browser_mock(page)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = self._run(
                _scrape_one_competitor(
                    competitor=_make_competitor(),
                    queries=["Engel MD60F 60L Fridge"],
                    match_threshold=70.0,
                    max_results=5,
                    browser=browser,
                )
            )

        assert result.status == "OK"
        # "Engel MD60F 60 Litre Portable Fridge Freezer" is the clear best match
        assert result.price == pytest.approx(12999.0)

    def test_fallback_query_matches_when_primary_misses(self):
        """
        Primary query returns empty; fallback query returns a good match.
        The scraper should succeed on the second query.
        """
        good_candidates = [
            {"title": "Engel MD60F Portable Fridge Freezer", "price": 11499.0,
             "url": "https://site.com/engel", "source": "json-ld"},
        ]
        # First call (primary query) → empty; second call (fallback) → match
        empty_page  = _make_page_mock(evaluate_return=[])
        good_page   = _make_page_mock(evaluate_return=good_candidates)

        browser = AsyncMock()
        browser.new_page.side_effect = [empty_page, good_page]
        browser.close.return_value = None

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = self._run(
                _scrape_one_competitor(
                    competitor=_make_competitor(),
                    queries=["Engel MD60F 60L", "MD60F"],  # primary + fallback
                    match_threshold=65.0,
                    max_results=5,
                    browser=browser,
                )
            )

        assert result.status == "OK"
        assert result.price == pytest.approx(11499.0)
        assert browser.new_page.call_count == 2


# ---------------------------------------------------------------------------
# scrape_product — integration over _scrape_one_competitor
# ---------------------------------------------------------------------------

class TestScrapeProduct:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _patched_scrape(self, results_map: dict[str, ScrapeResult]):
        """
        Patch _scrape_one_competitor to return from results_map keyed by
        competitor name.
        """
        async def _fake_scrape(*, competitor, queries, match_threshold, max_results, browser):
            name = competitor["name"]
            return results_map.get(
                name, ScrapeResult(competitor=name, status="SCRAPE_FAILED")
            )
        return patch(
            "scrapers.competitor_analysis.scraper._scrape_one_competitor",
            side_effect=_fake_scrape,
        )

    def test_all_competitors_return_results(self):
        comps = [_make_competitor("a", 1), _make_competitor("b", 2)]
        results_map = {
            "a": ScrapeResult(competitor="a", price=1000.0, status="OK"),
            "b": ScrapeResult(competitor="b", price=900.0,  status="OK"),
        }
        with self._patched_scrape(results_map):
            outcome = self._run(
                scrape_product(
                    sku="X1",
                    product_name="Test Fridge",
                    competitors=comps,
                    browser=AsyncMock(),
                )
            )

        assert len(outcome.results) == 2
        cheapest, source = outcome.cheapest()
        assert cheapest == pytest.approx(900.0)
        assert source == "b"

    def test_one_competitor_fails_others_succeed(self):
        """A failure on one competitor must not block results from others."""
        comps = [_make_competitor("a", 1), _make_competitor("b", 2)]
        results_map = {
            "a": ScrapeResult(competitor="a", status="SCRAPE_FAILED"),
            "b": ScrapeResult(competitor="b", price=1500.0, status="OK"),
        }
        with self._patched_scrape(results_map):
            outcome = self._run(
                scrape_product(
                    sku="X1", product_name="Test Fridge",
                    competitors=comps, browser=AsyncMock(),
                )
            )

        assert outcome.cheapest()[0] == pytest.approx(1500.0)

    def test_disabled_competitors_are_skipped(self):
        """enabled=False competitors must not be scraped."""
        comps = [
            _make_competitor("a", 1, enabled=True),
            _make_competitor("b", 2, enabled=False),
        ]
        results_map = {
            "a": ScrapeResult(competitor="a", price=2000.0, status="OK"),
        }
        with self._patched_scrape(results_map):
            outcome = self._run(
                scrape_product(
                    sku="X1", product_name="Test Fridge",
                    competitors=comps, browser=AsyncMock(),
                )
            )
        # Only competitor "a" result, "b" never called
        assert len(outcome.results) == 1
        assert outcome.results[0].competitor == "a"

    def test_all_fail_cheapest_is_none(self):
        comps = [_make_competitor("a", 1)]
        results_map = {"a": ScrapeResult(competitor="a", status="SCRAPE_FAILED")}
        with self._patched_scrape(results_map):
            outcome = self._run(
                scrape_product(
                    sku="Y1", product_name="Missing Product",
                    competitors=comps, browser=AsyncMock(),
                )
            )
        price, source = outcome.cheapest()
        assert price is None
        assert source is None


# ---------------------------------------------------------------------------
# ProductScrapeOutcome helpers
# ---------------------------------------------------------------------------

class TestProductScrapeOutcome:
    def _outcome(self, results: list[ScrapeResult]) -> ProductScrapeOutcome:
        o = ProductScrapeOutcome(sku="T1", product_name="Test")
        o.results = results
        return o

    def test_prices_by_competitor_dict(self):
        o = self._outcome([
            ScrapeResult(competitor="a", price=1200.0, status="OK"),
            ScrapeResult(competitor="b", price=None, status="NO_MATCH_FOUND"),
        ])
        d = o.prices_by_competitor()
        assert d["a"] == pytest.approx(1200.0)
        assert d["b"] is None

    def test_cheapest_excludes_none_prices(self):
        o = self._outcome([
            ScrapeResult(competitor="a", price=None, status="NO_MATCH_FOUND"),
            ScrapeResult(competitor="b", price=1800.0, status="OK"),
            ScrapeResult(competitor="c", price=2200.0, status="OK"),
        ])
        price, source = o.cheapest()
        assert price == pytest.approx(1800.0)
        assert source == "b"

    def test_cheapest_all_none_returns_none_none(self):
        o = self._outcome([
            ScrapeResult(competitor="a", status="SCRAPE_FAILED"),
        ])
        assert o.cheapest() == (None, None)


# ---------------------------------------------------------------------------
# load_competitors — config loader
# ---------------------------------------------------------------------------

class TestLoadCompetitors:
    def test_loads_yaml_and_sorts_by_priority(self, tmp_path):
        yaml_content = """
competitors:
  - name: beta
    search_url_pattern: "https://beta.com?q={query}"
    priority: 2
    enabled: true
  - name: alpha
    search_url_pattern: "https://alpha.com?q={query}"
    priority: 1
    enabled: true
  - name: gamma
    search_url_pattern: "https://gamma.com?q={query}"
    priority: 3
    enabled: false
"""
        cfg = tmp_path / "competitors.yaml"
        cfg.write_text(yaml_content)

        result = load_competitors(config_path=cfg)
        # gamma is disabled → not included
        assert [c["name"] for c in result] == ["alpha", "beta"]

    def test_disabled_competitors_excluded(self, tmp_path):
        yaml_content = """
competitors:
  - name: enabled_one
    search_url_pattern: "https://one.com?q={query}"
    priority: 1
    enabled: true
  - name: disabled_one
    search_url_pattern: "https://two.com?q={query}"
    priority: 2
    enabled: false
"""
        cfg = tmp_path / "competitors.yaml"
        cfg.write_text(yaml_content)

        result = load_competitors(config_path=cfg)
        assert len(result) == 1
        assert result[0]["name"] == "enabled_one"
