# Paper Trade Entry Receipt

When the single-account paper bot opens a position, Discord shows a receipt-style alert containing:

- ticker
- shares
- simulated fill price per share
- total simulated money invested
- percentage of the paper account allocated
- paper account value before entry
- uninvested simulated cash remaining
- stop-loss price and maximum planned dollar/account-percent loss
- TP1 price, planned TP1 share count, and approximate TP1 partial profit
- TP2 price, planned TP2 share count, and approximate TP2 partial profit
- runner share count after TP2
- setup score
- trade ID
- event time, Discord send time, and measured delivery lag

The account equity shown at entry includes both the simulated stock position and any uninvested simulated cash.