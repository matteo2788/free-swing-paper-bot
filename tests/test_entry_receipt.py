from __future__ import annotations

from swing_bot.entry_receipt import _price_move_percent


def test_price_move_percent() -> None:
    assert round(_price_move_percent(100.0, 105.0), 2) == 5.0
    assert round(_price_move_percent(100.0, 98.0), 2) == -2.0
    assert _price_move_percent(0.0, 100.0) == 0.0
