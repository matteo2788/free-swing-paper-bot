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

    @staticmethod
    def _download_raw(yf: Any, batch: list[str], period: str, interval: str) -> pd.DataFrame | None:
        """Download one batch without yfinance worker threads.

        yfinance's shared timezone/cache database can raise ``database is locked``
        when many ticker workers initialize concurrently. The bot values complete,
        deterministic research data more than a small download-speed gain, so batch
        requests are intentionally serialized inside yfinance.
        """
        return yf.download(
            tickers=" ".join(batch),
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=False,
            prepost=False,
            threads=False,
            progress=False,
            actions=False,
            timeout=20,
        )

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
                    result = self._download_raw(yf, batch, period, interval)
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
                batch_frames: dict[str, pd.DataFrame] = {}
            else:
                batch_frames = self._split_download(result, batch)
                frames.update(batch_frames)

            # yf.download can return a non-empty batch even when one or more ticker
            # requests failed. Retry any missing names one at a time so a partial
            # response cannot silently remove the entire ticker from the alpha scan.
            missing = [ticker for ticker in batch if ticker not in batch_frames]
            for ticker in missing:
                recovered = False
                for attempt in range(retries + 1):
                    try:
                        single = self._download_raw(yf, [ticker], period, interval)
                        if single is not None and not single.empty:
                            split = self._split_download(single, [ticker])
                            if ticker in split:
                                frames[ticker] = split[ticker]
                                recovered = True
                                break
                    except Exception as exc:
                        LOGGER.warning(
                            "Single-ticker retry %s failed for %s: %s",
                            attempt + 1,
                            ticker,
                            exc,
                        )
                    time.sleep(1.5 * (attempt + 1))
                if not recovered:
                    LOGGER.warning("No usable data returned for ticker: %s", ticker)

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
