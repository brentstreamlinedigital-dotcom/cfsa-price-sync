"""
Parse price-list PDFs into a raw DataFrame using pdfplumber table extraction.
Falls back to text extraction with heuristic row splitting if no tables found.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Union

import pandas as pd
import pdfplumber


def parse_pdf(
    source: Union[bytes, str, Path],
    skip_rows: int = 0,
) -> pd.DataFrame:
    """
    Extract tabular data from a PDF.

    Strategy:
    1. Try pdfplumber table extraction on every page (best for structured PDFs).
    2. If no tables found, fall back to text-based extraction.

    Args:
        source:    Raw bytes, file path, or Path object.
        skip_rows: Rows to skip after the header is detected.

    Returns:
        DataFrame with string-typed columns.
    """
    if isinstance(source, (str, Path)):
        raw = Path(source).read_bytes()
    else:
        raw = source

    df = _extract_tables(raw)

    if df is None or df.empty:
        df = _extract_text_fallback(raw)

    if df is None or df.empty:
        raise ValueError("No tabular data found in PDF — manual review required.")

    if skip_rows:
        df = df.iloc[skip_rows:].reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_tables(raw: bytes) -> pd.DataFrame | None:
    all_rows: list[list] = []
    headers: list[str] | None = None

    with pdfplumber.open(BytesIO(raw)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                # First non-empty table on first page defines headers
                if headers is None:
                    headers = [
                        str(h).strip() if h else f"col_{i}"
                        for i, h in enumerate(table[0])
                    ]
                    data_rows = table[1:]
                else:
                    data_rows = table

                for row in data_rows:
                    if any(cell for cell in row):
                        # Pad/truncate to header length
                        padded = list(row) + [None] * len(headers)
                        all_rows.append(padded[: len(headers)])

    if not headers or not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=headers)
    df = df.astype(str).replace("None", "").replace("nan", "")
    return df.dropna(how="all")


def _extract_text_fallback(raw: bytes) -> pd.DataFrame | None:
    """
    Last-resort: extract raw text, split on consistent whitespace.
    Works for simple fixed-width PDFs with no real tables.
    """
    lines = []
    with pdfplumber.open(BytesIO(raw)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.extend(text.splitlines())

    # Filter blank lines
    lines = [l for l in lines if l.strip()]
    if not lines:
        return None

    # Treat first line as header
    rows = [line.split() for line in lines]
    max_cols = max(len(r) for r in rows)
    headers = [f"col_{i}" for i in range(max_cols)]

    padded = [r + [""] * (max_cols - len(r)) for r in rows]
    return pd.DataFrame(padded[1:], columns=headers)
