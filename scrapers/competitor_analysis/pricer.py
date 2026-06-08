"""
Competitor pricing logic — pure functions, no I/O.

Status values
─────────────
NO_MATCH_FOUND      No competitor found a matching product.
ALREADY_COMPETITIVE CFSA current price is already <= suggested price.
PENDING_REVIEW      Suggestion computed; requires human approval before Shopify write.
MARGIN_FLOOR_HIT    Cheapest competitor is below our minimum margin floor.
                    Suggested price is the margin floor, not a competitor undercut.
SCRAPE_FAILED       All competitor scrapes failed — no data to work with.
"""
from __future__ import annotations

from typing import Optional

# Status constants exposed so callers can do `from .pricer import STATUS_*`
STATUS_NO_MATCH          = "NO_MATCH_FOUND"
STATUS_ALREADY_COMP      = "ALREADY_COMPETITIVE"
STATUS_PENDING           = "PENDING_REVIEW"
STATUS_MARGIN_FLOOR      = "MARGIN_FLOOR_HIT"
STATUS_SCRAPE_FAILED     = "SCRAPE_FAILED"
STATUS_APPROVED          = "APPROVED"
STATUS_OVERRIDDEN        = "OVERRIDDEN"
STATUS_REJECTED          = "REJECTED"

ALL_STATUSES = [
    STATUS_NO_MATCH, STATUS_ALREADY_COMP, STATUS_PENDING,
    STATUS_MARGIN_FLOOR, STATUS_SCRAPE_FAILED,
    STATUS_APPROVED, STATUS_OVERRIDDEN, STATUS_REJECTED,
]


def calculate_suggested_price(
    competitor_prices: list[float],
    cost_price: Optional[float],
    cfsa_current_price: float,
    min_margin_pct: float = 15.0,
    sanity_min_ratio: float = 0.15,
    sanity_max_ratio: float = 5.0,
    anchor_price: Optional[float] = None,
) -> tuple[Optional[float], str]:
    """
    Derive a suggested CFSA selling price from competitor data.

    Rules (in order):
    0. Sanity filter: discard any competitor price < sanity_min_ratio × cfsa_current_price
         (catches obviously wrong fuzzy matches — e.g. R42 when CFSA sells for R6,999)
    1. No prices remain after filtering → (None, NO_MATCH_FOUND)
    2. Pick the ANCHOR price:
         - If `anchor_price` is provided (e.g. Takealot's price) AND it survived
           sanity filter, use it. Takealot is the dominant ZA retailer — beating
           their price is the highest-value competitive signal.
         - Otherwise use the cheapest remaining competitor.
    3. Apply margin floor + R100 undercut against the anchor (see existing rules).
    4. If cfsa_current_price <= suggested → ALREADY_COMPETITIVE.

    The returned suggested_price is always rounded to 2 decimal places.
    This function NEVER writes to any external system.

    Args:
        competitor_prices:  List of valid (non-None) competitor prices in ZAR.
        cost_price:         CFSA cost price (cost_inc). May be None if unknown.
        cfsa_current_price: Current CFSA selling price on Shopify.
        min_margin_pct:     Minimum acceptable gross-margin percentage (default 15%).
        sanity_min_ratio:   Competitor prices below this fraction of cfsa_current_price
                            are discarded as likely wrong matches (default 15%).
        anchor_price:       Optional preferred anchor (typically Takealot). Used as
                            the benchmark instead of cheapest when supplied and
                            within sanity bounds.

    Returns:
        (suggested_price, status) tuple.
    """
    if not competitor_prices:
        return None, STATUS_NO_MATCH

    # Drop non-numeric / non-positive / NaN entries up front
    import math
    competitor_prices = [
        float(p) for p in competitor_prices
        if isinstance(p, (int, float))
        and not math.isnan(float(p))
        and not math.isinf(float(p))
        and float(p) > 0
    ]
    if not competitor_prices:
        return None, STATUS_NO_MATCH

    # Rule 0: sanity-filter both wildly low AND wildly high prices.
    #   Low  (< 15 % of CFSA): fuzzy match landed on an accessory or wrong product
    #   High (> 500% of CFSA): match landed on something completely different
    #                          (a Dometic air conditioner instead of fridge, etc.)
    if cfsa_current_price and cfsa_current_price > 0:
        low  = cfsa_current_price * sanity_min_ratio
        high = cfsa_current_price * sanity_max_ratio
        competitor_prices = [p for p in competitor_prices if low <= p <= high]
        anchor_valid = (
            anchor_price is not None
            and low <= anchor_price <= high
        )
    else:
        anchor_valid = anchor_price is not None and anchor_price > 0

    if not competitor_prices:
        return None, STATUS_NO_MATCH

    cheapest = min(competitor_prices)

    # ── Choose the anchor ────────────────────────────────────────────
    # Takealot price (anchor_price) takes priority when present and valid.
    # This lets us position relative to the dominant ZA retailer regardless
    # of whether some niche competitor happens to be the absolute cheapest.
    if anchor_valid:
        benchmark = float(anchor_price)
    else:
        benchmark = cheapest

    # Minimum viable price based on cost + margin floor
    if cost_price and cost_price > 0:
        min_viable = cost_price * (1 + min_margin_pct / 100)
    else:
        # No cost data — can't enforce margin floor; use R0 so we never block
        min_viable = 0.0

    if benchmark < min_viable:
        # Anchor is cheaper than we can go — floor at our margin minimum
        suggested = round(min_viable, 2)
        status = STATUS_MARGIN_FLOOR
    elif benchmark - 100.0 >= min_viable:
        # Room to undercut by R100 while staying above margin floor
        suggested = round(benchmark - 100.0, 2)
        status = STATUS_PENDING
    else:
        # Match the anchor (undercutting further would breach margin floor)
        suggested = round(benchmark, 2)
        status = STATUS_PENDING

    # If we're already at or below the suggested price, no action needed
    if cfsa_current_price <= suggested:
        status = STATUS_ALREADY_COMP

    # ── Output guardrails ────────────────────────────────────────────
    # These should never trip if the rules above are sound, but if they
    # ever do we want a loud log line (not a silent bad write).
    import logging as _log
    _logger = _log.getLogger(__name__)
    if cost_price and cost_price > 0 and suggested < cost_price:
        _logger.error(
            "PRICER GUARDRAIL: suggested R%.2f is BELOW cost R%.2f — "
            "forcing margin floor. (status was %s)",
            suggested, cost_price, status,
        )
        suggested = round(cost_price * (1 + min_margin_pct / 100), 2)
        status = STATUS_MARGIN_FLOOR
    if suggested > cfsa_current_price * 1.5:
        # Suggesting a >50 % price increase based on competitor data is almost
        # certainly garbage (likely a fuzzy match to a much more expensive
        # product class). Log and fall back to NO_MATCH.
        _logger.error(
            "PRICER GUARDRAIL: suggested R%.2f is >150%% of CFSA R%.2f — "
            "treating as NO_MATCH instead of writing an inflated suggestion.",
            suggested, cfsa_current_price,
        )
        return None, STATUS_NO_MATCH

    return suggested, status


def compute_discrepancy(cfsa_price: float, cheapest_competitor: Optional[float]) -> Optional[float]:
    """
    Return cfsa_price - cheapest_competitor in ZAR.
    Positive = CFSA is more expensive than the cheapest competitor.
    Returns None if cheapest_competitor is None.
    """
    if cheapest_competitor is None:
        return None
    return round(cfsa_price - cheapest_competitor, 2)


def compute_margin_pct(selling_price: float, cost_price: Optional[float]) -> Optional[float]:
    """
    Gross margin as a percentage.
    Returns None if cost_price is unknown or zero.
    """
    if not cost_price or cost_price <= 0:
        return None
    return round((selling_price - cost_price) / cost_price * 100, 1)
