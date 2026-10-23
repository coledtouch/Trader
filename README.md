# AI Trader

A private, self-hosted trading assistant for **one** Interactive Brokers (IBKR) account.
It scans a short list of liquid stocks (and optionally BTC/ETH) every few minutes, asks
two AI models, Claude and Grok, whether any setup is worth taking (a trade goes out only
when **both** agree; one model alone or a simple rule set are options), runs every idea
through a strict risk manager, and places small long-only orders with a protective stop. You
watch and control it from a web app at `https://desk.kaxo.dev` on your laptop or Android
phone (it installs like an app).

It runs on your own server (a DigitalOcean droplet), behind Cloudflare. Nobody else can
reach it, and no trading code runs anywhere you do not control.

## Reality check: read this first

**The goal of "$100 a day" on a $500-650 account means making 15-20% every single day.**
No strategy, human or AI, does that reliably. Top hedge funds are happy with 15-20% a
*year*. Small accounts that chase a daily dollar target almost always blow up: the
pressure to "make it back" leads to bigger size, worse entries and skipped stops.

So this app does something different:

- It shows the $100 target and your progress, but **nothing in the code ever trades bigger
  because you are behind**. Size depends only on your account size, the stop distance and
  fixed caps. After 3 losses in a row it trades *smaller*.
- It risks about 2% of the account per trade, stops for the day at a 6% loss, and stops
  opening new trades once the daily target is hit.
- It refuses trades whose fees would eat more than a quarter of the expected profit. On a
  small account that blocks most trades. That is the point: at IBKR a small stock round
  trip costs about $0.80 and a $100 crypto round trip about $2 (2%), see
  [docs/STRATEGY.md](docs/STRATEGY.md#fee-math-for-a-small-account).
- It logs every signal it sees, taken or not, and scores them later, so after a month you
  know whether a setup has an edge instead of guessing from one good day.

Running costs matter as much as trading results. A droplet ($12/month), market data
($4.50-14.50/month) and the AI APIs (Claude $1-90/month depending on model and settings;
asking Grok as well adds about $0.02-0.03 per decision: roughly 1.5x the AI bill with Opus,
2x with Sonnet and about 20x with Haiku, see the cost table)
can add up to 5-20% of a $650 account **per month** before the first winning trade. See
the cost table in [docs/SETUP_IBKR.md](docs/SETUP_IBKR.md#monthly-costs).

Realistic expectation: the first months are about learning whether any setup works at
all, on paper, with fake money. Treat any live money as tuition you can afford to lose.

## Lessons this app is built on

Two posts on Reddit's r/ai_trading shaped the design.

**1. A Grok agent given a $20K paper account and a "$1K/day minimum" quota on thin small caps.**

| What went wrong | What this app does |
|---|---|
| Paper fills on thin small caps were unrealistically kind | The simulator charges the spread plus slippage and market impact that grow as liquidity shrinks; the scanner skips names under 500k shares/day; entries are refused when the bid/ask spread is wider than 0.3% |
| The daily quota pushed the model to size up and chase | Hard risk caps in code; the AI can only scale size *down*; no code path looks at "how far behind" the day is |
| One great day "proved" the strategy | Full journal, equity curve, per-trade R multiples, a backtester with fees and slippage, and a 30-signal minimum before judging a setup |
| A bot lost its broker connection and almost held $50K of same-day SPX options over a weekend | Every stock entry carries a stop-loss order that lives at IBKR (it works even if this server is off); disconnect alerts; on reconnect the bot re-checks and re-protects positions; stocks are closed 10 minutes before the bell |
| LLM summaries "fluffed" the results | Every decision is stored with its inputs, output, tokens and cost; the dashboard shows fills reported by IBKR, not what the model claims |

**2. A builder whose bots were "trash" on paper for 7 months; two lucky manual halt trades made the money.**

| Lesson | What this app does |
|---|---|
| Write the rule down and log every qualifying signal for a month, including the ones you skip | The signal ledger records every scanner signal, why it was skipped, and how it played out 15/30/60 minutes later and at the close |
| Trading halts can reopen 30-40% lower; never chase a halt | No entries while a stock is halted, and none for 15 minutes after a halt |
| Value positions at the bid, not the last trade | Open P&L, stops and targets use the bid ("valued at the bid" on the dashboard) |
| Wide spreads quietly eat the edge | Entries refused above a 0.3% spread |
| The open is chaos | No new stock entries before 09:45 ET or after 15:30 ET |
| Stale trades tie up a small account | Time stop: a trade that has not made +0.5R after 45 minutes is closed |
| Size small | 2% risk per trade, max $250 per order, max 2 positions (all adjustable within hard caps) |

## How it fits together

```
 Your phone / laptop (browser or installed app)
          |  HTTPS
          v
 Cloudflare  --  Access: only your e-mail address, one-time PIN
          |  Cloudflare Tunnel (outbound from the server; no open ports)
          v
 DigitalOcean droplet (Ubuntu 24.04, firewall: SSH only)
 +-------------------------- docker compose ---------------------------+
 |                                                                     |
 |  cloudflared --http--> trader :8000  (web app + trading engine)     |
 |                          |    |    |                                |
 |                          |    |    +--> ./data/trader.db (journal)  |
 |                          |    +-------> Claude + Grok APIs (optional)|
 |                          |                                          |
 |                          +--TWS API--> ib-gateway :4004 paper       |
 |                            (private        :4003 live  --> IBKR     |
 |                             network only)                           |
 +---------------------------------------------------------------------+
```

- **trader**: this repository. A FastAPI web app plus the trading engine (scanner, AI or
  rule-based decisions, risk manager, journal).
- **ib-gateway**: IBKR's IB Gateway in a container
  ([gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker)) with IBC to
  log in automatically. Its API port is never reachable from outside the server.
- **cloudflared**: connects your Cloudflare Tunnel `desk` to the app, so `desk.kaxo.dev`
  works without opening any port on the droplet.

## Features

- Two-model consensus (default): Claude and Grok see the same data at the same time; a BUY
  goes out only when both buy the same symbol (the smaller size wins), and either one can
  close a position. Fewer trades and a higher AI bill (Grok adds about $0.02-0.03 per
  decision: ~1.5x with Opus, ~2x with Sonnet, ~20x with Haiku); switch to one model or to
  rules-only in Settings (leaving consensus needs a 2FA code). A missing API key never
  passes silently: the dashboard says which model is actually deciding.
- Dashboard: mode badge (SIM / PAPER / LIVE-SHADOW / LIVE-ARMED), equity, today's P&L vs.
  target and loss limit, open positions with stop and target, connection and market status,
  which AI is deciding, market-data warnings (such as IBKR's "competing live session"),
  AI spend today.
- Big buttons: start / stop the bot, run one cycle now (works even while the bot is
  stopped), **KILL** (cancel all orders and close every position), resume after a halt
  (needs a 2FA code).
- Activity: every AI decision with its reasoning and cost (with consensus, how each model
  voted), orders with block reasons, closed trades with P&L and R multiple, equity chart,
  daily stats.
- Signals: per-setup scorecard from the signal ledger, with a "too few to judge" badge
  under 30 evaluated signals.
- Settings: universe, risk limits, exit rules, decision mode, AI model and budget,
  autopilot or approve-every-order mode, live-trading arm/disarm, audit log.
- Bad-tick protection: crossed quotes and bids far from the last trade are ignored for
  valuing positions, and the bot's own stops and the daily loss limit fire only when the
  breach shows on two checks in a row (IBKR's own stop orders are not affected).
- Account switch guard: when the broker account changes (simulator, paper, live), the bot
  starts a fresh journal and never manages positions it did not open on that account.
- Phone alerts through [ntfy](https://ntfy.sh) (optional): disconnects, halts, orders
  waiting for approval.
- Security: Cloudflare Access + the app's own password and 2FA code, step-up 2FA for
  risky actions, audit log of every owner action. See [docs/SECURITY.md](docs/SECURITY.md).

## The safety ladder

Real-money orders need **three** things at once: `TRADER_ALLOW_LIVE=true` on the server, a
live (not paper) IBKR account, and you pressing **Arm** in the app with a fresh 2FA code.
Anything less and the bot cannot send a real order. Starting the app with
`TRADER_ALLOW_LIVE=false` clears the arm, so turning the gate back on always needs a fresh
Arm. Climb one step at a time:

| Step | What runs | Money | Move on when |
|---|---|---|---|
| 1. **Sim** | Built-in simulator with synthetic prices | Fake | You know the dashboard, the kill switch, approvals and the logs (a few days) |
| 2. **Paper** | IBKR paper account, real prices (with a data subscription) | Fake | 4+ weeks and 30+ evaluated signals for the setups you rely on, and the numbers still look positive after fees ([how to judge](docs/STRATEGY.md#judging-a-setup)) |
| 3. **Shadow** | Your live account, but every order is recorded as BLOCKED | None at risk | 1-2 weeks of sensible decisions on live data, PDT counter and balances read correctly |
| 4. **Armed live** | Live orders, small limits, autopilot off at first (you approve each order) | Real | Only increase limits after weeks of results that match paper |

Paper fills are kinder than real fills, so expect live results to be worse than paper.
You can disarm, stop or kill at any time from the app. Know the difference: **Stop** ends new
decisions while the bot keeps protecting open positions; **Disarm** blocks every order,
including the bot's own exits (only stops held at IBKR remain); **KILL** closes everything.

## Quick start: try the simulator on your Windows laptop

Optional: everything also runs on the droplet (see [docs/DEPLOY.md](docs/DEPLOY.md)).
This takes about 10 minutes and needs no IBKR account. Commands are for PowerShell.

1. Install Python 3.13 and Git (skip what you already have), then close and reopen PowerShell:
   ```powershell
   winget install --id Python.Python.3.13 -e
   winget install --id Git.Git -e
   ```
2. Get the code and install it into a private folder (`.venv`):
   ```powershell
   cd $HOME
   git clone https://github.com/coledtouch/Trader.git
   cd Trader
   py -3.13 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
   (For a private repository, Git opens a browser window to sign in to GitHub.)
3. Create a local settings file for plain `http://localhost` with the simulator:
   ```powershell
   Set-Content -Path .env -Encoding ascii -Value "TRADER_BROKER=sim", "TRADER_COOKIE_SECURE=false"
   ```
4. Create your password and 2FA. Install an authenticator app on your phone first (for
   example Google Authenticator, Microsoft Authenticator or Aegis), then:
   ```powershell
   .\.venv\Scripts\python.exe -m trader setup
   ```
   Choose a password of 12+ characters, scan the QR code with the authenticator app, and
   type the 6-digit code it shows.
5. Start the app and open <http://localhost:8000> in your browser:
   ```powershell
   .\.venv\Scripts\python.exe -m trader serve
   ```
   Press `Ctrl+C` in PowerShell to stop it. Simulated stocks trade 09:30-16:00 New York
   time on weekdays; outside those hours switch on **Trade crypto** in Settings to see the
   bot work (simulator only: crypto fees are too high for a small real account).
6. Optional: see what fees and slippage do to the rule-based strategy over 20 synthetic
   days (synthetic prices test the mechanics, not whether there is an edge):
   ```powershell
   .\.venv\Scripts\python.exe -m trader backtest --days 20 --equity 600
   ```

Other commands: `python -m trader status` (settings check, never prints secrets),
`reset-password`, `reset-2fa`, `logout-all`, `disarm` (the last four also disarm live
trading).

## Documentation

| Document | What it covers |
|---|---|
| [docs/SETUP_IBKR.md](docs/SETUP_IBKR.md) | Your remaining IBKR checklist (IB Key, paper account, market data, Claude and xAI keys) and the monthly cost table |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Installing and running it on the droplet, updates, backups, weekly 2FA, going live, troubleshooting |
| [docs/SECURITY.md](docs/SECURITY.md) | Who can reach what, how secrets are stored, the audit log, what to do if you lose your phone |
| [docs/STRATEGY.md](docs/STRATEGY.md) | How decisions are made, every risk and exit rule, the signal ledger, fee math |
| [docs/SPEC.md](docs/SPEC.md) | Technical specification for developers |

For developers: `pip install -r requirements-dev.txt`, then `ruff check .`,
`ruff format --check .` and `pytest -q` (offline; no IBKR or Anthropic access needed).

## Disclaimer

This is personal software, not financial advice and not a product. Trading stocks and
crypto can lose money quickly, including all of it; automated trading adds software,
connection and data risks on top. Past results, backtests and paper trading do not
predict live results. You are responsible for every order your account sends and for
complying with IBKR's terms and the rules that apply to you. Use money you can afford to
lose.
