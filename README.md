# Free Swing Paper Bot

A fully free, rules-based **paper-trading swing scanner** that runs in GitHub Actions and sends structured alerts to Discord. It never connects to a brokerage, never places a real order, and never uses real money.

## What the bot does

1. Builds a sector-diverse universe from the S&P 500 plus major ETFs.
2. Uses the Daily chart to remove weak, illiquid, non-trending stocks.
3. Uses a 4-hour trend check as the final context gate.
4. Scores the newest **closed 15-minute candle** from 0 to 100.
5. Sends a B watchlist alert at 60–79 and an A setup alert at 80–100.
6. Creates a pending paper entry after an A setup.
7. Opens the paper position only when a later candle actually touches the entry zone.
8. Sends Discord alerts for entry, TP1, TP2, stop-loss, breakeven stop, runner exit, and time-based exit.
9. Sends an end-of-day summary of open paper positions and their current percentage P/L.
10. Saves every completed paper trade to `data/trades.csv`.

## The five score sections

Each section is worth 20 points:

- Relative-volume surge
- Price-structure breakout
- VWAP reclaim or hold
- RSI and MACD momentum
- Clear space before the next overhead resistance

The score only alerts on a **fresh crossing**. A stock sitting above 80 does not spam the same A alert every 15 minutes. It must fall below 80 before it can trigger a new A crossing later.

## Paper position rules

The default fake account is $10,000. Each paper trade risks at most 1% of that account, while the paper position itself is capped at 20% of the fake account. These values are only used to calculate a sensible simulated share count.

- TP1: close roughly 50% at +1R, then move the stop to breakeven.
- TP2: close roughly 25% at +2R.
- Runner: keep the remaining shares until price closes below the 1-hour EMA20.
- Time stop: close anything still stagnant after five trading days.
- Maximum open paper positions: five.

All values can be changed in `config/settings.yaml`.

## Free architecture

- **Hosting and scheduler:** GitHub Actions
- **Market data:** `yfinance`, using Yahoo Finance's publicly available data
- **Alerts:** Discord incoming webhook
- **Storage:** small JSON, CSV, and JSONL files plus GitHub Actions cache
- **Paid services required:** none

Free data and scheduled GitHub runs are best-effort. A scan can occasionally be delayed, and market data can occasionally be missing or delayed. That is why this project is deliberately paper-only.

## First-time setup

### 1. Create a new GitHub repository

Create an empty repository named:

`free-swing-paper-bot`

A private repository keeps the strategy files private. A public repository gives standard GitHub-hosted Actions unlimited free minutes, while a private GitHub Free repository has a monthly free-minute allowance. Never place the Discord webhook URL inside a public or private file.

### 2. Upload this project

Upload every file and folder from this project to the new repository. Make sure `.github/workflows/scanner.yml` is included.

### 3. Create the Discord server and channel

Create a server, then make a channel such as:

`#swing-paper-alerts`

Open that channel's settings, go to **Integrations → Webhooks**, create a webhook, and copy its URL. Treat the URL like a password.

### 4. Add the webhook as a GitHub secret

Inside the new repository, open:

**Settings → Secrets and variables → Actions → New repository secret**

Name it exactly:

`DISCORD_WEBHOOK_URL`

Paste the webhook URL as the value.

### 5. Allow the workflow to save state

Open:

**Settings → Actions → General → Workflow permissions**

Select:

**Read and write permissions**

Save the change.

### 6. Test Discord

Open the repository's **Actions** tab. Select **Free Swing Paper Bot**, click **Run workflow**, and choose:

`test-discord`

A green connection message should appear in Discord.

### 7. Build the first Daily pool

Run the workflow again and choose:

`refresh`

The first refresh is the slowest because it checks the broad universe. After that, normal scans only analyze the stocks that passed the Daily context filters.

### 8. Leave it alone

The workflow runs every 15 minutes during a wide weekday window. The Python code checks the official NYSE calendar and exits when the market is closed, on holidays, or before a full 15-minute candle has closed.

## Discord alert sequence

A normal winning sequence looks like this:

1. `A-SETUP • 84/100`
2. `PAPER POSITION OPENED`
3. `TP1 HIT • stop moved to breakeven`
4. `TP2 HIT • runner remains`
5. `RUNNER EXITED`

A losing sequence looks like this:

1. `A-SETUP • 81/100`
2. `PAPER POSITION OPENED`
3. `STOP-LOSS HIT • -X.XX% • -1.00R`

A setup can also expire without entering if price never touches the entry zone within two trading days.

## Important files

- `MASTER_SPEC.md` — the original strategy specification
- `config/settings.yaml` — every adjustable strategy setting
- `src/swing_bot/strategy.py` — context filters and 0–100 scoring
- `src/swing_bot/paper.py` — entries, sizing, TP/SL logic, and P/L tracking
- `src/swing_bot/alerts.py` — Discord message formatting
- `state/runtime.json` — scores, pending entries, and open paper positions
- `data/trades.csv` — completed trade results
- `data/events.jsonl` — full event history

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e . pytest
pytest -q
python -m swing_bot.cli --dry-run status
```

To send a real local Discord test, set `DISCORD_WEBHOOK_URL` in your terminal environment and run:

```bash
python -m swing_bot.cli test-discord
```

## Safety boundary

This repository intentionally contains no brokerage API, no real-order function, and no real-money execution path. It is a scanner, alert system, and paper-position simulator only.
