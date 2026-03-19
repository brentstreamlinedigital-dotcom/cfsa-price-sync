"""
Route a file (by extension) to the correct parser.
Returns a raw DataFrame before normalization.
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

import pandas as pd

from .csv_parser import parse_csv
from .pdf_parser import parse_pdf
from .xlsx_parser import parse_xlsx


SUPPORTED_EXTENSIONS = {"xlsx", "xls", "csv", "pdf"}


def parse_file(
    source: Union[bytes, str, Path],
    filename: str,
    sheet_name: Union[str, int, None] = None,
    skip_rows: int = 0,
) -> pd.DataFrame:
    """
    Dispatch to the appropriate parser based on file extension.

    Args:
        source:     Raw bytes or file path.
        filename:   Original filename (used to determine extension).
        sheet_name: For XLSX files only.
        skip_rows:  Header rows to skip.
    """
    ext = Path(filename).suffix.lstrip(".").lower()

    if ext in ("xlsx", "xls"):
        return parse_xlsx(source, sheet_name=sheet_name, skip_rows=skip_rows)
    elif ext == "csv":
        return parse_csv(source, skip_rows=skip_rows)
    elif ext == "pdf":
        return parse_pdf(source, skip_rows=skip_rows)
    else:
        raise ValueError(
            f"Unsupported file type: .{ext}. Supported: {SUPPORTED_EXTENSIONS}"
        )
