from __future__ import annotations

import io
import logging
from pathlib import Path

import pandas as pd
import requests

LOGGER = logging.getLogger(__name__)

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def fetch_sp500_symbols(timeout: int = 20) -> list[str]:
    response = requests.get(
        SP500_URL,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 FreeSwingPaperBot/1.0"},
    )
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    if not tables or "Symbol" not in tables[0].columns:
        raise RuntimeError("Could not find the S&P 500 symbol table")
    symbols = [str(value).strip().upper().replace(".", "-") for value in tables[0]["Symbol"]]
    return sorted(set(symbols))


def load_fallback_symbols(path: str | Path) -> list[str]:
    frame = pd.read_csv(path)
    if "ticker" not in frame.columns:
        raise ValueError(f"Fallback universe must contain a 'ticker' column: {path}")
    return sorted({str(value).strip().upper() for value in frame["ticker"] if str(value).strip()})


def build_universe(universe_config: dict, root: Path) -> list[str]:
    fallback = root / str(universe_config.get("fallback_csv", "config/universe_fallback.csv"))
    symbols: list[str]
    try:
        source = str(universe_config.get("source", "sp500")).lower()
        if source != "sp500":
            raise ValueError(f"Unsupported universe source: {source}")
        symbols = fetch_sp500_symbols()
        LOGGER.info("Loaded %s S&P 500 symbols", len(symbols))
    except Exception as exc:
        LOGGER.warning("Live universe refresh failed; using fallback list: %s", exc)
        symbols = load_fallback_symbols(fallback)

    etfs = [str(value).strip().upper() for value in universe_config.get("include_etfs", [])]
    return sorted(set(symbols + etfs))
