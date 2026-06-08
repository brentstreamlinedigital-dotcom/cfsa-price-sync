"""
Load and validate supplier YAML configs using Pydantic.
Each file in config/suppliers/*.yaml represents one supplier.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class EmailSource(BaseModel):
    type: str = "email"
    email_from_domains: list[str] = Field(default_factory=list)
    email_subject_contains: list[str] = Field(default_factory=list)
    attachment_types: list[str] = Field(default_factory=lambda: ["xlsx", "csv"])


class ScrapeAuth(BaseModel):
    login_url: str
    username_field: str
    password_field: str
    username_secret: str  # Secret Manager key name
    password_secret: str  # Secret Manager key name


class ScrapeFallback(BaseModel):
    enabled: bool = False
    days_threshold: int = 14  # scrape only if no email in N days; 0 = always
    url: str = ""
    strategy: str = "table"  # table | pagination | api
    brand_filter: str = ""   # if set, only keep products whose title contains this string
    auth: Optional[ScrapeAuth] = None


class PriceFormula(BaseModel):
    key: str = ""
    expression: str = ""  # e.g. "rrp * 0.85"


class SkuNormalization(BaseModel):
    strip_prefix: str = ""
    uppercase: bool = True
    remove_spaces: bool = False


class ShopifyConfig(BaseModel):
    sync_price: bool = True
    sync_inventory: bool = True
    location_id: str = ""
    inventory_policy: str = "deny"  # deny | continue


class DescriptionFilter(BaseModel):
    """
    Keyword-based filter on the normalised description field.
    Applied after column mapping, before price calculation.

    include: keep row only if description contains AT LEAST ONE of these (case-insensitive)
             — leave empty to skip this check.
    exclude: drop row if description contains ANY of these (case-insensitive)
             — leave empty to skip this check.
    """
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Root supplier config model
# ---------------------------------------------------------------------------

class SupplierConfig(BaseModel):
    supplier_key: str
    display_name: str
    active: bool = True
    sku_prefix_filter: Optional[list[str]] = None  # if set, only keep rows whose SKU starts with one of these
    description_filter: Optional[DescriptionFilter] = None  # keyword filter on description

    source: EmailSource = Field(default_factory=EmailSource)
    scrape_fallback: ScrapeFallback = Field(default_factory=ScrapeFallback)

    # column_map: master_field -> supplier column name (str) or index (int)
    column_map: dict[str, Union[str, int]] = Field(default_factory=dict)

    sheet_name: Optional[Union[str, int]] = None  # None = first sheet
    skip_rows: int = 0

    price_formula: PriceFormula = Field(default_factory=PriceFormula)
    stock_status_map: dict[str, str] = Field(default_factory=dict)
    sku_normalization: SkuNormalization = Field(default_factory=SkuNormalization)
    shopify: ShopifyConfig = Field(default_factory=ShopifyConfig)

    # Optional cost-estimation fallback: when the supplier feed exposes only
    # RRP (no wholesale cost), derive cost_inc = rrp × ratio. Stored as a
    # plain dict to keep this loose — different methods may be added later
    # (e.g. flat markup, per-product map).
    cost_estimation: Optional[dict[str, Any]] = None

    @field_validator("supplier_key")
    @classmethod
    def key_must_be_slug(cls, v: str) -> str:
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"supplier_key must be alphanumeric/underscore: {v!r}")
        return v.lower()

    @model_validator(mode="after")
    def column_map_must_have_sku(self) -> "SupplierConfig":
        if "sku" not in self.column_map:
            raise ValueError(
                f"[{self.supplier_key}] column_map must include 'sku'"
            )
        return self


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_supplier_config(path: Union[str, Path]) -> SupplierConfig:
    """Load and validate a single supplier YAML file."""
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f)
    try:
        return SupplierConfig(**raw)
    except Exception as e:
        raise ValueError(f"Invalid supplier config {path.name}: {e}") from e


def load_all_supplier_configs(
    config_dir: Union[str, Path] = None,
    active_only: bool = True,
) -> dict[str, SupplierConfig]:
    """
    Load all *.yaml files from config/suppliers/.
    Returns {supplier_key: SupplierConfig}.
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config" / "suppliers"
    config_dir = Path(config_dir)

    configs: dict[str, SupplierConfig] = {}
    errors: list[str] = []

    for yaml_file in sorted(config_dir.glob("*.yaml")):
        try:
            cfg = load_supplier_config(yaml_file)
            if active_only and not cfg.active:
                continue
            configs[cfg.supplier_key] = cfg
        except Exception as e:
            errors.append(str(e))

    if errors:
        raise ValueError("Supplier config errors:\n" + "\n".join(errors))

    return configs


def load_app_config(path: Union[str, Path] = None) -> dict[str, Any]:
    """Load global app.yaml config."""
    if path is None:
        path = Path(__file__).parent.parent / "config" / "app.yaml"
    with open(path) as f:
        return yaml.safe_load(f) or {}
