"""
Parse XLSX/XLS files into a raw DataFrame.
Applies sheet selection and header row skipping from supplier config.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Union

import pandas as pd


def parse_xlsx(
    source: Union[bytes, str, Path],
    sheet_name: Union[str, int, None] = None,
    skip_rows: int = 0,
) -> pd.DataFrame:
    """
    Parse an Excel file into a DataFrame.

    Args:
        source:     Raw bytes, file path string, or Path object.
        sheet_name: Sheet name (str) or index (int). None = first sheet.
        skip_rows:  Number of rows to skip before the header row.

    Returns:
        DataFrame with string-typed columns (casting is handled by normalizer).
    """
    if isinstance(source, (str, Path)):
        buf: Union[bytes, BytesIO] = Path(source).read_bytes()
    else:
        buf = source

    io = BytesIO(buf) if isinstance(buf, bytes) else buf

    # sheet_name=None reads all sheets; we want a single sheet
    target_sheet = 0 if sheet_name is None else sheet_name

    df = pd.read_excel(
        io,
        sheet_name=target_sheet,
        skiprows=skip_rows,
        dtype=str,          # read everything as str; normalizer casts types
        header=0,
    )

    df = _clean(df)
    return df


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all")          # drop rows that are entirely empty
    df = df.reset_index(drop=True)
    # Strip whitespace from column names and string values
    df.columns = [str(c).strip() for c in df.columns]
    for col in df.columns:
        df[col] = df[col].apply(lambda v: v.strip() if isinstance(v, str) else v)
    return df
