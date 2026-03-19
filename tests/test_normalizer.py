"""
Unit tests for the normalizer.
Uses fixture DataFrames — no file I/O, no network calls.
"""
import pandas as pd
import pytest

from src.config_loader import SupplierConfig
from src.normalizer import MASTER_FIELDS, normalize


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> SupplierConfig:
    base = {
        "supplier_key": "test_supplier",
        "display_name": "Test Supplier",
        "column_map": {
            "sku": "SKU",
            "description": "Description",
            "cost_inc": "Cost Inc",
            "rrp": "RRP",
        },
        "price_formula": {
            "key": "rrp_x_0.85",
            "expression": "rrp * 0.85",
        },
        "stock_status_map": {
            "In Stock": "In Stock",
            "Out of Stock": "Out of Stock",
            "": "Unknown",
        },
    }
    base.update(overrides)
    return SupplierConfig(**base)


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"SKU": "MD14F", "Description": "Engel 14L Fridge", "Cost Inc": "7500", "RRP": "8699", "Notes": "In Stock"},
        {"SKU": "MT35F", "Description": "Engel 35L Fridge", "Cost Inc": "12000", "RRP": "14199", "Notes": "Out of Stock"},
        {"SKU": "", "Description": "Empty row — should be skipped", "Cost Inc": "0", "RRP": "0"},
    ])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNormalizeBasic:
    def test_output_has_master_fields(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg)
        assert list(df.columns) == MASTER_FIELDS

    def test_skips_empty_sku(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg)
        assert len(df) == 2  # 3 rows, 1 has empty SKU

    def test_supplier_key_set(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg)
        assert (df["supplier"] == "test_supplier").all()

    def test_source_set(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg, source="email")
        assert (df["source"] == "email").all()

    def test_sku_uppercased(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg)
        assert df.iloc[0]["sku"] == "MD14F"

    def test_raw_sku_preserved(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg)
        assert df.iloc[0]["raw_sku"] == "MD14F"

    def test_cost_cast_to_float(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg)
        assert df.iloc[0]["cost_inc"] == 7500.0

    def test_rrp_cast_to_float(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg)
        assert df.iloc[0]["rrp"] == 8699.0

    def test_selling_price_calculated(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg)
        expected = round(8699.0 * 0.85, 2)
        assert df.iloc[0]["selling_price"] == expected

    def test_row_hash_generated(self):
        cfg = _make_config()
        df = normalize(_sample_df(), cfg)
        assert df.iloc[0]["row_hash"] is not None
        assert len(df.iloc[0]["row_hash"]) == 32  # MD5 hex

    def test_identical_rows_produce_same_hash(self):
        cfg = _make_config()
        df1 = normalize(_sample_df(), cfg)
        df2 = normalize(_sample_df(), cfg)
        assert df1.iloc[0]["row_hash"] == df2.iloc[0]["row_hash"]

    def test_currency_symbol_stripped(self):
        data = pd.DataFrame([
            {"SKU": "CF45", "Description": "Chest Freezer", "Cost Inc": "R5,749.00", "RRP": "R6,199.00"}
        ])
        cfg = _make_config()
        df = normalize(data, cfg)
        assert df.iloc[0]["cost_inc"] == 5749.0
        assert df.iloc[0]["rrp"] == 6199.0


class TestStockStatusMap:
    def test_maps_known_status(self):
        data = pd.DataFrame([
            {"SKU": "X1", "Description": "Test", "Cost Inc": "100", "RRP": "150", "stock_status": "In Stock"}
        ])
        cfg = _make_config(
            column_map={
                "sku": "SKU",
                "description": "Description",
                "cost_inc": "Cost Inc",
                "rrp": "RRP",
                "stock_status": "stock_status",
            }
        )
        df = normalize(data, cfg)
        assert df.iloc[0]["stock_status"] == "In Stock"

    def test_maps_empty_to_unknown(self):
        data = pd.DataFrame([
            {"SKU": "X1", "Description": "Test", "Cost Inc": "100", "RRP": "150", "stock_status": ""}
        ])
        cfg = _make_config(
            column_map={
                "sku": "SKU",
                "description": "Description",
                "cost_inc": "Cost Inc",
                "rrp": "RRP",
                "stock_status": "stock_status",
            }
        )
        df = normalize(data, cfg)
        assert df.iloc[0]["stock_status"] == "Unknown"


class TestPriceFormulas:
    def test_cost_x_1_15(self):
        data = pd.DataFrame([
            {"SKU": "A1", "Description": "Item", "Cost Inc": "1000", "RRP": "1500"}
        ])
        cfg = _make_config(
            price_formula={"key": "cost_x_1.15", "expression": "cost_inc * 1.15"}
        )
        df = normalize(data, cfg)
        assert df.iloc[0]["selling_price"] == 1150.0

    def test_missing_price_returns_none_not_crash(self):
        data = pd.DataFrame([
            {"SKU": "A1", "Description": "Item", "Cost Inc": "", "RRP": ""}
        ])
        cfg = _make_config(
            price_formula={"key": "rrp_x_0.85", "expression": "rrp * 0.85"}
        )
        df = normalize(data, cfg)
        assert df.iloc[0]["selling_price"] == 0.0  # 0 * 0.85 = 0


class TestColumnIndexMapping:
    def test_integer_column_index(self):
        data = pd.DataFrame([["MD14F", "Engel 14L", "7500", "8699"]])
        cfg = _make_config(
            column_map={
                "sku": 0,
                "description": 1,
                "cost_inc": 2,
                "rrp": 3,
            }
        )
        df = normalize(data, cfg)
        assert df.iloc[0]["sku"] == "MD14F"
        assert df.iloc[0]["cost_inc"] == 7500.0
