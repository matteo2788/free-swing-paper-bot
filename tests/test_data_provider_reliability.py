from __future__ import annotations

import pandas as pd

from swing_bot.data_provider import YahooDataProvider


class _FakeYFinance:
    def __init__(self) -> None:
        self.kwargs = None

    def download(self, **kwargs):
        self.kwargs = kwargs
        return pd.DataFrame({"Close": [1.0]})


def test_download_raw_disables_yfinance_threads() -> None:
    fake = _FakeYFinance()
    YahooDataProvider._download_raw(fake, ["AAPL", "MSFT"], "1mo", "15m")
    assert fake.kwargs is not None
    assert fake.kwargs["threads"] is False


def test_partial_batch_retries_missing_ticker_individually(monkeypatch) -> None:
    provider = YahooDataProvider(batch_size=2, pause_seconds=0)
    calls: list[tuple[str, ...]] = []
    dummy = pd.DataFrame({"Close": [1.0]})

    def fake_download_raw(_yf, batch, _period, _interval):
        calls.append(tuple(batch))
        return dummy

    def fake_split(_downloaded, batch):
        if batch == ["AAPL", "MSFT"]:
            return {"AAPL": dummy}
        if batch == ["MSFT"]:
            return {"MSFT": dummy}
        return {}

    monkeypatch.setattr(provider, "_download_raw", fake_download_raw)
    monkeypatch.setattr(provider, "_split_download", fake_split)

    frames = provider.download(["AAPL", "MSFT"], period="1mo", interval="15m", retries=0)

    assert set(frames) == {"AAPL", "MSFT"}
    assert calls == [("AAPL", "MSFT"), ("MSFT",)]
