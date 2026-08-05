from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

import pandas as pd

from .indicators import clean_ohlcv

LOGGER = logging.getLogger(__name__)


class YahooDataProvider:
    """Best-effort free market data adapter for research and paper trading only."""

    def __init__(self, batch_size: int = 60, pause_seconds: float = 1.0) -> None:
        self.batch_size = max(1, int(batch_size))
        self.pause_seconds = max(0.0, float(pause_seconds))

    @staticmethod
    def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
        for index in range(0, len(values), size):
            yield values[index : index + size]

    def download(
        self,
        tickers: list[str],
        *,
        period: str,
        interval: str,
        retries: int = 2,
    ) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        normalized = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
        frames: dict[str, pd.DataFrame] = {}
        for batch in self._chunks(normalized, self.batch_size):
            result: pd.DataFrame | None = None
            for attempt in range(retries + 1):
                try:
                    result = yf.download(
                        tickers=" ".join(batch),
                        period=period,
                        interval=interval,
                        group_by="ticker",
                        auto_adjust=False,
                        prepost=False,
                        threads=True,
                        progress=False,
                        actions=False,
                        timeout=20,
                    )
                    if result is not None and not result.empty:
                        break
                except Exception as exc:  # yfinance can raise several network/parser exceptions
                    LOGGER.warning(
                        "Download attempt %s failed for %s: %s",
                        attempt + 1,
                        ",".join(batch),
                        exc,
                    )
                time.sleep(1.5 * (attempt + 1))

            if result is None or result.empty:
                LOGGER.warning("No data returned for batch: %s", ",".join(batch))
                continue

            frames.update(self._split_download(result, batch))
            if self.pause_seconds:
                time.sleep(self.pause_seconds)
        return frames

    def _split_download(self, downloaded: pd.DataFrame, batch: list[str]) -> dict[str, pd.DataFrame]:
        output: dict[str, pd.DataFrame] = {}
        if isinstance(downloaded.columns, pd.MultiIndex):
            first_level = set(map(str, downloaded.columns.get_level_values(0)))
            second_level = set(map(str, downloaded.columns.get_level_values(1)))
            for ticker in batch:
                try:
                    if ticker in first_level:
                        frame = downloaded[ticker]
                    elif ticker in second_level:
                        frame = downloaded.xs(ticker, axis=1, level=1)
                    else:
                        continue
                    cleaned = self._normalize_index(clean_ohlcv(frame))
                    if not cleaned.empty:
                        output[ticker] = cleaned
                except Exception as exc:
                    LOGGER.debug("Could not normalize %s: %s", ticker, exc)
        elif len(batch) == 1:
            cleaned = self._normalize_index(clean_ohlcv(downloaded))
            if not cleaned.empty:
                output[batch[0]] = cleaned
        return output

    @staticmethod
    def _normalize_index(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
            return frame
        result = frame.copy()
        if result.index.tz is not None:
            result.index = result.index.tz_convert("America/New_York")
        return result


def serializable_number(value: Any, digits: int = 6) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return round(number, digits)
