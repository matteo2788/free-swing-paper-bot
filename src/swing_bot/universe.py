from __future__ import annotations

import io
import logging
from pathlib import Path

import pandas as pd
import requests

LOGGER = logging.getLogger(__name__)
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def fetch_sp500_constituents(timeout: int = 20) -> pd.DataFrame:
    response = requests.get(
        SP500_URL,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 FreeSwingPaperBot/1.0"},
    )
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    if not tables or "Symbol" not in tables[0].columns:
        raise RuntimeError("Could not find the S&P 500 symbol table")
    frame = tables[0].copy()
    frame["Symbol"] = (
        frame["Symbol"].astype(str).str.strip().str.upper().str.replace(".", "-", regex=False)
    )
    if "GICS Sector" not in frame.columns:
        frame["GICS Sector"] = "UNKNOWN"
    frame["GICS Sector"] = frame["GICS Sector"].astype(str).str.strip()
    return frame[["Symbol", "GICS Sector"]].drop_duplicates(subset=["Symbol"])


def fetch_sp500_symbols(timeout: int = 20) -> list[str]:
    frame = fetch_sp500_constituents(timeout=timeout)
    return sorted(set(frame["Symbol"].tolist()))


def fetch_sp500_sector_map(timeout: int = 20) -> dict[str, str]:
    frame = fetch_sp500_constituents(timeout=timeout)
    return {
        str(row["Symbol"]): str(row["GICS Sector"])
        for _, row in frame.iterrows()
        if str(row["Symbol"]).strip()
    }


def load_fallback_symbols(path: str | Path) -> list[str]:
    frame = pd.read_csv(path)
    if "ticker" not in frame.columns:
        raise ValueError(f"Fallback universe must contain a 'ticker' column: {path}")
    return sorted({str(value).strip().upper() for value in frame["ticker"] if str(value).strip()})


def build_universe_with_sectors(
    universe_config: dict,
    root: Path,
) -> tuple[list[str], dict[str, str]]:
    fallback = root / str(universe_config.get("fallback_csv", "config/universe_fallback.csv"))
    symbols: list[str]
    sectors: dict[str, str] = {}
    try:
        source = str(universe_config.get("source", "sp500")).lower()
        if source != "sp500":
            raise ValueError(f"Unsupported universe source: {source}")
        constituents = fetch_sp500_constituents()
        symbols = sorted(set(constituents["Symbol"].tolist()))
        sectors = {
            str(row["Symbol"]): str(row["GICS Sector"])
            for _, row in constituents.iterrows()
        }
        LOGGER.info("Loaded %s S&P 500 symbols with sector metadata", len(symbols))
    except Exception as exc:
        LOGGER.warning("Live universe refresh failed; using fallback list: %s", exc)
        symbols = load_fallback_symbols(fallback)

    etfs = [str(value).strip().upper() for value in universe_config.get("include_etfs", [])]
    return sorted(set(symbols + etfs)), sectors


def build_universe(universe_config: dict, root: Path) -> list[str]:
    symbols, _ = build_universe_with_sectors(universe_config, root)
    return symbols
