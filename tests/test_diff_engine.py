"""
Unit tests for the diff engine.
"""
import pandas as pd
import pytest

from src.diff_engine import compute_diff
from src.normalizer import MASTER_FIELDS


def _make_row(**kwargs) -> dict:
    base = {f: None for f in MASTER_FIELDS}
    base.update(kwargs)
    return base


def _df(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=MASTER_FIELDS)


class TestComputeDiff:
    def test_new_row_detected(self):
        incoming = _df(_make_row(sku="NEW1", supplier="engel", row_hash="abc123", selling_price=100.0))
        master = pd.DataFrame(columns=MASTER_FIELDS)
        result = compute_diff(incoming, master, supplier="engel")
        assert len(result.new_rows) == 1
        assert result.new_rows.iloc[0]["sku"] == "NEW1"

    def test_unchanged_row_skipped(self):
        row = _make_row(sku="X1", supplier="engel", row_hash="same_hash", selling_price=100.0)
        incoming = _df(row)
        master = _df(row)
        result = compute_diff(incoming, master, supplier="engel")
        assert result.unchanged_count == 1
        assert result.new_rows.empty
        assert result.changed_rows.empty

    def test_unchanged_row_captured_in_unchanged_rows(self):
        """unchanged_rows list should contain the row for price_sync_log logging."""
        row = _make_row(sku="X1", supplier="engel", row_hash="same_hash", selling_price=100.0)
        incoming = _df(row)
        master = _df(row)
        result = compute_diff(incoming, master, supplier="engel")
        assert len(result.unchanged_rows) == 1
        assert result.unchanged_rows[0]["sku"] == "X1"

    def test_changed_row_detected(self):
        master_row = _make_row(sku="X1", supplier="engel", row_hash="old_hash", selling_price=100.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new_hash", selling_price=105.0)
        master = _df(master_row)
        incoming = _df(incoming_row)
        result = compute_diff(incoming, master, supplier="engel")
        assert len(result.changed_rows) == 1

    def test_large_price_change_triggers_alert(self):
        master_row = _make_row(sku="X1", supplier="engel", row_hash="old", selling_price=1000.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new", selling_price=1200.0)
        master = _df(master_row)
        incoming = _df(incoming_row)
        result = compute_diff(incoming, master, supplier="engel", price_alert_threshold_pct=15.0)
        assert len(result.alerts) == 1
        assert result.alerts[0]["sku"] == "X1"
        assert result.alerts[0]["price_delta_pct"] == pytest.approx(20.0, rel=0.01)

    def test_small_price_change_no_alert(self):
        master_row = _make_row(sku="X1", supplier="engel", row_hash="old", selling_price=1000.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new", selling_price=1050.0)
        master = _df(master_row)
        incoming = _df(incoming_row)
        result = compute_diff(incoming, master, supplier="engel", price_alert_threshold_pct=15.0)
        assert len(result.alerts) == 0
        assert len(result.changed_rows) == 1

    def test_only_diffs_own_supplier_rows(self):
        # Master has rows for two suppliers; diff should only compare against own
        arb_row = _make_row(sku="ARB1", supplier="arb", row_hash="arb_hash", selling_price=200.0)
        master = _df(arb_row)
        incoming = _df(_make_row(sku="ARB1", supplier="engel", row_hash="engel_hash", selling_price=150.0))
        result = compute_diff(incoming, master, supplier="engel")
        # ARB1 exists in master under 'arb', not 'engel' — so it's treated as NEW
        assert len(result.new_rows) == 1

    def test_empty_incoming_returns_empty_result(self):
        master = _df(_make_row(sku="X1", supplier="engel", row_hash="h1"))
        incoming = pd.DataFrame(columns=MASTER_FIELDS)
        result = compute_diff(incoming, master, supplier="engel")
        assert not result.has_changes


class TestIncreaseThresholdFilter:
    """2% upside filter — increases below threshold are skipped; decreases always applied."""

    def test_tiny_increase_below_threshold_skipped(self):
        """1% increase < 2% threshold → goes to skipped_rows, not changed_rows."""
        master_row  = _make_row(sku="X1", supplier="engel", row_hash="old", selling_price=1000.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new", selling_price=1010.0)  # +1%
        result = compute_diff(_df(incoming_row), _df(master_row), supplier="engel",
                               increase_threshold_pct=2.0)
        assert len(result.skipped_rows) == 1
        assert result.changed_rows.empty
        assert result.skipped_rows[0]["sku"] == "X1"
        assert result.skipped_rows[0]["skip_reason"] == "Below 2% increase threshold"

    def test_increase_exactly_at_threshold_is_applied(self):
        """2% increase == 2% threshold → applied (boundary is exclusive below)."""
        master_row  = _make_row(sku="X1", supplier="engel", row_hash="old", selling_price=1000.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new", selling_price=1020.0)  # +2%
        result = compute_diff(_df(incoming_row), _df(master_row), supplier="engel",
                               increase_threshold_pct=2.0)
        assert result.skipped_rows == []
        assert len(result.changed_rows) == 1

    def test_increase_above_threshold_is_applied(self):
        """5% increase > 2% threshold → applied to changed_rows."""
        master_row  = _make_row(sku="X1", supplier="engel", row_hash="old", selling_price=1000.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new", selling_price=1050.0)  # +5%
        result = compute_diff(_df(incoming_row), _df(master_row), supplier="engel",
                               increase_threshold_pct=2.0)
        assert result.skipped_rows == []
        assert len(result.changed_rows) == 1

    def test_price_decrease_always_applied(self):
        """Any price decrease bypasses the increase threshold filter entirely."""
        master_row  = _make_row(sku="X1", supplier="engel", row_hash="old", selling_price=1000.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new", selling_price=995.0)  # -0.5%
        result = compute_diff(_df(incoming_row), _df(master_row), supplier="engel",
                               increase_threshold_pct=2.0)
        assert result.skipped_rows == []
        assert len(result.changed_rows) == 1

    def test_threshold_zero_applies_all_increases(self):
        """Setting increase_threshold_pct=0 disables the filter."""
        master_row  = _make_row(sku="X1", supplier="engel", row_hash="old", selling_price=1000.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new", selling_price=1001.0)  # +0.1%
        result = compute_diff(_df(incoming_row), _df(master_row), supplier="engel",
                               increase_threshold_pct=0.0)
        assert result.skipped_rows == []
        assert len(result.changed_rows) == 1

    def test_skipped_row_has_correct_pct_change(self):
        """skipped_rows entry should record the actual pct_change."""
        master_row  = _make_row(sku="X1", supplier="engel", row_hash="old", selling_price=1000.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new", selling_price=1015.0)  # +1.5%
        result = compute_diff(_df(incoming_row), _df(master_row), supplier="engel",
                               increase_threshold_pct=2.0)
        assert len(result.skipped_rows) == 1
        assert result.skipped_rows[0]["pct_change"] == pytest.approx(1.5, rel=0.01)

    def test_skipped_row_not_counted_in_changed(self):
        """A skipped row must not appear in has_changes."""
        master_row  = _make_row(sku="X1", supplier="engel", row_hash="old", selling_price=1000.0)
        incoming_row = _make_row(sku="X1", supplier="engel", row_hash="new", selling_price=1005.0)  # +0.5%
        result = compute_diff(_df(incoming_row), _df(master_row), supplier="engel",
                               increase_threshold_pct=2.0)
        assert not result.has_changes
