"""
Unit tests for the config loader.
Tests YAML validation without touching the filesystem config files.
"""
import pytest

from src.config_loader import SupplierConfig


class TestSupplierConfig:
    def test_minimal_valid_config(self):
        cfg = SupplierConfig(
            supplier_key="engel",
            display_name="Engel",
            column_map={"sku": "SKU"},
        )
        assert cfg.supplier_key == "engel"

    def test_supplier_key_lowercased(self):
        cfg = SupplierConfig(
            supplier_key="ENGEL",
            display_name="Engel",
            column_map={"sku": "SKU"},
        )
        assert cfg.supplier_key == "engel"

    def test_missing_sku_in_column_map_raises(self):
        with pytest.raises(ValueError, match="column_map must include 'sku'"):
            SupplierConfig(
                supplier_key="engel",
                display_name="Engel",
                column_map={"description": "Description"},
            )

    def test_invalid_supplier_key_raises(self):
        with pytest.raises(ValueError):
            SupplierConfig(
                supplier_key="engel!bad",
                display_name="Engel",
                column_map={"sku": "SKU"},
            )

    def test_integer_column_index_allowed(self):
        cfg = SupplierConfig(
            supplier_key="dometic_thrsa",
            display_name="Dometic THRSA",
            column_map={"sku": 0, "description": 1, "cost_inc": 2},
        )
        assert cfg.column_map["sku"] == 0

    def test_price_formula_defaults(self):
        cfg = SupplierConfig(
            supplier_key="test",
            display_name="Test",
            column_map={"sku": "SKU"},
        )
        assert cfg.price_formula.expression == ""

    def test_active_defaults_true(self):
        cfg = SupplierConfig(
            supplier_key="test",
            display_name="Test",
            column_map={"sku": "SKU"},
        )
        assert cfg.active is True
