"""
Unit tests for scrapers/competitor_analysis/pricer.py

All tests are pure — no I/O, no network, no mocking required.
"""
import pytest

from scrapers.competitor_analysis.pricer import (
    STATUS_ALREADY_COMP,
    STATUS_MARGIN_FLOOR,
    STATUS_NO_MATCH,
    STATUS_PENDING,
    calculate_suggested_price,
    compute_discrepancy,
    compute_margin_pct,
)


# ---------------------------------------------------------------------------
# calculate_suggested_price
# ---------------------------------------------------------------------------

class TestNoMatch:
    def test_empty_prices_returns_no_match(self):
        price, status = calculate_suggested_price([], None, 1000.0)
        assert price is None
        assert status == STATUS_NO_MATCH

    def test_empty_prices_with_cost_returns_no_match(self):
        price, status = calculate_suggested_price([], 500.0, 1000.0)
        assert price is None
        assert status == STATUS_NO_MATCH


class TestPendingReview:
    def test_undercut_by_100_when_room_exists(self):
        """cheapest=R2000, cost=R1000, min_viable=R1150 → suggested=R1900 (R2000-100)"""
        price, status = calculate_suggested_price(
            competitor_prices=[2000.0],
            cost_price=1000.0,
            cfsa_current_price=2200.0,
            min_margin_pct=15.0,
        )
        assert status == STATUS_PENDING
        assert price == pytest.approx(1900.0)

    def test_match_competitor_when_undercut_breaches_floor(self):
        """cheapest=R1200, cost=R1000, min_viable=R1150 → R1200-100=R1100 < floor → match at R1200"""
        price, status = calculate_suggested_price(
            competitor_prices=[1200.0],
            cost_price=1000.0,
            cfsa_current_price=1500.0,
            min_margin_pct=15.0,
        )
        assert status == STATUS_PENDING
        assert price == pytest.approx(1200.0)

    def test_uses_cheapest_of_multiple_competitors(self):
        """Should use min([1800, 2200, 1600]) = 1600 as the anchor."""
        price, status = calculate_suggested_price(
            competitor_prices=[1800.0, 2200.0, 1600.0],
            cost_price=1000.0,
            cfsa_current_price=2000.0,
            min_margin_pct=15.0,
        )
        assert status == STATUS_PENDING
        assert price == pytest.approx(1500.0)  # 1600 - 100

    def test_no_cost_price_still_undercuts(self):
        """Without cost data, min_viable=0, so we always undercut by R100."""
        price, status = calculate_suggested_price(
            competitor_prices=[1500.0],
            cost_price=None,
            cfsa_current_price=1800.0,
            min_margin_pct=15.0,
        )
        assert status == STATUS_PENDING
        assert price == pytest.approx(1400.0)


class TestMarginFloor:
    def test_competitor_below_floor_raises_floor(self):
        """cheapest=R1050, cost=R1000, min_viable=R1150 → floor hit, suggested=R1150"""
        price, status = calculate_suggested_price(
            competitor_prices=[1050.0],
            cost_price=1000.0,
            cfsa_current_price=1500.0,
            min_margin_pct=15.0,
        )
        assert status == STATUS_MARGIN_FLOOR
        assert price == pytest.approx(1150.0)

    def test_competitor_exactly_at_floor_does_not_trigger(self):
        """cheapest=R1150 == min_viable=R1150 → NOT a floor hit (match competitor)"""
        price, status = calculate_suggested_price(
            competitor_prices=[1150.0],
            cost_price=1000.0,
            cfsa_current_price=1500.0,
            min_margin_pct=15.0,
        )
        # 1150 - 100 = 1050 < 1150 (floor), so we match at 1150
        assert status == STATUS_PENDING
        assert price == pytest.approx(1150.0)

    def test_zero_cost_never_triggers_floor(self):
        """Zero cost → min_viable=0, any competitor price is valid."""
        price, status = calculate_suggested_price(
            competitor_prices=[100.0],
            cost_price=0.0,
            cfsa_current_price=200.0,
            min_margin_pct=15.0,
        )
        assert status != STATUS_MARGIN_FLOOR

    def test_none_cost_never_triggers_floor(self):
        price, status = calculate_suggested_price(
            competitor_prices=[100.0],
            cost_price=None,
            cfsa_current_price=200.0,
            min_margin_pct=15.0,
        )
        assert status != STATUS_MARGIN_FLOOR


class TestAlreadyCompetitive:
    def test_cfsa_price_below_suggested_marks_competitive(self):
        """CFSA=R1700, cheapest=R2000 → suggested=R1900. R1700 <= R1900 → ALREADY_COMPETITIVE."""
        price, status = calculate_suggested_price(
            competitor_prices=[2000.0],
            cost_price=1000.0,
            cfsa_current_price=1700.0,
            min_margin_pct=15.0,
        )
        assert status == STATUS_ALREADY_COMP
        assert price == pytest.approx(1900.0)

    def test_cfsa_price_equal_to_suggested_marks_competitive(self):
        """CFSA=R1900 == suggested=R1900 → ALREADY_COMPETITIVE."""
        price, status = calculate_suggested_price(
            competitor_prices=[2000.0],
            cost_price=1000.0,
            cfsa_current_price=1900.0,
            min_margin_pct=15.0,
        )
        assert status == STATUS_ALREADY_COMP

    def test_cfsa_price_one_rand_above_suggested_is_pending(self):
        """CFSA=R1901, suggested=R1900 → NOT competitive (just 1 rand above)."""
        price, status = calculate_suggested_price(
            competitor_prices=[2000.0],
            cost_price=1000.0,
            cfsa_current_price=1901.0,
            min_margin_pct=15.0,
        )
        assert status == STATUS_PENDING

    def test_already_competitive_overrides_margin_floor_when_cfsa_is_cheap(self):
        """
        Even in a margin floor scenario, if CFSA current price <= floor,
        the status should be ALREADY_COMPETITIVE (we don't need to act).
        """
        price, status = calculate_suggested_price(
            competitor_prices=[1050.0],
            cost_price=1000.0,
            cfsa_current_price=1100.0,  # below the floor of 1150
            min_margin_pct=15.0,
        )
        # Suggested = 1150.0 (floor), cfsa=1100 <= 1150 → ALREADY_COMPETITIVE
        assert status == STATUS_ALREADY_COMP
        assert price == pytest.approx(1150.0)


class TestRounding:
    def test_suggested_price_rounded_to_2dp(self):
        price, _ = calculate_suggested_price(
            competitor_prices=[1999.99],
            cost_price=1000.0,
            cfsa_current_price=2500.0,
            min_margin_pct=15.0,
        )
        assert price == pytest.approx(1899.99)

    def test_margin_floor_rounded_to_2dp(self):
        price, _ = calculate_suggested_price(
            competitor_prices=[1050.0],
            cost_price=1000.0,
            cfsa_current_price=2000.0,
            min_margin_pct=15.0,
        )
        assert price == pytest.approx(1150.0)


# ---------------------------------------------------------------------------
# compute_discrepancy
# ---------------------------------------------------------------------------

class TestComputeDiscrepancy:
    def test_cfsa_more_expensive_positive(self):
        assert compute_discrepancy(1500.0, 1200.0) == pytest.approx(300.0)

    def test_cfsa_cheaper_negative(self):
        assert compute_discrepancy(1000.0, 1200.0) == pytest.approx(-200.0)

    def test_equal_is_zero(self):
        assert compute_discrepancy(1000.0, 1000.0) == pytest.approx(0.0)

    def test_none_competitor_returns_none(self):
        assert compute_discrepancy(1500.0, None) is None


# ---------------------------------------------------------------------------
# compute_margin_pct
# ---------------------------------------------------------------------------

class TestComputeMarginPct:
    def test_standard_margin(self):
        # (1150 - 1000) / 1000 * 100 = 15%
        assert compute_margin_pct(1150.0, 1000.0) == pytest.approx(15.0)

    def test_zero_cost_returns_none(self):
        assert compute_margin_pct(1000.0, 0.0) is None

    def test_none_cost_returns_none(self):
        assert compute_margin_pct(1000.0, None) is None

    def test_negative_margin_possible(self):
        assert compute_margin_pct(800.0, 1000.0) == pytest.approx(-20.0)
