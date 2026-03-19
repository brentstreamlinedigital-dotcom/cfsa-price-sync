"""
Parse CSV files into a raw DataFrame.
Handles encoding detection and header skipping.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Union

import chardet
import pandas as pd


def parse_csv(
    source: Union[bytes, str, Path],
    skip_rows: int = 0,
) -> pd.DataFrame:
    """
    Parse a CSV file into a DataFrame.

    Args:
        source:    Raw bytes, file path, or Path object.
        skip_rows: Number of rows to skip before the header row.

    Returns:
        DataFrame with string-typed columns.
    """
    if isinstance(source, (str, Path)):
        raw = Path(source).read_bytes()
    else:
        raw = source

    encoding = chardet.detect(raw)["encoding"] or "utf-8"

    df = pd.read_csv(
        BytesIO(raw),
        skiprows=skip_rows,
        encoding=encoding,
        dtype=str,
        on_bad_lines="warn",
    )

    df = _clean(df)
    return df


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all")
    df = df.reset_index(drop=True)
    df.columns = [str(c).strip() for c in df.columns]
    for col in df.columns:
        df[col] = df[col].apply(lambda v: v.strip() if isinstance(v, str) else v)
    return df
