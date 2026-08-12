# Quant Alpha Engine

This repository is a research and paper-trading simulator. It does not route live brokerage orders.

## Pipeline

1. **Macro regime gate** evaluates SPY/QQQ 20-EMA state and slope plus VIX/SPY-ATR expansion. New long paper entries are suppressed when the macro gate is risk-off. Existing paper positions continue to be managed by the 5-minute monitor.
2. **Composite Alpha Score** ranks the eligible cross-section using volatility squeeze/expansion, sector-relative strength, volume plus anchored VWAP, and options gamma when real gamma data is available.
3. **ATR risk sizing** risks approximately 2% of current simulated account equity at the initial 1.5x-ATR stop, subject to available simulated capital and whole-share sizing.
4. **Execution friction** models 0.05% adverse slippage and the configured transaction fee on each entry and exit fill.
5. **Dynamic exits** include early 5-minute VWAP invalidation and a 2x-ATR Chandelier stop for the runner after TP2.
6. **Analytics** maintain paper-account equity, win rate, expectancy per closed trade, profit factor, and maximum drawdown.

## Composite factors

Configured target weights are:

- Volatility contraction/expansion: 30%
- Sector-relative strength: 30%
- Volume anomaly / anchored VWAP: 20%
- Options gamma / open-interest positioning: 20%

The options gamma factor is never inferred from Yahoo OHLCV. It is `UNAVAILABLE` unless a valid external snapshot is explicitly configured. When gamma is unavailable, the composite score renormalizes across the measured factors instead of assigning fabricated values.

## Optional gamma snapshot

`options_gamma.enabled` is false by default. A future real options-data adapter may write `data/options_gamma.json` in this shape:

```json
{
  "as_of": "2026-08-12T14:30:00Z",
  "tickers": {
    "AAPL": {
      "gamma_acceleration_score": 1.8,
      "call_oi_ratio": 2.1,
      "overhead_strike": 250.0
    }
  }
}
```

The snapshot must be recent enough to pass `options_gamma.max_age_minutes`. The engine treats missing, stale, invalid, or ticker-missing data as unavailable.

## Hard signal gates

A top-tier long candidate requires all measured hard gates to pass:

- prior Bollinger bandwidth in the configured bottom percentile while BB was inside KC;
- squeeze release, bandwidth expansion, and smoothed momentum crossing above zero;
- 5d/20d sector-relative-strength cross-sectional Z-score at or above the configured threshold;
- RVOL at or above the configured minimum while price is above anchored VWAP from the prior swing high;
- positive gamma acceleration when real gamma data is available.

The scanner still allows only one pending or open paper position at a time and selects the strongest eligible candidate by Alpha Score.

## Paper execution model

- Initial stop: 1.5x signal ATR below simulated fill.
- TP1: 1.5x signal ATR above simulated fill.
- TP2: 2.5x signal ATR above simulated fill.
- Risk budget: 2% of current simulated account equity by default.
- Slippage: 0.05% adverse per simulated fill.
- Transaction fee: 0.01% of simulated notional per fill by default and configurable.
- Early invalidation: during the first three closed 5-minute bars after entry, a close below entry-session VWAP with declining volume exits the position before the full stop.
- Runner: after TP2, the remaining shares use a Chandelier stop at the highest high since entry minus 2x current 5-minute ATR; the trail only ratchets upward.

All metrics and fills are simulated and depend on delayed/free market data characteristics.
