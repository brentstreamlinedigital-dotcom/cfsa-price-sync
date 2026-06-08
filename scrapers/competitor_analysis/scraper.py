"""
Competitor price scraper — async Playwright.

Design principles
─────────────────
• One async function per concern; fully injectable for testing.
• Any single competitor failure is isolated — never crashes the full run.
• Random inter-request delays and UA rotation reduce bot-detection rate.
• Fuzzy matching (rapidfuzz) gates which search results are accepted.
• All I/O is async; competitors per product run concurrently via asyncio.gather.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus

log = logging.getLogger(__name__)

# ── User-agent pool ─────────────────────────────────────────────────────────
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

# ZAR price regex — matches "R 1,299.00", "R1299", "R 299.99", "R1,299"
# Uses a permissive digit+comma group so "R2500" (no commas) and "R1,299" both parse.
# Commas are stripped in _parse_price().
_PRICE_RE = re.compile(r"R\s*([\d,]+(?:\.\d{2})?)")


@dataclass
class ScrapeResult:
    competitor: str
    product_name: Optional[str] = None
    price: Optional[float] = None
    url: Optional[str] = None
    match_score: float = 0.0
    status: str = "OK"          # OK | NO_MATCH_FOUND | SCRAPE_FAILED


@dataclass
class ProductScrapeOutcome:
    """All competitor results for one product."""
    sku: str
    product_name: str
    results: list[ScrapeResult] = field(default_factory=list)

    def prices_by_competitor(self) -> dict[str, Optional[float]]:
        """Return {competitor_name: price_or_None} for every result."""
        return {r.competitor: r.price for r in self.results}

    def cheapest(self) -> tuple[Optional[float], Optional[str]]:
        """Return (cheapest_price, competitor_name). Both None if no prices found."""
        prices = [(r.price, r.competitor) for r in self.results if r.price is not None]
        if not prices:
            return None, None
        return min(prices, key=lambda x: x[0])


# ── JS extraction snippet injected into each search results page ─────────────
_EXTRACT_JS = """
() => {
    const priceRe = /R\\s*(\\d{1,3}(?:,\\d{3})*(?:\\.\\d{2})?|\\d+(?:\\.\\d{2})?)/i;
    const results = [];

    // ── No-results guard ──────────────────────────────────────────────────────
    const bodyText = (document.body && document.body.innerText)
        ? document.body.innerText.toLowerCase() : '';
    const noResultPhrases = [
        'no results found', 'no products found', 'nothing found',
        'your search returned no results', 'sorry, but nothing matched',
        'no items found', '0 results', 'did not match any products',
        "we couldn't find", 'no search results',
    ];
    if (noResultPhrases.some(p => bodyText.includes(p))) return [];
    // ─────────────────────────────────────────────────────────────────────────

    // Helper: extract current (non-strikethrough) price from an element.
    // WooCommerce wraps sale price in <ins> and original in <del>.
    // Takealot / Shopify often use data-price or a dedicated price span.
    function extractPrice(el) {
        if (!el) return 0;
        // Prefer data-price attribute (Shopify, Takealot)
        if (el.dataset && el.dataset.price) {
            const v = parseFloat(el.dataset.price) / 100;  // Shopify stores cents
            if (v > 0) return v;
        }
        // Prefer <ins> child (WooCommerce sale price — ignore crossed-out <del>)
        const ins = el.querySelector('ins');
        const src = ins || el;
        // Remove <del> nodes before reading textContent to avoid picking up old price
        const clone = src.cloneNode(true);
        clone.querySelectorAll('del,s').forEach(n => n.remove());
        const txt = clone.textContent;
        const m = txt.match(priceRe);
        return m ? parseFloat(m[1].replace(/,/g, '')) : 0;
    }

    // Strategy 1: JSON-LD structured data (most reliable when present)
    document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
        try {
            const d = JSON.parse(s.textContent);
            const items = d['@graph'] || (d['@type'] === 'ItemList' ? d.itemListElement : null) || [d];
            items.forEach(item => {
                const name = item.name || (item.item && item.item.name);
                const url  = item.url  || (item.item && item.item.url) || '';
                const offerList = item.offers || (item.item && item.item.offers) || [];
                const offers = Array.isArray(offerList) ? offerList : [offerList];
                offers.forEach(o => {
                    const price = parseFloat(o.price || o.lowPrice || 0);
                    if (name && price > 0) {
                        results.push({ title: String(name), price, url: String(url), source: 'json-ld' });
                    }
                });
            });
        } catch(e) {}
    });
    if (results.length >= 3) return results.slice(0, 10);

    // Strategy 2a: WooCommerce-specific product cards
    document.querySelectorAll('li.product, ul.products li').forEach(card => {
        const titleEl = card.querySelector(
            '.woocommerce-loop-product__title, .product-title, h2, h3, h4'
        );
        const priceEl = card.querySelector('.price, .woocommerce-Price-amount');
        const linkEl  = card.querySelector('a[href]');
        const title   = titleEl ? titleEl.textContent.trim() : '';
        const price   = extractPrice(priceEl);
        const url     = linkEl ? linkEl.href : '';
        if (title && price > 0) {
            results.push({ title, price, url, source: 'woocommerce' });
        }
    });
    if (results.length >= 3) return results.slice(0, 10);

    // Strategy 2b: Common product-card CSS patterns (Shopify, custom themes, Takealot)
    const cardSelectors = [
        '[class*="product-item"]', '[class*="product-card"]',
        '[class*="search-result"]', '[class*="grid-product"]',
        '[class*="listing-inner"]', '[class*="product-listing"]',
        'article[class*="product"]', 'li[class*="product"]',
        '.product', '[data-product-id]',
    ];
    for (const sel of cardSelectors) {
        document.querySelectorAll(sel).forEach(card => {
            const titleEl = card.querySelector(
                'h1,h2,h3,h4,[class*="title"],[class*="name"],[class*="heading"]'
            );
            const priceEl = card.querySelector(
                '[class*="price"],[class*="amount"],[data-price],[class*="buybox"]'
            );
            const linkEl  = card.querySelector('a[href]');
            const title   = titleEl ? titleEl.textContent.trim() : '';
            const price   = extractPrice(priceEl);
            const url     = linkEl ? linkEl.href : '';
            if (title && price > 0) {
                results.push({ title, price, url, source: 'css-card' });
            }
        });
        if (results.length >= 3) break;
    }
    if (results.length >= 3) return results.slice(0, 10);

    // Strategy 3: Walk all text nodes for ZAR prices, find nearby titles.
    // Skip text inside <del> or <s> elements (crossed-out / original prices).
    function isStrikethrough(node) {
        let el = node.parentElement;
        while (el) {
            const tag = el.tagName && el.tagName.toLowerCase();
            if (tag === 'del' || tag === 's') return true;
            el = el.parentElement;
        }
        return false;
    }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode()) && results.length < 10) {
        if (isStrikethrough(node)) continue;
        const txt = node.textContent.trim();
        if (!priceRe.test(txt) || txt.length > 30) continue;
        const m = txt.match(priceRe);
        const price = m ? parseFloat(m[1].replace(/,/g,'')) : 0;
        if (price < 100) continue;  // Skip implausibly cheap items (accessories)
        // Walk up the DOM to find a title and link
        let el = node.parentElement;
        let title = '', url = '';
        for (let i = 0; i < 8 && el; i++, el = el.parentElement) {
            const hEl = el.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"]');
            const aEl = el.querySelector('a[href]');
            if (hEl && !title) title = hEl.textContent.trim();
            if (aEl && !url)   url   = aEl.href;
            if (title && url) break;
        }
        if (title && price > 0) results.push({ title, price, url, source: 'text-walk' });
    }

    return results.slice(0, 10);
}
"""


def _parse_price(raw: str) -> Optional[float]:
    """Parse a ZAR price string like 'R1,299.00' → 1299.0."""
    m = _PRICE_RE.search(raw)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _fuzzy_score(query: str, candidate: str) -> float:
    """Return rapidfuzz token_set_ratio score (0–100)."""
    try:
        from rapidfuzz import fuzz
        return fuzz.token_set_ratio(query.lower(), candidate.lower())
    except ImportError:
        # Fallback: simple containment check (for CI without rapidfuzz installed)
        q_words = set(query.lower().split())
        c_words = set(candidate.lower().split())
        if not q_words:
            return 0.0
        return len(q_words & c_words) / len(q_words) * 100


# Model-token regex: alphanumeric tokens with at least one digit, length ≥ 3.
# Catches "CFX50", "CFF35", "MR40F", "MD60F", "ARB10802605K", "CDF18", "TW75".
import re as _re_models
_MODEL_TOKEN_RE = _re_models.compile(r"\b([A-Za-z]*\d+[A-Za-z0-9]*)\b")

# Units that, when they're the ONLY letter in a digit+letter token, mean the
# token is a spec (size/voltage/wattage), not a model identifier.
# "16L", "12V", "75L", "240V" → spec.  "TW75", "CDF18", "CFX3" → model.
_SPEC_UNIT_RE = _re_models.compile(r"^\d+[lvwakgmcLVWAKGMC]$")


def _model_tokens(text: str) -> set[str]:
    """
    Extract DISTINCTIVE model tokens (e.g. CFX50, MR40F, TW75) from text.

    A token is distinctive if it:
      • has at least one digit, AND
      • either has 2+ alphabetic chars (CFX50, MR40F, COB16W, KID75)
        OR is 5+ chars long (catches things like "ARB10802605K")

    Excludes pure spec tokens like 16L, 12V, 75L — they're sizes/voltages
    and are shared by countless unrelated products.
    """
    tokens: set[str] = set()
    for m in _MODEL_TOKEN_RE.finditer(text):
        tok = m.group(1)
        if len(tok) < 3:
            continue
        # Reject pure size/voltage specs ("16L", "12V", "240V")
        if _SPEC_UNIT_RE.match(tok):
            continue
        # Require 2+ letters OR 5+ chars
        n_letters = sum(1 for ch in tok if ch.isalpha())
        if n_letters < 2 and len(tok) < 5:
            continue
        tokens.add(tok.lower())
    return tokens


def _brands_in(text: str) -> set[str]:
    """Return the set of known brand names appearing in the text (lowercased)."""
    t = text.lower()
    return {b for b in _KNOWN_BRANDS if b in t}


def _shares_model_token(query: str, candidate: str) -> bool:
    """
    Decide whether a candidate is plausibly the same product as the query.

    Guard 1 (brand): if the query mentions a known brand, the candidate must
    mention the SAME brand. This stops "Tsunami Cooler Box" from matching
    "Mobi Garden Cooler Box" purely on shared generic words.

    Guard 2 (model token): if both query and candidate have distinctive model
    tokens (e.g. CFX50, MR40F), they must share at least one. Stops "CFX50"
    matching "CDF18" on brand+category similarity alone.

    If neither guard applies — generic search with no brand and no model —
    fall back to the fuzzy score (caller's threshold decides).
    """
    # ── Guard 1: brand ───────────────────────────────────────────────
    q_brands = _brands_in(query)
    if q_brands:
        c_brands = _brands_in(candidate)
        # Candidate must contain at least one brand the query mentions.
        if not (q_brands & c_brands):
            return False

    # ── Guard 2: model token ─────────────────────────────────────────
    q_tokens = _model_tokens(query)
    c_tokens = _model_tokens(candidate)
    if q_tokens and c_tokens:
        return bool(q_tokens & c_tokens)
    # One side has no distinctive token → brand guard (if it ran) is enough,
    # otherwise trust the fuzzy score.
    return True


# Words that add noise to search queries but carry no product identity signal.
_NOISE_WORDS = frozenset({
    "the", "a", "an", "and", "or", "for", "with", "without", "in", "on", "at",
    "to", "of", "from", "by", "new", "used", "portable", "camping", "fridge",
    "freezer", "cooler", "refrigerator", "unit", "genuine", "original", "brand",
    "inc", "vat", "incl", "includes", "included", "including", "black", "white",
    "silver", "grey", "gray", "blue", "red", "green", "colour", "color",
    "12v", "240v", "12", "240", "volt", "volts", "dc", "ac",
})

# Regex patterns that identify meaningful product tokens to keep.
_KEEP_RE = re.compile(
    r"""
    \b(
        \d+\s*l(?:itre|iter)?s?\b  # capacity: 60L, 60 litre, 60L etc.
      | [a-z]{1,4}\d+[a-z0-9\-]*   # model codes: md60f, cfx3, ld-45, sc-35
      | \d+[a-z]{1,4}\d*           # numeric-first models: 55im, 40b
      | [a-z]+[-_]\d+[a-z0-9\-]*   # hyphenated models: sk-50, sc-35
    )\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Well-known camping fridge & cooler brands — used both for query construction
# AND for the brand-match guard in _shares_model_token.
_KNOWN_BRANDS = [
    # Compressor fridges
    "engel", "dometic", "snomaster", "iceco", "brass monkey", "evakool",
    "waeco", "bushman", "national luna", "alpicool", "icebox", "koolatron",
    "bodega", "antarctic star", "setpower", "bluetti", "jackery", "vitrifrigo",
    "isotherm", "webasto", "truma", "frozen", "ironman",
    # Hard/soft coolers (CFSA's full range)
    "tsunami", "flex", "yeti", "rtic", "pelican", "igloo", "coleman",
    "romer", "wild coolers", "bushbox", "leisure quip", "totai",
]


def _extract_key_terms(product_name: str, sku: str) -> str:
    """
    Extract the high-signal terms from a product description for use as a search query.

    Strategy:
    1. Always keep the brand name if recognised.
    2. Always keep tokens that look like model codes or capacities.
    3. Drop generic noise words.
    4. Append the SKU if it looks like a model code and isn't already present.

    Examples:
      "Engel 60L Portable Fridge Freezer MD60F White 12V/240V" + "MD60F"
      → "Engel MD60F 60L"

      "Dometic CFX3 55 Litre Dual Zone Fridge Freezer" + "CFX3-55"
      → "Dometic CFX3 55L CFX3-55"
    """
    text = product_name.lower()
    tokens: list[str] = []

    # 1. Grab any recognised brand (preserve original casing from product_name)
    for brand in _KNOWN_BRANDS:
        if brand in text:
            # Find original-case version in product_name
            start = text.find(brand)
            tokens.append(product_name[start:start + len(brand)])
            break

    # 2. Extract model codes and capacities
    for m in _KEEP_RE.finditer(product_name):
        tok = m.group(0).strip()
        if tok.lower() not in _NOISE_WORDS and tok not in tokens:
            tokens.append(tok)

    # 3. Include SKU if it looks like a model code and isn't already captured
    sku_clean = sku.strip()
    if sku_clean and sku_clean.lower() not in " ".join(tokens).lower():
        tokens.append(sku_clean)

    # 4. Fallback: if we extracted nothing useful, use the raw product_name
    if not tokens:
        return product_name.strip()

    return " ".join(tokens)


def build_search_queries(product_name: str, sku: str) -> list[str]:
    """
    Return an ordered list of search queries to try for a product, best-first.

    Query 1 — extracted key terms (brand + model code + capacity)
    Query 2 — SKU alone (model number is often the most specific search term)
    Query 3 — first 6 words of description (catches products with no model code)

    Duplicates are removed while preserving order.
    """
    queries: list[str] = []

    q1 = _extract_key_terms(product_name, sku)
    if q1:
        queries.append(q1)

    # SKU-only fallback — useful when description is vague but SKU is the model number
    sku_clean = sku.strip()
    if sku_clean and sku_clean not in queries:
        queries.append(sku_clean)

    # Short description fallback
    words = product_name.split()
    q3 = " ".join(words[:6]).strip()
    if q3 and q3 not in queries:
        queries.append(q3)

    return [q for q in queries if q]


_MAX_RETRIES = 2          # was 3 — 2 attempts is enough; 3rd rarely succeeds
_RETRY_BACKOFF_BASE = 1.5  # seconds; attempt N waits N * base before retrying
_PAGE_TIMEOUT_MS    = 15_000  # was 30_000 — fail fast on dead sites

# Minimum price to accept from text-walk (avoids stationery / accessories).
# Applied inside the JS snippet too for the text-walk path.
_MIN_PRICE_TEXT_WALK = 100


async def _fetch_candidates_http(
    *,
    competitor: dict,
    query: str,
) -> Optional[list[dict]]:
    """
    Lightweight HTTP-based candidate fetch for server-rendered sites.

    Uses Python's requests library (no browser) — fast and doesn't trigger
    Playwright bot-detection.  Falls back to None on any error so the caller
    can switch to the Playwright path.

    strategy: 'woocommerce_http'  — parses WooCommerce search results HTML.
    """
    import asyncio
    import re as _re
    strategy = competitor.get("strategy", "playwright")
    base_url  = competitor.get("base_url", "").rstrip("/")
    url_pattern = competitor.get("search_url_pattern", "")
    search_url = url_pattern.replace("{query}", quote_plus(query))

    def _do_request():
        import requests as _req
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-ZA,en;q=0.9",
        }
        resp = _req.get(search_url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        return resp.text

    try:
        html = await asyncio.to_thread(_do_request)
    except Exception as exc:
        log.warning("[%s] HTTP fetch failed for %r: %s", competitor["name"], query, exc)
        return None

    if not html:
        return None

    # No-results guard — only when there are zero type-product blocks AND
    # the page shows a definitive WooCommerce "no results" notice. Just
    # matching text anywhere is fragile (theme i18n bundles often contain
    # the literal string "No results found." inside JS translation tables
    # even when products are present — Trailpod hits this).
    has_products = bool(_re.search(
        r'<(?:li|div)[^>]+class="[^"]*type-product[^"]*"', html, _re.IGNORECASE,
    ))
    if not has_products:
        body_lower = html.lower()
        # WooCommerce-specific phrases (visible page text, not generic i18n bundle entries)
        if any(p in body_lower for p in [
            "no products were found matching your selection",
            "sorry, but nothing matched your search terms",
            "no products found",
        ]):
            return []

    candidates: list[dict] = []

    if strategy == "woocommerce_http":
        # WooCommerce search results HTML structure (confirmed from live site):
        #
        #   <li class="product type-product ...">
        #     <a href="https://...">
        #       <h2 class="woocommerce-loop-product__title">Title</h2>
        #       <span class="price">
        #         <del><span class="woocommerce-Price-amount"><bdi>
        #           <span class="woocommerce-Price-currencySymbol">&#82;</span>9,999.00
        #         </bdi></span></del>
        #         <ins><span class="woocommerce-Price-amount"><bdi>
        #           <span class="woocommerce-Price-currencySymbol">&#82;</span>9,275.00
        #         </bdi></span></ins>
        #       </span>
        #     </a>
        #   </li>
        #
        # Key points:
        # - Currency symbol is HTML-encoded as &#82; (letter R), not the literal R.
        # - Sale price is in <ins>; original (struck-through) price is in <del>.
        # - We must decode HTML entities AND strip <del> before matching prices.

        import html as _html

        # WooCommerce themes differ: standard WC uses <li class="...type-product...">
        # but some themes (e.g. XStore / Flatsome) use <div class="...type-product...">.
        # Strategy: find the start of each product element, then grab a fixed-size chunk
        # (5 kB) — enough to capture title and price without needing to balance div tags.
        raw_blocks: list[str] = []
        for m in _re.finditer(
            r'<(?:li|div)[^>]+class="[^"]*type-product[^"]*"',
            html, _re.IGNORECASE,
        ):
            raw_blocks.append(html[m.start(): m.start() + 5000])

        for block in raw_blocks:
            # ── Title ──────────────────────────────────────────────────
            # Try several patterns — WC themes differ in how they render titles:
            #  1. Standard WC:        class="woocommerce-loop-product__title"
            #  2. WoodMart/Flatsome:  class="wd-entities-title"  (used by THR Outdoor)
            #  3. Generic:            class="...product-title..."
            #  4. Flatsome <a></a></p> fallthrough
            #  5. <hN>...</hN> generic heading
            #  6. aria-label on the product-image-link <a> (WoodMart fallback)
            title_m = (
                _re.search(
                    r'class="[^"]*(?:loop-product__title|wd-entities-title|product-title)[^"]*"'
                    r'[^>]*>\s*(?:<a[^>]*>\s*)?([^<]+)',
                    block, _re.IGNORECASE,
                )
                # Flatsome/XStore: title is text node before </a></p>
                or _re.search(
                    r'>([^<>]{5,120})\s*</a>\s*</p>',
                    block,
                )
                # Generic heading fallback (<h2>/<h3>/<h4>, possibly wrapping an <a>)
                or _re.search(
                    r'<h[234][^>]*>\s*(?:<a[^>]*>)?\s*([^<]+?)\s*(?:</a>)?\s*</h[234]>',
                    block, _re.DOTALL | _re.IGNORECASE,
                )
                # WoodMart product-image-link aria-label fallback
                or _re.search(
                    r'aria-label="([^"]{10,150})"\s+class="[^"]*product-image-link',
                    block, _re.IGNORECASE,
                )
            )
            title = _html.unescape(
                _re.sub(r'<[^>]+>', '', title_m.group(1))
            ).strip() if title_m else ""

            # ── URL ────────────────────────────────────────────────────
            url_m = _re.search(r'href="([^"]+)"', block)
            url = url_m.group(1) if url_m else ""
            if url and url.startswith("/"):
                url = base_url + url

            # ── Price ──────────────────────────────────────────────────
            # WooCommerce puts the SALE price in <ins> and the old price in <del>.
            # Pull <ins> first; if no <ins>, restrict to the <span class="price"> block
            # to avoid picking up unrelated numbers elsewhere in the 5 kB chunk.
            def _extract_wc_price(blk: str) -> float:
                """
                Extract a price from a WooCommerce product block.

                Handles both number formats found in the wild:
                  - R 9,999.00   (period decimal, comma thousands — standard)
                  - R 9999,00    (comma decimal — South African, used by Trailpod)
                """
                def _parse_number(text: str) -> Optional[float]:
                    # Strip all tags and HTML entities
                    text = _html.unescape(text)
                    text = _re.sub(r'<[^>]+>', '', text)
                    # Try period-decimal first (R 9,999.00)  — most common
                    m = _re.search(r'(\d[\d,]*\.\d{2})', text)
                    if m:
                        try:
                            return float(m.group(1).replace(',', ''))
                        except ValueError:
                            pass
                    # Then comma-decimal (R 9999,00) — used by some ZA stores
                    m = _re.search(r'(\d[\d.]*),(\d{2})(?!\d)', text)
                    if m:
                        try:
                            whole = m.group(1).replace('.', '')   # remove thousand-dots if any
                            return float(f"{whole}.{m.group(2)}")
                        except ValueError:
                            pass
                    return None

                # 1. Sale price in <ins>
                ins_m = _re.search(r'<ins[^>]*>(.*?)</ins>', blk, _re.DOTALL | _re.IGNORECASE)
                if ins_m:
                    val = _parse_number(ins_m.group(1))
                    if val is not None:
                        return val

                # 2. <span class="price"> chunk — but first strip <del>…</del>
                #    blocks so the struck-through original price doesn't win
                #    over the actual sale price.
                price_start = _re.search(
                    r'<(?:span|div)[^>]+class="[^"]*\bprice\b[^"]*"', blk, _re.IGNORECASE,
                )
                if price_start:
                    chunk = blk[price_start.start(): price_start.start() + 800]
                    chunk_no_del = _re.sub(
                        r'<del[^>]*>.*?</del>', ' ', chunk, flags=_re.DOTALL | _re.IGNORECASE,
                    )
                    # Also remove screen-reader text that mentions "Original price"
                    chunk_no_del = _re.sub(
                        r'<span class="screen-reader-text">.*?</span>', ' ',
                        chunk_no_del, flags=_re.DOTALL | _re.IGNORECASE,
                    )
                    val = _parse_number(chunk_no_del)
                    if val is not None:
                        return val

                return 0.0

            price = _extract_wc_price(block)

            if title and price > 0:
                candidates.append({
                    "title": title, "price": price, "url": url,
                    "source": "woocommerce-http",
                })

    # Return empty list (not None) so the caller knows the fetch succeeded —
    # no Playwright fallback needed.  None is reserved for fetch failures.
    return candidates


async def _fetch_candidates_takealot_api(
    *,
    query: str,
) -> Optional[list[dict]]:
    """
    Hit Takealot's public REST API directly.

    Takealot is a Next.js SPA — scraping the rendered HTML is brittle and slow
    (the DOM takes 3-8 s to populate and `networkidle` never fires because of
    ad pixels). The API at api.takealot.com responds in ~1 s with clean JSON.

    Returns candidate list, or None on network failure.
    """
    import asyncio
    api_url = (
        "https://api.takealot.com/rest/v-1-16-0/searches/"
        "products,filters,facets,sort_options,breadcrumbs,slots_audience,context"
    )

    def _do_request():
        import requests as _req
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json",
            "Accept-Language": "en-ZA,en;q=0.9",
            "Referer": "https://www.takealot.com/",
        }
        resp = _req.get(
            api_url,
            params={"qsearch": query, "rows": 20},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    try:
        data = await asyncio.to_thread(_do_request)
    except Exception as exc:
        log.warning("[takealot] API fetch failed for %r: %s", query, exc)
        return None

    results = (
        data.get("sections", {}).get("products", {}).get("results", [])
    )
    candidates: list[dict] = []
    for r in results:
        core    = r.get("product_views", {}).get("core", {}) or {}
        buybox  = r.get("product_views", {}).get("buybox_summary", {}) or {}
        title   = core.get("title") or r.get("title") or ""
        # Try several price paths — Takealot sometimes nests under `pretty_price`
        price_raw = (
            buybox.get("price")
            or buybox.get("listing_price")
            or buybox.get("pretty_price", "").replace("R", "").replace(",", "").strip() or 0
        )
        try:
            price = float(price_raw) if price_raw else 0.0
        except (TypeError, ValueError):
            price = 0.0

        slug = core.get("slug") or r.get("uri", "")
        url  = f"https://www.takealot.com{slug}" if slug and slug.startswith("/") else slug

        if title and price > 0:
            candidates.append({
                "title": title, "price": price, "url": url,
                "source": "takealot-api",
            })

    return candidates


async def _fetch_candidates_shopify_json(
    *,
    competitor: dict,
    query: str,
) -> Optional[list[dict]]:
    """
    Lightweight Shopify product search via /search/suggest.json.

    Every Shopify storefront exposes this JSON endpoint. It returns clean
    product data (title, price, url) without any rendering. ~200 ms / request.

    Returns None on network failure, [] on a successful empty result.
    """
    import asyncio
    base_url = competitor.get("base_url", "").rstrip("/")
    if not base_url:
        return None
    api_url = f"{base_url}/search/suggest.json"

    def _do_request():
        import requests as _req
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json",
            "Accept-Language": "en-ZA,en;q=0.9",
        }
        resp = _req.get(
            api_url,
            params={
                "q": query,
                "resources[type]": "product",
                "resources[limit]": 10,
            },
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    try:
        data = await asyncio.to_thread(_do_request)
    except Exception as exc:
        log.warning(
            "[%s] Shopify JSON fetch failed for %r: %s",
            competitor["name"], query, exc,
        )
        return None

    products = (
        data.get("resources", {})
            .get("results", {})
            .get("products", [])
    )
    candidates: list[dict] = []
    for p in products:
        title = p.get("title") or ""
        price_raw = p.get("price") or p.get("price_min") or 0
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            price = 0.0
        url_path = p.get("url") or ""
        url = f"{base_url}{url_path}" if url_path.startswith("/") else url_path

        if title and price > 0:
            candidates.append({
                "title": title, "price": price, "url": url,
                "source": "shopify-json",
            })

    return candidates


async def _fetch_candidates_amazon_http(
    *,
    competitor: dict,
    query: str,
) -> Optional[list[dict]]:
    """
    Amazon search results parser.

    Amazon's HTML uses repeated card blocks marked by:
        <div ... data-asin="XXXXXXX" data-component-type="s-search-result">
    with the visible price in:
        <span class="a-offscreen">R 1,234.56</span>

    Amazon may serve a robot-check page if hit too hard; we fail soft.
    """
    import asyncio
    import re as _re
    base_url = competitor.get("base_url", "https://www.amazon.co.za").rstrip("/")
    url_pattern = competitor.get(
        "search_url_pattern",
        f"{base_url}/s?k={{query}}&ref=nb_sb_noss",
    )
    search_url = url_pattern.replace("{query}", quote_plus(query))

    def _do_request():
        import requests as _req
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-ZA,en;q=0.9",
        }
        resp = _req.get(search_url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        return resp.text

    try:
        html = await asyncio.to_thread(_do_request)
    except Exception as exc:
        log.warning(
            "[%s] Amazon HTTP fetch failed for %r: %s",
            competitor["name"], query, exc,
        )
        return None

    # Robot-check / "sorry" page
    if "Sorry, we just need to make sure" in html or "/errors/validateCaptcha" in html:
        log.warning("[%s] Amazon served a captcha page — skipping query %r", competitor["name"], query)
        return []

    # Find all product card start positions (ASIN must be non-empty)
    matches = list(_re.finditer(
        r'data-asin="([A-Z0-9]{10,})"[^>]*data-component-type="s-search-result"',
        html,
    ))
    if not matches:
        # Try reversed attribute order
        matches = list(_re.finditer(
            r'data-component-type="s-search-result"[^>]*data-asin="([A-Z0-9]{10,})"',
            html,
        ))
    if not matches:
        return []

    candidates: list[dict] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else min(start + 12000, len(html))
        block = html[start:end]
        asin  = m.group(1)

        # Title — Amazon uses <h2><span>TITLE</span></h2>, but also aria-labels
        title = None
        for pat in (
            r'<h2[^>]*>.*?<span[^>]*>([^<]+)</span>',
            r'<span class="a-size-medium[^"]*"[^>]*>([^<]+)</span>',
            r'<span class="a-size-base-plus[^"]*"[^>]*>([^<]+)</span>',
        ):
            tm = _re.search(pat, block, _re.DOTALL)
            if tm:
                title = tm.group(1).strip()
                break

        # Price — Amazon HTML has multiple a-offscreen spans per card (the main
        # price, a strikethrough RRP, savings, shipping, etc.). The MAIN price
        # is always inside a wrapping <span class="a-price …"> WITHOUT the
        # `a-text-price` modifier (that one is the strikethrough).
        #
        # The class attribute can be either:
        #   class="a-price"                              ← bare (matches XL price)
        #   class="a-price a-text-price"                 ← skip (strikethrough)
        #   class="a-price a-text-price a-size-base"     ← skip (savings)
        price = 0.0
        # Iterate all a-price wrappers, skip ones containing "a-text-price"
        for pw in _re.finditer(
            r'<span class="(a-price[^"]*)"[^>]*>(.{0,500}?)</span></span>',
            block, _re.DOTALL,
        ):
            class_str = pw.group(1)
            if "a-text-price" in class_str:
                continue
            inner = pw.group(2)
            offm = _re.search(r'<span class="a-offscreen">R[\s\xa0]*([\d,]+\.\d{2})', inner)
            if offm:
                try:
                    price = float(offm.group(1).replace(",", ""))
                    break
                except ValueError:
                    pass

        if title and price > 0:
            candidates.append({
                "title": title,
                "price": price,
                "url":   f"{base_url}/dp/{asin}",
                "source": "amazon-http",
            })

    return candidates


async def _fetch_candidates(
    *,
    competitor: dict,
    query: str,
    browser,
) -> Optional[list[dict]]:
    """
    Open one search page and return the raw candidate list, or None on failure.
    Retries up to _MAX_RETRIES times with exponential backoff.
    """
    name = competitor["name"]
    url_pattern = competitor.get("search_url_pattern", "")
    search_url = url_pattern.replace("{query}", quote_plus(query))

    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        ua = random.choice(_USER_AGENTS)
        page = None
        try:
            page = await browser.new_page(
                user_agent=ua,
                viewport={"width": 1280, "height": 800},
            )
            await page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf}",
                lambda r: r.abort(),
            )
            wait_until = competitor.get("wait_until", "domcontentloaded")
            await page.goto(search_url, wait_until=wait_until, timeout=_PAGE_TIMEOUT_MS)

            # Optional: wait for a specific selector (for SPAs that render after DOM ready).
            wait_sel = competitor.get("wait_for_selector")
            if wait_sel:
                sel_timeout = int(competitor.get("wait_for_selector_timeout", 8000))
                try:
                    await page.wait_for_selector(wait_sel, timeout=sel_timeout, state="attached")
                except Exception:
                    # Selector never appeared — likely no results page. Continue and let
                    # the extractor's no-results guard handle it (returns []).
                    pass

            await asyncio.sleep(random.uniform(0.3, 0.8))
            return await page.evaluate(_EXTRACT_JS)

        except Exception as exc:
            last_exc = exc
            log.warning(
                "[%s] Attempt %d/%d failed for query %r: %s",
                name, attempt, _MAX_RETRIES, query, exc,
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(attempt * _RETRY_BACKOFF_BASE)
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    log.error(
        "[%s] All %d attempts failed for query %r: %s",
        name, _MAX_RETRIES, query, last_exc,
    )
    return None


async def _scrape_one_competitor(
    *,
    competitor: dict,
    queries: list[str],       # ordered — best query first, fallbacks after
    match_threshold: float,
    max_results: int,
    browser,              # playwright Browser instance
) -> ScrapeResult:
    """
    Try each query in order against one competitor.

    For each query:
      1. Fetch search result candidates (with retry/backoff).
      2. Score every candidate title against the query using rapidfuzz.
      3. If the best score >= match_threshold, return that result.

    If all queries fail to reach the threshold, return NO_MATCH_FOUND.
    If every fetch attempt fails (network/timeout), return SCRAPE_FAILED.
    """
    name = competitor["name"]
    url_pattern = competitor.get("search_url_pattern", "")
    if not url_pattern:
        return ScrapeResult(competitor=name, status="SCRAPE_FAILED")

    all_fetches_failed = True
    strategy = competitor.get("strategy", "playwright")

    for query in queries:
        # Lightweight strategies first — faster, more reliable, no browser overhead.
        if strategy == "takealot_api":
            candidates = await _fetch_candidates_takealot_api(query=query)
        elif strategy == "shopify_json":
            candidates = await _fetch_candidates_shopify_json(
                competitor=competitor, query=query,
            )
        elif strategy == "amazon_http":
            candidates = await _fetch_candidates_amazon_http(
                competitor=competitor, query=query,
            )
        elif strategy == "woocommerce_http":
            candidates = await _fetch_candidates_http(
                competitor=competitor, query=query,
            )
            if candidates is None:
                # HTTP failed → fall back to Playwright for this query
                log.debug("[%s] HTTP fetch returned None, falling back to Playwright", name)
                candidates = await _fetch_candidates(
                    competitor=competitor, query=query, browser=browser,
                )
        else:
            candidates = await _fetch_candidates(
                competitor=competitor, query=query, browser=browser,
            )

        if candidates is None:
            # Network failure on this query — try next query
            continue

        all_fetches_failed = False

        if not candidates:
            log.debug("[%s] No candidates returned for query %r", name, query)
            continue

        # Score each candidate; track best across all candidates for this query.
        # We score against ALL candidates returned by the search (not just the
        # top max_results) and apply a model-token guard so false positives
        # like CFX50 → CDF18 get rejected even if the brand-word similarity is
        # above threshold.
        best_score = 0.0
        best: Optional[dict] = None
        original_query = queries[0]
        for c in candidates:
            title = str(c.get("title", ""))
            score = max(
                _fuzzy_score(query, title),
                _fuzzy_score(original_query, title),
            )
            # Model-token guard: query and title must share a distinctive
            # model token (e.g. CFX50) — unless one of them has no model
            # token at all (then we trust the fuzzy score).
            if not (
                _shares_model_token(query, title)
                or _shares_model_token(original_query, title)
            ):
                continue
            if score > best_score:
                best_score = score
                best = c

        if best is not None and best_score >= match_threshold:
            price_val: Optional[float] = None
            raw_price = best.get("price")
            if isinstance(raw_price, (int, float)) and raw_price > 0:
                price_val = round(float(raw_price), 2)
            elif isinstance(raw_price, str):
                price_val = _parse_price(raw_price)

            if price_val and price_val > 0:
                log.debug(
                    "[%s] ✓ %r → R%.2f (score=%.0f, query=%r)",
                    name, best.get("title", ""), price_val, best_score, query,
                )
                return ScrapeResult(
                    competitor=name,
                    product_name=str(best.get("title", "")),
                    price=price_val,
                    url=str(best.get("url", "")),
                    match_score=best_score,
                    status="OK",
                )

        log.debug(
            "[%s] No match above %.0f for query %r (best=%.1f)",
            name, match_threshold, query, best_score,
        )

    if all_fetches_failed:
        return ScrapeResult(competitor=name, status="SCRAPE_FAILED")
    return ScrapeResult(competitor=name, status="NO_MATCH_FOUND")


async def scrape_product(
    *,
    sku: str,
    product_name: str,
    competitors: list[dict],
    match_threshold: float = 80.0,
    max_results: int = 5,
    browser,
) -> ProductScrapeOutcome:
    """
    Scrape all enabled competitors for one product concurrently.

    Returns a ProductScrapeOutcome with one ScrapeResult per competitor.
    """
    queries = build_search_queries(product_name, sku)
    log.debug("[%s] Search queries: %s", sku, queries)
    enabled = [c for c in competitors if c.get("enabled", True)]

    tasks = [
        _scrape_one_competitor(
            competitor=c,
            queries=queries,
            match_threshold=match_threshold,
            max_results=max_results,
            browser=browser,
        )
        for c in enabled
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    outcome = ProductScrapeOutcome(sku=sku, product_name=product_name)
    for comp, res in zip(enabled, results):
        if isinstance(res, Exception):
            log.error("[%s] Unhandled exception for %s: %s", comp["name"], sku, res)
            outcome.results.append(ScrapeResult(competitor=comp["name"], status="SCRAPE_FAILED"))
        else:
            outcome.results.append(res)

    return outcome


async def scrape_all_products(
    *,
    products: list[dict],
    competitors: list[dict],
    match_threshold: float = 80.0,
    max_results: int = 5,
    inter_product_delay: float = 0.5,
) -> list[ProductScrapeOutcome]:
    """
    Scrape all products against all competitors.

    For each product, all competitor scrapes run concurrently.
    Products are processed sequentially with a short inter-product delay
    to avoid hammering any one site.

    Args:
        products: list of dicts with at least 'sku' and 'description' keys.
        competitors: loaded from competitors.yaml, sorted by priority.
        match_threshold: minimum fuzzy score to accept a match (0–100).
        max_results: top N search results evaluated per competitor.
        inter_product_delay: seconds between product scrapes.

    Returns:
        list[ProductScrapeOutcome] in the same order as products.
    """
    from playwright.async_api import async_playwright

    outcomes: list[ProductScrapeOutcome] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            for i, product in enumerate(products):
                sku = str(product.get("sku", ""))
                name = str(product.get("description", "") or sku)
                log.info(
                    "[competitor] Scraping %s — %s (%d/%d)",
                    sku, name[:50], i + 1, len(products),
                )
                outcome = await scrape_product(
                    sku=sku,
                    product_name=name,
                    competitors=competitors,
                    match_threshold=match_threshold,
                    max_results=max_results,
                    browser=browser,
                )
                outcomes.append(outcome)

                if i < len(products) - 1:
                    await asyncio.sleep(inter_product_delay)
        finally:
            await browser.close()

    return outcomes


def load_competitors(config_path=None) -> list[dict]:
    """
    Load and sort competitor configs from competitors.yaml.
    Returns only enabled competitors, sorted by priority (ascending).
    """
    from pathlib import Path
    import yaml

    if config_path is None:
        config_path = Path(__file__).parent.parent.parent / "config" / "competitors.yaml"

    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    competitors = data.get("competitors", [])
    enabled = [c for c in competitors if c.get("enabled", True)]
    return sorted(enabled, key=lambda c: c.get("priority", 99))
