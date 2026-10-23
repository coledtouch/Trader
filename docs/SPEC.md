# AI Trader: Technical Specification

This is the build contract for every module. If you change a contract here, update the
code that depends on it and this file in the same change.

## 1. Purpose and non-negotiables

A private, self-hosted web app (one owner, no other users) that runs an AI-assisted
trading bot against an Interactive Brokers (IBKR) account. The owner manages it from a
laptop and an Android phone (installable PWA).

Owner context that shapes the design:
- Starting capital: **$250–$750**, IBKR Pro, **"No Borrow Margin"** account type (can't
  borrow, can use unsettled funds; may still be subject to the old PDT day-trade count
  until IBKR adopts FINRA's 2026 intraday-margin rule).
- Owner's stated goal: "$100/day minimum". That is ~13–40% per day on this capital. It is
  **not achievable reliably**; chasing it is the classic way small accounts blow up.
  The app shows the target and progress toward it, but **no code path may increase risk
  because the bot is behind its target** (no martingale, no "make it back" sizing).
- Inspiration: a Reddit post where a Grok agent was given a $20K *paper* account and a
  "$1K/day minimum" quota on thin small caps. Lessons from that thread that are
  requirements here:
  1. Paper fills on thin names are unrealistically kind → simulator models spread,
     slippage and partial liquidity; risk manager filters illiquid names.
  2. A daily quota pushes a model to size up and chase → hard risk caps in code;
     the AI can only scale size *down*.
  3. One good day proves nothing → full journal, equity curve, per-trade R multiples,
     backtester with fees and slippage.
  4. A bot that lost its broker connection almost held $50K of 0DTE SPX over a weekend →
     **broker-side protective stops** placed with every stock entry, disconnect alerts,
     reconcile + re-protect on reconnect, flatten stocks before the close.
  5. LLMs "fluff" their outputs → every decision is logged with timestamp, inputs
     (candidates), outputs, tokens and cost; the UI shows what actually happened
     (fills from the broker), not what the model claims.

Safety rules (enforced in code, covered by tests):
- Default broker is the offline simulator. Real-money orders need **all three**:
  `TRADER_ALLOW_LIVE=true` env, a live (non-"DU") IBKR account, and the owner "arming"
  live trading in the UI with a fresh TOTP code. Otherwise a live account runs in
  **shadow mode**: decisions are logged and every ENTRY is recorded as BLOCKED. Exits
  for positions the engine opened (those with positions_meta) still go out: engine-side
  stops/targets, time stops, breakeven/trailing moves, re-placed stops, flatten before
  the close, AI/approved CLOSEs, and the daily-loss flatten (bot positions only). The
  owner-confirmed kill closes everything (owner decision, 2026-10-23: exits only reduce risk). The arm lasts only
  while the gate stays open: a process that starts with `TRADER_ALLOW_LIVE=false` clears it,
  and so do the CLI's `logout-all`, `reset-2fa`, `reset-password` and `disarm` (7.5).
- Every entry must have a stop-loss below entry; long-only; no shorting; no options
  writing; no leverage beyond `max_total_exposure_pct` (≤100%).
- Hard caps in `trader.models.HARD_CAPS` clamp user settings.
- Daily loss limit → halt (and flatten if `flatten_on_halt`). Resume requires TOTP.
- In LIVE mode, block stock entries when market data is delayed.
- Block stock entries when `AccountSnapshot.day_trades_remaining` is known and < 1 (the
  engine first subtracts its own same-day round trips closed in the last 5 minutes: IBKR
  refreshes the counter only every few minutes, 7.5).
- Kill switch (UI button) → halt, cancel all orders, flatten all positions.
- LLM spend is tracked; daily LLM budget exhausted → fall back to rules-only decisions
  (never trade blindly), and log it. In consensus mode (7.4) the rules then only manage
  exits: a BUY needs both models.
- Default decision mode is **consensus**: a BUY only when Claude AND Grok both BUY the same
  symbol (7.4). A missing API key is never silent (status warning).

## 2. Stack

Python 3.12+ (3.13 in Docker), FastAPI + Uvicorn, asyncio throughout, SQLite (stdlib),
`ib_async` 2.1.0 for IBKR (TWS API via IB Gateway), `anthropic` 2.1.0 for Claude,
`argon2-cffi`, `pyotp`, `cryptography` (Fernet), `httpx` (ntfy, the xAI/Grok REST API,
tests), `qrcode`.
Frontend: static HTML/CSS/vanilla JS (no build step, no CDN), PWA manifest + service
worker. Strict CSP: no inline scripts or styles.

Dependencies are pinned in `requirements.txt` / `requirements-dev.txt`. Do not add new
runtime dependencies without a strong reason.

## 3. Layout and module ownership

```
trader/
  __init__.py
  __main__.py          # `python -m trader [serve|setup|reset-password|backtest|...]` -> cli.main
  cli.py               # argparse CLI (setup wizard, password/TOTP reset, backtest, status, ntfy test alert)
  config.py            # AppConfig (env, secrets)                       [DONE - shared]
  models.py            # shared pydantic types                         [DONE - shared]
  db.py                # SQLite Database class                          [DONE - shared]
  timeutil.py          # market calendar/hours helpers (NY time, holidays)
  indicators.py        # pure-python indicators
  risk.py              # RiskManager (sizing + checks + halts)
  engine.py            # Engine: loops, orchestration, journal, reconciliation; build_engine()
  notify.py            # Notifier (ntfy) - optional
  backtest.py          # backtester for the rule-based strategy (CLI entry)
  broker/
    base.py            # Broker ABC                                     [DONE - shared]
    sim.py             # SimBroker (synthetic market, realistic fills)
    ibkr.py            # IBKRBroker (ib_async)
  strategy/
    signals.py         # Scanner: setups -> Candidate list
    decide.py          # DecisionMaker protocol + RuleBasedDecisionMaker
    common.py          # shared LLM output schema, validation, failure helpers (7.4)
    claude.py          # ClaudeDecisionMaker (Anthropic API, structured output)
    grok.py            # GrokDecisionMaker (xAI Responses API via httpx, structured output)
    consensus.py       # ConsensusDecisionMaker (Claude AND Grok must agree to BUY)
    prompts.py         # system prompt + context rendering (shared by Claude and Grok)
  security/
    auth.py            # passwords, TOTP, sessions, CSRF, rate limiting, step-up
    crypto.py          # Fernet helpers for secrets at rest
  web/
    app.py             # create_app(): FastAPI app, middleware, routes
    static/            # index.html, login.html, app.js, login.js, styles.css,
                       # manifest.webmanifest, sw.js, icon.svg, icon-192.png?, icon-512.png?
tests/                 # pytest; no network; sim broker + fake LLM
docs/                  # SPEC.md (this), SETUP_IBKR.md, DEPLOY.md, SECURITY.md, STRATEGY.md
Dockerfile, docker-compose.yml, .env.example, README.md, .github/workflows/ci.yml
```

## 4. Shared contracts (already implemented)

- `trader/models.py`: all domain types. Key ones: `Instrument` (`.key` = "STK:AAPL"),
  `Bar`, `Quote` (`.price`), `Position`, `AccountSnapshot`, `OrderRequest`, `OrderRecord`,
  `Fill`, `Candidate`, `AIDecision`, `DecisionBatch`, `RiskLimits`, `HARD_CAPS`,
  `LOWER_IS_LOOSER`, `BotSettings`, `RiskVerdict`, `DayStats`, `TradeRecord`,
  `EngineStatus`, `DecisionContext`.
- `trader/config.py`: `AppConfig` (env prefix `TRADER_`), `get_config()`.
- `trader/db.py`: `Database` - kv/flags, settings, auth row, sessions, login attempts,
  audit, decisions, orders, fills, positions_meta, trades, daily_stats, equity.
- `trader/broker/base.py`: `Broker` ABC + `BrokerError`; default `round_qty`,
  `close_position`, `flatten_all`.

Read these files before writing code that uses them. Do not change their public
signatures; if you truly need a new field or method, add it (backward compatible) and
mention it in your final report.

## 5. Module specifications

### 5.1 `trader/timeutil.py`
- `NY = ZoneInfo(config.market_timezone)` default "America/New_York".
- `trading_day(now: datetime) -> str` - NY calendar date "YYYY-MM-DD" (crypto P&L day
  also rolls at NY midnight; one day boundary for everything).
- `is_us_market_holiday(d: date) -> bool` - NYSE full-day holidays for 2025–2028
  (hard-coded list; note half-days: 13:00 close on day after Thanksgiving, Christmas Eve,
  July 3 when applicable).
- `stock_market_open(now) -> bool` (regular session 09:30–16:00 NY, weekdays, not holiday,
  respects half-day early closes).
- `market_close_time(day) -> datetime` (16:00 or 13:00 NY, tz-aware).
- `in_entry_window(now, start="09:45", cutoff="15:30") -> bool` (also false on holidays
  and after an early close minus 20 min).
- `minutes_to_close(now) -> float | None`.

### 5.2 `trader/indicators.py` (pure functions on `list[float]` / `list[Bar]`)
`sma, ema, rsi (Wilder), atr (Wilder), bollinger(period, k) -> (mid, upper, lower),
session_vwap(bars, tz) (resets each NY session), highest, lowest, pct_change,
avg_volume`. Return lists aligned with the input (None for warm-up) or a latest scalar
helper `last(values)`. Unit-tested against hand-computed values.

### 5.3 `trader/strategy/signals.py`
`Scanner(settings: BotSettings)` with `scan(instrument, bars, quote, now) -> list[Candidate]`
and `snapshot(instrument, bars, quote) -> dict[str, float]` (price, ema9, ema21, rsi14,
atr14, atr_pct, vwap, dist_vwap_pct, rel_volume, day_change_pct, high_of_day,
low_of_day, ...). Setups (long-only), each with score 0..1, ATR-based stop and target
with reward:risk >= 2 by default:
- `orb_breakout` - price breaks the opening-range (first 15 min) high on rel volume > 1.5,
  above VWAP, EMA9 > EMA21. (Stocks only.)
- `vwap_reclaim` - crosses back above VWAP after trading below, rising EMA9.
- `trend_pullback` - EMA9 > EMA21 rising, pullback to EMA21 with bullish close.
- `rsi_reversion` - RSI14 < 30 turning up, price at/below lower Bollinger band, not in a
  steep downtrend (price within 2 ATR of EMA21). Smaller score.
- `volume_spike` - bar volume >= 3x the average of the previous 20 bars on a bullish bar
  (close > open), close above SMA20 (and SMA50 when available) and above the session open
  (stocks) or 24h open (crypto), positive 20-session/20-day trend when the history allows,
  ATR% within 0.3-8% (5-minute bars, scaled for other sizes); stocks only from 09:45 NY.
  Stop 1.5 ATR, target >= 2R.
Filters: price >= `risk.min_price`, avg volume >= `min_avg_volume` (stocks), enough bars.
Crypto uses the same setups minus ORB, with a rolling 24h "session" for VWAP.

### 5.4 `trader/strategy/decide.py`
```python
class DecisionMaker(Protocol):
    name: str
    async def decide(self, ctx: DecisionContext) -> DecisionBatch: ...

class RuleBasedDecisionMaker:   # name = "rules"; __init__(settings: BotSettings | None = None)
    # Highest-score candidate above settings.llm_min_candidate_score -> BUY with the
    # candidate's stop/target, LIMIT at candidate price; CLOSE positions whose setup has
    # invalidated (e.g. close below VWAP for vwap/orb setups) - conservative, deterministic.
```
`DecisionBatch.candidates` must be set to `ctx.candidates`.

### 5.5 `trader/strategy/claude.py` + `prompts.py`
`ClaudeDecisionMaker(api_key, model, effort, fallback: DecisionMaker)`; `name = "claude"`.
- Uses `anthropic.AsyncAnthropic`. Default model `claude-opus-5-5`, effort from settings
  (`output_config={"effort": ...}`); never send temperature/top_p/top_k or a thinking
  budget (400 on this model). Thinking stays default (adaptive).
- Failure policy: transient API errors (connection/timeout, 408/409/429, 5xx) return the
  fallback maker's CLOSE decisions only, with `error` set; refusals, other 4xx errors and
  parse failures return no decisions. `DecisionBatch.model` is the model that served the
  response (it can be a server-side fallback model).
- Structured output: `client.beta.messages.parse(..., output_format=<pydantic model>,
  betas=["server-side-fallback-2026-07-01"], fallbacks="default")` - verified present in
  anthropic 2.1.0. Validate the parsed result again and convert to `AIDecision`s.
- Handle `stop_reason == "refusal"` (→ `refused=True`, no decisions), `max_tokens`
  (→ error, no decisions), API errors with the typed exception chain (→ error, no
  decisions). On any failure the batch has **no BUY decisions** (fail safe), but the
  engine still manages exits via stops.
- Prompt caching: stable system prompt with `cache_control: {"type": "ephemeral", "ttl":
  "1h"}` (no beta header; cycles are often > 5 min apart, so a 5-minute entry would expire);
  volatile context in the user message. Never put timestamps in the system prompt.
- Cost: compute from `usage` (input, output, cache read, cache write) with a per-model
  price table (USD per 1M tokens): opus-5-5 4/20 (cache read 0.20, 5-min write 5.0, 1-h
  write 8.0), sonnet-5-5 2/10 (0.20, 2.5, 4.0), haiku-5-5 0.10/0.50 (0.01, 0.125, 0.20);
  1-hour writes cost 2x base input. `usage.cache_creation` splits writes into
  `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`; writes it does not explain
  are charged at the 1-hour price. Unknown model → opus prices. `max_tokens` 16000
  (non-streaming parse).
- The output model (`ModelDecision(s)`, aliased `ClaudeDecision(s)`), `convert_decisions`
  and the failure helpers live in `strategy/common.py` and are shared with Grok.
- The system prompt makes Claude a disciplined intraday risk manager: may only BUY
  symbols present in candidates (or CLOSE/HOLD existing positions), must provide
  stop below entry, prefers HOLD when edge is unclear, never sizes up to catch a
  daily target, explains rationale in <= 2 sentences, respects delayed-data warnings.
  The engine re-validates all of this anyway.

### 5.6 `trader/risk.py`
```python
class RiskManager:
    def __init__(self, limits: RiskLimits): ...   # clamps to HARD_CAPS
    def set_limits(self, limits: RiskLimits) -> None   # after a settings change (clamped)
    def effective_limits(self) -> RiskLimits
    def size_entry(self, *, decision: AIDecision, entry: float, stop: float,
                   account: AccountSnapshot, positions: list[Position], day: DayStats,
                   broker: Broker, allow_fractional: bool) -> tuple[float, list[str]]
    def check(self, req: OrderRequest, *, quote: Quote, account: AccountSnapshot,
              positions: list[Position], open_orders: list[OrderRecord], day: DayStats,
              mode: Mode, data_delayed: bool, broker: Broker,
              now: datetime, settings: BotSettings,
              last_halt_at: datetime | None = None) -> RiskVerdict
    def halt_reason(self, *, account: AccountSnapshot, day: DayStats) -> str | None
```
Sizing: `risk_usd = start_equity * max_risk_per_trade_pct/100 * size_fraction * streak`
(streak = 0.5 when `day.loss_streak >= loss_streak_halve_after`), qty = min(risk_usd /
(entry - stop), max_position_pct·equity/entry, max_order_notional/entry, available cash
/ entry (settled_cash for cash-like accounts; buying_power capped at equity), remaining
exposure room / entry), rounded DOWN with `broker.round_qty`. Size never depends on the
daily target or on being behind.
Entry checks (any failure blocks): halted day; trades_today >= max; open positions >=
max; already holding the instrument; stop missing or >= entry; target (if given) <=
entry; reward:risk < min; price < min_price; limit price outside collar vs quote; order
notional > max; exposure after fill > max_total_exposure; dollar risk at the stop >
max_risk_per_trade_pct of start equity; fees (`estimate_commission` BUY at the entry + SELL
at the target) > max_fee_to_reward_pct of expected reward; stock entry outside entry window or market
closed; LIVE + delayed data for stocks; day_trades_remaining known and < 1 for stocks;
quote missing/stale (> 2 × monitor interval for live data); qty rounds to 0.
Exits (`is_exit`) are always allowed if they reduce an existing long and never exceed
the held qty (no accidental shorts).
Halt reasons: equity <= start_equity × (1 − max_daily_loss_pct/100) → "daily loss limit";
pnl >= target and stop_trading_at_target → "daily target reached" (a *soft* halt: no new
entries, existing positions keep their stops; resets next day).

### 5.7 `trader/engine.py`
```python
class Engine:
    def __init__(self, config: AppConfig, db: Database, broker: Broker,
                 decider: DecisionMaker, notifier: Notifier | None = None,
                 clock: Callable[[], datetime] = utcnow, *,
                 scanner: SignalScanner | None = None, risk: RiskManager | None = None,
                 makers: Mapping[str, DecisionMaker] | None = None): ...  # 7.4
    async def start(self) -> None        # connect + monitor & decision loops (idempotent);
                                         # sets running and the engine_running flag
    async def start_monitor(self) -> None  # connect + monitor loop only (protect positions)
    async def stop(self) -> None         # stop the decision loop; the monitor keeps
                                         # protecting positions; clears engine_running
    async def shutdown(self) -> None     # stop both loops + disconnect (app shutdown);
                                         # keeps engine_running so a restart resumes
    async def kill(self, actor: str = "owner") -> None    # halt + cancel_all + flatten_all
    async def resume(self, actor: str = "owner") -> None  # clear halt for today
    async def run_cycle(self, manual: bool = False) -> DecisionBatch | None
    async def monitor_tick(self) -> None
    async def approve_order(self, order_id: int, actor: str = "owner") -> OrderRecord
    async def reject_order(self, order_id: int, actor: str = "owner") -> OrderRecord
        # both raise LookupError (unknown id) / ValueError (not pending); approve raises
        # BrokerError when fresh data is unavailable (the order stays pending)
    async def arm_live(self, actor: str = "owner") -> None    # caller already verified TOTP
    async def disarm_live(self, actor: str = "owner") -> None
    def reload_settings(self) -> None   # re-read BotSettings from db
    def status(self) -> EngineStatus
    def snapshot(self) -> dict          # cached {"account": AccountSnapshot | None,
                                         # "positions": [Position], "open_orders": [OrderRecord]}
                                         # (pydantic objects) refreshed by monitor_tick

def build_engine(config: AppConfig, db: Database) -> Engine
    # broker: SimBroker if config.broker == "sim" else IBKRBroker
    # makers: {"rules"} + "claude" (Anthropic key) + "grok" (xAI key) + "consensus" (both),
    #         Claude/Grok with the rules maker as their outage fallback; passed as
    #         Engine(..., makers=...). settings.decision_mode and settings.llm_enabled are
    #         read every cycle (no restart); fallbacks and warnings in 7.4
```
Loops (asyncio tasks, each wrapped so one exception logs + sets `last_error` and the
loop continues; exponential backoff on broker errors; never crash the app):
- **monitor loop** every `monitor_seconds`: ensure connected (reconnect with backoff;
  notify once if down > 60 s, and again on recovery), refresh account/positions/open
  orders cache, sync order statuses + fills into the journal (dedupe by exec_id), build
  `TradeRecord`s when a position closes (FIFO from fills; pnl after commissions; R
  multiple from positions_meta.initial_risk), update DayStats (start_equity at first
  snapshot of the day; wins/losses/loss_streak/fees/realized), record equity point at
  most every 5 min, evaluate `halt_reason` (halt + flatten per settings + notify),
  engine-side stop/target check for positions without native stops (and as a backup
  for stocks: only if price breaches stop by > 0.5% and no protective order is working;
  every engine-side exit and the daily halts need two consecutive ticks, see 7.4),
  re-place missing protective stops for stock positions after reconnect, and flatten
  stocks at close − 10 min when `flatten_stocks_before_close`.
- **decision loop** every `cycle_seconds`: skip when halted, disconnected, or nothing is
  tradable now (stocks closed and crypto disabled). Build universe (settings + optional
  scanner movers), fetch bars + quote per instrument (concurrency-limited, per-symbol
  errors tolerated), run Scanner, build `DecisionContext`. Call the LLM only if there is
  a candidate scoring >= `llm_min_candidate_score` or an open position the engine opened
  (positions_meta) to manage and
  today's LLM cost < budget; otherwise use the rule-based maker (and log why). Journal
  the batch (`insert_decision`), update `DayStats.llm_cost`. For each decision: BUY →
  (under the entry lock, on a fresh clock, quote, account, positions and working orders,
  never the pre-LLM context; dropped when older than `DECISION_MAX_AGE`, 7.5) build
  `OrderRequest` (LIMIT at decision limit or quote ask/last; stop/target from the
  decision or candidate) → size → risk check → BLOCKED (journal with reasons) or, if
  approved: shadow mode → BLOCKED("shadow mode") for entries; autopilot off → PENDING_APPROVAL
  (+ notify); else place via broker, journal, store positions_meta (stop, target,
  entry, initial_risk = (entry − stop) × qty, setup, decision_id), count the trade.
  CLOSE → `broker.close_position`, only for a position with positions_meta (otherwise
  journaled BLOCKED "not opened by the bot"). HOLD → nothing.
- `approve_order` re-runs the full risk check with fresh data before sending; pending
  approvals expire after 10 minutes (→ CANCELLED, "approval expired").
- All owner actions (start/stop/kill/resume/arm/disarm/approve/reject/settings) are
  written to the audit log by the web layer; engine-originated events (halts, flatten,
  reconnects) are audited with actor "engine".
- Live gating: `shadow = (broker.mode == LIVE) and not (config.allow_live and
  db.get_flag("live_armed"))`. `arm_live` raises `PermissionError` if `config.allow_live`
  is False or the broker is not LIVE. An engine constructed with `allow_live` False clears
  a stored `live_armed` (audit `live_disarmed`, actor "engine").

### 5.8 `trader/notify.py`
`Notifier(config)` with `async send(title, message, priority="default", tags=None)`; no-op
when `ntfy_url` is unset; never raises (log failures); 5 s timeout; rate limit duplicate
messages (same title within 60 s).

### 5.9 `trader/broker/sim.py`
`SimBroker(seed, starting_cash, clock=utcnow, commission_plan="tiered", *,
max_history_days=30)` (fees from `trader.fees`, see 7.1),
`mode = SIM`, always "connected" after `connect()`. Synthetic market: per-symbol
geometric random walk with intraday volatility regimes and volume, deterministic per
seed, with sensible base prices (SPY ~ 600, QQQ ~ 520, AAPL ~ 230, AMD ~ 160, F ~ 11,
SOFI ~ 15, PLTR ~ 40, INTC ~ 22, BTC ~ 65000, ETH ~ 3000; unknown symbols get a stable
hash-based price). `bars()` returns history consistent with `quote()`; time advances
with the injected clock (tests use a fake clock). Fills: market orders fill at ask/bid
plus slippage (bps scaled by inverse liquidity); limit orders fill when price crosses
(a limit placed between the mid and the far side of the spread fills only once the ask
reaches it for a BUY / the bid for a SELL, 7.5);
bracket stops/targets are simulated natively (`supports_native_stops` True for stocks,
False for crypto so the engine path is exercised); a SELL exit carrying `stop_loss` rests
as a standalone stop (`supports_standalone_stops` True for stocks). No shorting (sells
beyond held qty are rejected). Cash account semantics (no borrowing). Test hooks:
`advance(seconds)`, `set_price(instrument, price)`, `set_halted(symbol, halted, *,
gap_pct=None)` (entries rejected while halted; exits queue for the reopen, as at IBKR).
State is in memory: a new process trades a fresh simulated account (see 7.3).

### 5.10 `trader/broker/ibkr.py`
`IBKRBroker(config, ib: IB | None = None)` (ib injectable for tests) using `ib_async.IB` (asyncio; never call blocking variants; never
`util.startLoop`/`nest_asyncio` inside the server). Connect with `connectAsync(host,
port, clientId, timeout)`, `reqMarketDataType(config.ib_market_data_type)`. Mode from
account id ("DU…" → PAPER, else LIVE). Account values from `accountValues()` /
`accountSummaryAsync()` tags: NetLiquidation, TotalCashValue, SettledCash, BuyingPower,
AvailableFunds, RealizedPnL, UnrealizedPnL, DayTradesRemaining (−1 → None). Contracts:
stocks `Stock(symbol, "SMART", "USD")`, crypto `Crypto(symbol, config.ib_crypto_exchange, "USD")`
(default "ZEROHASH"; the owner's account routes BTC via ZEROHASH, conid 541686651); qualify
and cache. Bars via `reqHistoricalDataAsync` (useRTH False for crypto, True for
stocks... choose sensibly), converted to UTC `Bar`s. Quotes via snapshot/streaming
tickers with timeout; mark `delayed` when market data type is 3/4 (`data_delayed`), or
per instrument after error 10167; 354 means no data for that instrument (7.5). Stock entries:
bracket order (parent LMT/MKT, child STP at stop_loss, child LMT at take_profit,
OCA, transmit flags correct; child TIF GTC so protection survives disconnects and the
day boundary) via `bracketOrder` helpers when available. A stock SELL exit carrying
`stop_loss` is sent as a standalone GTC STP (re-placed protection); crypto has no stop
orders, so such a request is rejected, never sold at market. Crypto: IBKR crypto order
rules differ (research them: cashQty for market buys, allowed TIFs, whether STP is
supported) - implement what is supported, return REJECTED with a clear message
otherwise, and report `supports_native_stops(crypto)` truthfully. Fills via
`reqExecutionsAsync` + commission reports (only the latest revision of a corrected
execution); order status from `trades()`/`openTrades()` (an order completed while the app
was down is found through its executions' permId).
Event handlers: `disconnectedEvent`, `errorEvent` (log codes; 1100/1102 connectivity; 1101
forces a full reconnect before any data is trusted again),
`orderStatusEvent`. Estimate commissions via `trader.fees` with `config.commission_plan`. Optional
`async scan_movers(limit=10) -> list[str]` using `reqScannerDataAsync` (TOP_PERC_GAIN,
US major, price 2–50, volume > 1M) that returns [] on any error. All IB calls wrapped
with timeouts; raise `BrokerError` on connectivity failures.

### 5.11 `trader/security/*` and `trader/web/app.py`
Auth model: single owner. `python -m trader setup` creates the password (min 12 chars,
argon2id) and a TOTP secret (shows otpauth URI + terminal QR), writes `TRADER_SECRET_KEY`
to `.env` if missing (Fernet key; TOTP secret stored encrypted with it).
- Sessions: 32-byte random token in cookie `__Host-trader` (HttpOnly, Secure,
  SameSite=Strict, Path=/); store only SHA-256(token). If `cookie_secure` is False
  (local http dev), cookie name `trader_session`, Secure off. Idle timeout and absolute
  lifetime from config; purge expired on access. A GET with `X-Background: 1` (the
  dashboard's automatic status refresh) does not reset the idle timer. Logout deletes the
  row.
- Login: `POST /api/auth/login {password, totp}` - both required, constant-time,
  same generic error for any failure, per-IP limit (`login_max_attempts_per_ip` per
  15 min → 429) and global lockout (`login_lockout_failures` in 15 min → 429 for
  everyone), TOTP valid_window=1 with replay protection (`totp_last_step`), audit
  every attempt. On success rotate session (new token), return `{"ok": true, "csrf":
  "<token>"}`.
- CSRF: every POST/PUT/PATCH/DELETE under `/api` except login requires header
  `X-CSRF-Token` matching the session's csrf_token (constant-time) AND an `Origin`
  (or `Referer`) that equals the request's own origin or one of `public_origins`.
- Step-up: `/api/live/arm`, `/api/bot/resume`, and settings changes that loosen any
  risk limit (vs current; see `LOWER_IS_LOOSER`), enable autopilot while live, switch
  off `flatten_stocks_before_close`, or leave consensus decisions (another
  `decision_mode`, or `llm_enabled` off while in consensus) require a fresh `totp` field
  in the body (replay-protected).
- Middleware: TrustedHostMiddleware (`allowed_hosts`), security headers on every
  response: `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src
  'self'; img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src
  'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  no-referrer`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`,
  `Cross-Origin-Opener-Policy: same-origin`, HSTS when `cookie_secure`, and
  `Cache-Control: no-store` on `/api/*` and HTML. No OpenAPI docs routes
  (`docs_url=None, redoc_url=None, openapi_url=None`). Request body size limit 64 KB.
  Client IP = socket peer unless `trust_proxy_headers`.
- `create_app(config: AppConfig | None = None, db: Database | None = None,
  engine: Engine | None = None, start_engine: bool = True) -> FastAPI` - lifespan creates
  the db/engine if not given; the engine is started only if the persisted flag
  `engine_running` is True (so a restart resumes the previous on/off state); otherwise only
  its monitor runs (`start_monitor()`), which is the state Stop leaves behind, so open
  positions stay protected. The web layer sets the flag on /api/bot/start and /api/bot/stop.
  Tests pass `start_engine=False` and their own engine/db.

### 5.12 HTTP API contract (JSON; all `/api/*` except `/api/auth/login` and
`/api/session` require a valid session → 401 `{"detail": "..."}` otherwise)

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/healthz` | - | `{"ok": true}` (no auth, no details) |
| GET | `/` | - | index.html if authed, else 302 → `/login` |
| GET | `/login` | - | login.html |
| GET | `/static/*` | - | static assets |
| POST | `/api/auth/login` | `{"password", "totp"}` | `{"ok": true, "csrf"}` + cookie; 401; 429 |
| POST | `/api/auth/logout` | - | `{"ok": true}` |
| GET | `/api/session` | - | `{"authenticated": bool, "csrf"?: str}` |
| GET | `/api/status` | - | `{"status": EngineStatus, "account": AccountSnapshot\|null, "positions": [Position], "open_orders": [OrderRecord], "pending": [OrderRecord], "today": DayStats\|null}` |
| GET | `/api/decisions?limit=20` | - | `[DecisionBatch]` (limit ≤ 200) |
| GET | `/api/orders?limit=50` | - | `[OrderRecord]` |
| GET | `/api/trades?limit=50` | - | `[TradeRecord]` |
| GET | `/api/equity?days=30` | - | `[{"ts", "equity"}]` |
| GET | `/api/days?limit=30` | - | `[DayStats]` |
| GET | `/api/audit?limit=50` | - | `[{"id","ts","actor","action","detail","ip"}]` |
| GET | `/api/settings` | - | `{"settings": BotSettings, "hard_caps": {...}, "llm_available": bool (any AI key), "ai_keys": {"claude": bool, "grok": bool}, "grok_model": str, "live_allowed": bool}` |
| PUT | `/api/settings` | `{"settings": BotSettings, "totp"?: str}` | same as GET; 403 `{"detail":"step_up_required"}` when TOTP needed/invalid; 422 invalid |
| POST | `/api/bot/start` | - | `EngineStatus` |
| POST | `/api/bot/stop` | - | `EngineStatus` |
| POST | `/api/bot/kill` | - | `EngineStatus` |
| POST | `/api/bot/resume` | `{"totp"}` | `EngineStatus`; 403 step_up_required |
| POST | `/api/bot/cycle` | - | `{"ok": true}` (runs one decision cycle in background) |
| POST | `/api/live/arm` | `{"totp"}` | `EngineStatus`; 403 if not allowed / bad TOTP |
| POST | `/api/live/disarm` | - | `EngineStatus` |
| POST | `/api/orders/{id}/approve` | - | `OrderRecord`; 409 if not pending |
| POST | `/api/orders/{id}/reject` | - | `OrderRecord`; 409 if not pending |
| GET | `/api/signals?limit=100&setup=` | - | `[SignalRecord]` (limit ≤ 500) |
| GET | `/api/signals/stats?days=30` | - | `[SetupStats]` |

Datetimes serialize as ISO-8601 strings (pydantic `model_dump(mode="json")`).
Errors: rate-limited login/step-up → 429 `{"detail": "too many failed attempts"}`; PUT
/api/settings → 422 with a string detail when a risk value exceeds HARD_CAPS, the entry
window is invalid or a universe has an invalid symbol / more than 50 symbols; engine
failures map PermissionError → 403, ValueError/LookupError → 409, anything else → 503.
`/static/sw.js` carries `Service-Worker-Allowed: /`.

### 5.13 Frontend (`trader/web/static/`)
Mobile-first, dark/light via `prefers-color-scheme`, works at 360 px width and on a
laptop. No external requests. Files: `login.html` + `login.js`, `index.html` + `app.js`,
`styles.css`, `manifest.webmanifest` (name "AI Trader", display standalone, icons),
`sw.js` (cache the static shell only; never cache `/api/*`; network-first for HTML),
`icon.svg` (+ PNG icons generated if feasible).
Views (bottom tab bar on phones, top tabs on desktop):
- **Dashboard**: mode badge (SIM / PAPER / LIVE-SHADOW / LIVE-ARMED, color coded),
  connection + market-open + delayed-data indicators, equity, today's P&L vs target
  (progress bar that also shows the loss limit), trades today / max, LLM cost today /
  budget, open positions (qty, avg, last, unrealized P&L, stop, target), pending
  approvals with Approve / Reject buttons, halt banner with reason, big buttons:
  Start/Stop bot, Run cycle now, **KILL** (confirm dialog: "Cancel all orders and close
  every position?"), Resume (asks for a 2FA code).
- **Activity**: AI decision log (time, maker/model, market view, each decision with
  action/symbol/conviction/rationale, cost, refused/errors), orders (status chips,
  block reasons), closed trades (P&L, R), equity chart (inline SVG line chart drawn in
  JS, last 30 days), daily stats table.
- **Settings**: form for BotSettings incl. nested risk limits, showing hard caps;
  universe editors (comma-separated); save → if the server answers 403
  step_up_required, prompt for a 2FA code and retry; Live trading card: shows whether
  live is allowed by the server, Arm (2FA) / Disarm; audit log list; Logout.
Behavior: poll `/api/status` every 5 s while the page is visible (pause when hidden);
show a stale-data warning if polling fails; all mutating fetches send `X-CSRF-Token`
(from `/api/session`) and handle 401 by redirecting to `/login`. Escape all text
(build DOM with `textContent`, never `innerHTML` with data). Accessible: labels,
focus states, 44 px tap targets, `aria-live` for P&L/halt changes.

### 5.14 Deployment
- `Dockerfile`: python:3.13-slim, non-root user, `pip install --no-cache-dir -r
  requirements.txt`, copy `trader/`, `HEALTHCHECK` hitting `/healthz`, `CMD ["python",
  "-m", "trader", "serve"]`, data volume `/data` (`TRADER_DATA_DIR=/data`).
- `docker-compose.yml`: services `ib-gateway` (ghcr.io/gnzsnz/ib-gateway image with
  IBC; env from `.env`: TWS_USERID, TWS_PASSWORD, TRADING_MODE=paper|live,
  READ_ONLY_API=no, TWOFA_TIMEOUT_ACTION=restart, auto-restart time; **no host ports
  published** except optionally VNC on 127.0.0.1) and `trader` (builds the Dockerfile,
  `ports: ["127.0.0.1:8000:8000"]`, `TRADER_HOST=0.0.0.0` inside the container,
  `TRADER_IB_HOST=ib-gateway`, port per the image's docs for container-to-container
  access, volume `./data:/data`, `restart: unless-stopped`).
- Private access via **Tailscale** on the host: `tailscale serve --bg 8000` gives
  `https://<machine>.<tailnet>.ts.net` reachable only from the owner's devices; set
  `TRADER_ALLOWED_HOSTS`/`TRADER_PUBLIC_ORIGINS` accordingly. Never port-forward or
  expose IB Gateway.
- `.env.example` documents every variable with safe defaults.

## 6. Testing requirements
`pytest` must pass offline (no IBKR, no Anthropic). Cover at least: indicators; scanner
setups on crafted bars; risk manager (every block reason, sizing math, hard caps,
no-martingale property: sizing identical whether behind or ahead of target); engine
end-to-end on SimBroker with a fake decision maker (entry → stop hit → trade journaled
→ day stats; daily loss halt + flatten; kill switch; shadow mode blocks; approval flow
and expiry; LLM budget fallback); Claude maker with a mocked client (parse success,
refusal, API error → no BUYs, cost math); IBKR broker with a mocked `IB` object (account
parsing incl. DayTradesRemaining, bracket construction, mode detection); auth (hashing,
TOTP + replay, lockout, session expiry, CSRF + Origin checks, step-up); web API (login
flow, 401s, headers present, settings step-up, approve/reject).
`ruff check .` and `ruff format --check .` must pass.

## 7. Round 2 additions (owner facts, Reddit lessons, deployment)

Owner facts (from the live account, Oct 2026): IBKR **Pro, Tiered** pricing (Lite has no
API access, so Pro is required), "No Borrow Margin" account, $510 equity, all trading
permissions enabled (the app must still only ever build plain stock/ETF and crypto
contracts). IBKR crypto routes via **ZEROHASH**. SPY trades ~$764 and BTC ~$81.7k, so many
stocks are unaffordable as whole shares with a ~$255 position cap. Access will be via
**Cloudflare Tunnel + Cloudflare Access at `desk.kaxo.dev`** on a DigitalOcean droplet
(Ubuntu 24.04, Docker, user `trader`, ufw with only SSH open).

Lessons from a second r/ai_trading post (a builder whose bots were "trash" on paper for
7 months; two lucky manual halt trades made the money): write the rule down and **log every
qualifying signal**, including the ones you skip, then score them for a month; halts can
reopen −30–40%, never chase a halt; mark P&L at the **bid**; refuse spreads > 0.3%; wait
for the open to settle; time stops; size small.

### 7.1 Shared contract changes (implemented)
- `trader/fees.py`: `estimate_commission(instrument, qty, price, side, plan)` - IBKR Pro
  Tiered (default) / Fixed for stocks incl. sell-side regulatory fees; crypto 0.18% min
  $1.75 capped at 1% of value. Brokers' `estimate_commission` must delegate to it with
  `config.commission_plan`.
- `AppConfig`: `ib_crypto_exchange` (ZEROHASH), `commission_plan` (tiered|fixed),
  `cf_access_team_domain`, `cf_access_aud`, `cf_access_enabled`; `trust_proxy_headers`
  now means: client IP = `CF-Connecting-IP` if present, else first `X-Forwarded-For`.
- `Quote`: `bid_size`, `ask_size`, `halted`, `spread_pct`, `exit_price` (bid, else price).
- `RiskLimits`: `max_spread_pct` (0.3), `halt_cooldown_minutes` (15),
  `allow_leveraged_etfs` (False). `HARD_CAPS["max_spread_pct"] = 2.0`,
  `HARD_CAPS["loss_streak_halve_after"] = 10`. `LEVERAGED_ETFS`
  blocklist. Loosening rules: `LOWER_IS_LOOSER`, `BOOL_TRUE_IS_LOOSER`,
  `BOOL_FALSE_IS_LOOSER` (web step-up uses these; every other numeric risk field: higher
  is looser).
- `ExitRules` (in `BotSettings.exits`): `time_stop_minutes` (45), `time_stop_min_r` (0.5),
  `breakeven_at_r` (1.0), `trail_atr_mult` (0 = off).
- `BotSettings.stock_universe` default: liquid lower-priced names.
- `SignalRecord`, `SetupStats` models; `Database.insert_signal/update_signal/get_signal/
  list_signals/unevaluated_signals/setup_stats`.
- `Broker.modify_stop(instrument, new_stop) -> bool` (default False) and
  `Broker.scan_movers(limit) -> list[str]` (default []).

### 7.2 Behavior required from the remaining modules
- **Risk manager** adds entry blocks: symbol in `LEVERAGED_ETFS` (unless allowed); quote
  `halted`; within `halt_cooldown_minutes` after a halt was last seen for the symbol (engine
  passes `last_halt_at`); `quote.spread_pct` missing (for stocks) or > `max_spread_pct`;
  fee check uses `broker.estimate_commission` for BOTH legs (buy at entry, sell at target).
- **Engine**:
  - Prefilter the universe before scanning/LLM: skip symbols whose 1 unit (stocks: 1
    share unless fractional) costs more than the max position size or available cash;
    skip leveraged ETFs unless allowed. Log skipped symbols once per day, not every cycle.
  - Track `last_halt_at` per symbol from quotes (`halted=True`).
  - Mark positions at `quote.exit_price` (bid) for unrealized P&L, stops, targets, R.
  - **Signal ledger**: every scanner candidate that passes the scanner's own filters is
    written with `insert_signal` (taken False). When it becomes an order, set `taken=True`,
    `order_id`, `decision_id`; otherwise `skip_reason` (e.g. "AI: HOLD", "risk: spread
    0.8% > 0.3%", "LLM budget", "not top candidate"). Evaluate in the monitor loop (or a
    slower ledger loop every ~5 min): for signals at least 60 min old (stocks: or after
    the close), fetch bars since the signal and fill `r_15m/r_30m/r_60m/r_close/mfe_r/
    mae_r/first_hit` (stop checked first when both are hit in one bar), then
    `evaluated=True`. Bound the work per tick; tolerate data errors.
  - **Exits** (`ExitRules`): time stop → `close_position` with reason "time stop"; at
    `breakeven_at_r` move stop to entry (`broker.modify_stop`; if False, update
    positions_meta and manage it engine-side); trailing per `trail_atr_mult`. Never move
    a stop down.
  - Never add to an existing position; never enter a symbol that halted within the
    cooldown.
- **Brokers**: IBKR crypto exchange from config; `estimate_commission` via `trader.fees`
  with `config.commission_plan`; `modify_stop` for stocks modifies the working GTC STP
  child (same orderId, new auxPrice) and returns True on acceptance; quotes fill
  `bid_size/ask_size/halted` (ib_async Ticker `halted` > 0 means halted). SimBroker: same
  fee module, base prices refreshed (SPY ~764, QQQ ~690, AAPL ~265, BTC ~81,700, ETH
  ~3,900 are fine approximations), `set_halted(symbol, bool)` for tests, `modify_stop`
  for its native brackets.
- **Web**: `/api/signals` and `/api/signals/stats` (table above). Cloudflare Access
  middleware when `cf_access_enabled` (both values set; exactly one set is a configuration
  error, the app refuses to start; the state is logged at startup, 7.5): require header
  `Cf-Access-Jwt-Assertion` on every
  request except `/healthz`; verify RS256 signature against the team's JWKS
  (`https://<team_domain>/cdn-cgi/access/certs`, fetched with httpx, cached 1 h, refetched
  once on unknown `kid`), `aud` contains `cf_access_aud`, `iss == https://<team_domain>`,
  `exp`/`nbf` with 60 s leeway; reject with 403 otherwise. Implement with `cryptography`
  (no new dependencies). Tests use a locally generated RSA key and a stubbed JWKS fetch.
- **Frontend**: a **Signals** view (tab) showing `/api/signals/stats` per setup (signals,
  taken, evaluated, target-first %, avg R at close, MFE/MAE) with a clear "N < 30: too
  few to judge" badge, plus the latest signals list (time, symbol, setup, score, taken /
  skip reason, outcome). Dashboard P&L copy: "valued at the bid".
- **Deploy/docs**: Cloudflare Tunnel is the primary access path: a `cloudflared` service
  in docker-compose (`cloudflare/cloudflared` image pinned to a version tag, `tunnel
  --no-autoupdate run`, `TUNNEL_TOKEN` from `.env`), public hostname `desk.kaxo.dev` →
  `http://trader:8000` (plain http inside the docker network `edge`; ib-gateway is on a
  separate network `broker` that only the trader joins, 7.5; the owner already created
  the tunnel `desk`, the Access app and policy). `.env.example`:
  `TRADER_ALLOWED_HOSTS=desk.kaxo.dev,localhost,127.0.0.1`,
  `TRADER_PUBLIC_ORIGINS=https://desk.kaxo.dev`, `TRADER_TRUST_PROXY_HEADERS=true`,
  `TRADER_CF_ACCESS_TEAM_DOMAIN=`, `TRADER_CF_ACCESS_AUD=` (owner fills in; never commit
  real values), `TRADER_IB_HOST=ib-gateway`, `TRADER_IB_PORT=4004` (paper; 4003 live) per
  the gnzsnz image's socat relay. Tailscale stays documented as an alternative.
  DigitalOcean hardening notes: SSH drop-in must be named `00-hardening.conf` because
  DigitalOcean's `50-cloud-init.conf` sets `PasswordAuthentication yes` and sshd uses the
  first value; verify with `sudo sshd -T`. Docker publishes ports past ufw, so never
  publish anything except `127.0.0.1:8000`.

### 7.3 Integration notes (round 3)
- **Broker contract**: `estimate_commission(instrument, qty, price, side=Side.BUY)` (the risk
  manager prices the target leg as a SELL, so regulatory fees count);
  `supports_standalone_stops(instrument) -> bool` (default False): True means `place_order`
  rests a stop for a SELL exit carrying `stop_loss` - the engine only re-places missing stops
  on such brokers; `place_order` may return PARTIALLY_FILLED; the default `close_position`
  (new keyword `restore_stop`) and `flatten_all` re-place the cancelled protective stop when
  the broker rejects the exit, and the engine reports a refused kill/halt flatten as a
  failure (urgent notification, `last_error` "kill switch failed: ...").
- **Config**: `ib_market_data_type` is an int 1-4 (a `Literal` could not be set from the
  environment).
- **BotSettings.trade_crypto** defaults to False: at $100-250 per trade the crypto fee fails
  the fee-vs-reward check almost always, while 24/7 candidates would still cost LLM calls.
- **Step-up** also covers switching off `flatten_stocks_before_close` (holding stocks
  overnight).
- **EngineStatus.loss_limit_usd**: how far today's P&L may fall before the daily loss halt,
  as a positive amount (after a resume halts are measured from the resume equity, so it can
  differ from start_equity x max_daily_loss_pct). The dashboard meter uses it.
- **Engine extras**: unfilled entry orders are cancelled 10 minutes after they were sent (for an
  approved order: after the approval, kv `order_sent:<id>`); the kill-switch halt
  carries over to the next day (the daily-loss halt does not); kill also acts in shadow mode
  (owner-confirmed); exit rules and flatten-before-close only touch positions with
  positions_meta (kill and the daily-loss flatten close everything); AI CLOSE decisions wait
  for approval when autopilot is off; extra per-position data lives in kv
  `position_extra:<key>` and the resume baseline in kv `halt_baseline:<day>`.
- **Simulator restarts**: SimBroker keeps its account in memory, so every process trades a
  fresh simulated account. The engine starts a journal epoch at construction (SIM mode only):
  fills from before it are never matched against the new account, positions_meta of
  positions that no longer exist is dropped, journal orders the new simulator does not know
  are cancelled ("simulator restarted"), and today's P&L baseline restarts from the new
  account's equity (a stale *target* halt is cleared; loss and kill halts stay).
- **Tests**: `tests/test_e2e.py` runs the real SimBroker, rule-based maker, Engine and web
  app together through the HTTP API on a fake clock (entry → stop/target → trades, days,
  signal ledger and stats; kill switch; approvals and settings step-up; shadow mode;
  Cloudflare Access).

### 7.4 Round 4: Grok consensus and hardening

Owner decision (2026-10-23): **a BUY goes out only when Claude AND Grok both agree.**

- **Config**: `xai_api_key` (`SecretStr | None`; env `TRADER_XAI_API_KEY` or `XAI_API_KEY`),
  `xai_model` ("grok-4.7"), `xai_base_url` ("https://api.x.ai/v1"), `xai_price_input` /
  `xai_price_cached_input` / `xai_price_output` (2.0 / 0.5 / 6.0 USD per 1M tokens:
  third-party figures for grok-4.7, not confirmed by xAI; overridable).
- **Models**: `BotSettings.decision_mode: Literal["consensus", "claude", "grok", "rules"] =
  "consensus"`; `DecisionBatch.votes: dict[str, list[AIDecision]]` (each model's own
  validated decisions in a consensus batch; db column `decisions.votes`, added to older
  databases at startup); `EngineStatus.decision_maker` (who decides now),
  `decision_warning` (why that differs from the configured mode), `data_warning` (broker
  market-data problem, e.g. IBKR 10197).
- **`strategy/grok.py`** `GrokDecisionMaker(api_key, model, fallback, *, base_url, price,
  client=None, sleep=asyncio.sleep)`, `name = "grok"`: plain `httpx.AsyncClient` (90 s
  timeout, 10 s connect), xAI's **Responses API**: `POST {base_url}/responses` with
  `input` = [system: `prompts.SYSTEM_PROMPT`, user: `render_context(ctx)`], `text.format =
  {"type": "json_schema", "name": "trading_decisions", "schema": strict_json_schema(
  ModelDecisions), "strict": true}`, `max_output_tokens` 16000, `store: false`. The wire
  format is isolated in `build_request` / `read_response` (switching to
  `/chat/completions` + `response_format` touches only those). Answer text = the
  `output_text` parts of `message` items; a `refusal` part or `incomplete_details.reason ==
  "content_filter"` → `refused`; any other incomplete/failed status, no text, or text that
  fails `ModelDecisions` validation → error, no decisions; then `convert_decisions` (same
  validation as Claude). Retries: 408/409/429/5xx and connection errors, at most 2 retries,
  `Retry-After` honoured (capped 20 s), else 1 s / 2 s; timeouts are not retried. Remaining
  transient failures → the fallback maker's CLOSEs only; other 4xx → error, no decisions.
  Cost from `usage` (Responses or Chat field names): cached prompt tokens at the cached
  price; billed output = `total_tokens - input_tokens` when `total_tokens` is reported
  (covers reasoning tokens either way), else output + reasoning; a 2xx answer without usable
  usage is charged a worst-case estimate. The key travels only in the `Authorization` header,
  is held as a `SecretStr`, and is scrubbed from error text.
- **`strategy/consensus.py`** `ConsensusDecisionMaker(primary, secondary)`, `name =
  "consensus"`: both `decide()` concurrently (an exception becomes a failed batch). BUY only
  when both BUY the same instrument, with the primary's (Claude's) order type, limit, stop,
  target and setup, `conviction` and `size_fraction` = the smaller of the two. CLOSE when
  either says CLOSE. Every other instrument either side voted on becomes a HOLD whose
  rationale starts with `"consensus: "` and names the blocker ("Grok said HOLD", "Claude
  did not BUY", "Grok failed", "Grok refused", "both said HOLD"). A side that raised,
  refused, or returned an error other than "dropped N invalid decision(s)" blocks every BUY;
  the other side's CLOSEs still count. `cost_usd`/tokens = sums, `refused` = either,
  `error` = "Claude: ...; Grok: ...", `model` = "claude-...+grok-...", `market_view` =
  "Claude: ... | Grok: ...", `votes` = both decision lists.
- **Engine**: `Engine(..., makers: Mapping[str, DecisionMaker] | None = None)`; `decider`
  is a property resolving `settings.decision_mode` every call (without `makers`, the
  injected decider serves every AI mode). Fallbacks: consensus without one key → the model
  that has a key; without both → rules; a single-model mode without its key → rules; each
  with a `decision_warning`. `llm_enabled` off → rules, no warning. `reload_settings`
  updates Claude model/effort on every Claude maker, including the one inside consensus.
  The daily LLM budget counts `batch.cost_usd`, i.e. the combined cost. When the rules
  stand in for consensus (budget spent, nothing worth an AI call) their BUYs are dropped
  ("consensus mode: rules manage exits only"; ledger: "consensus mode: no entries without
  both models"). Consensus HOLD rationales become the ledger `skip_reason`.
- **Quote sanity** (`risk.quote_problem`, `risk.sane_exit_price`): a quote is implausible when
  crossed (bid > ask) or when its bid deviates more than 5% (stocks) / 8% (crypto) from last
  (or mid without last); locked-and-far quotes are caught by the same test. Implausible
  quotes are not used to mark positions (the broker's mark is kept), not used for
  engine-side exits, and block entries in `RiskManager.check`.
- **Breach confirmation** (`BREACH_CONFIRM_TICKS = 2`): engine-side stops (incl. the backup
  stop and engine-managed raised stops), engine-side targets, time stops and both daily
  halts (with the loss-limit flatten) fire only when the condition holds on two consecutive
  monitor ticks that span at least `BREACH_MIN_SPACING` (0.5) x `monitor_seconds` (the tick
  run_cycle runs right after the loop's never counts as a second look); a tick without the
  condition (or without a usable quote) restarts the count. While a halt is pending, `run_cycle` skips and `approve_order` blocks entries.
  Broker-native stops are unaffected.
- **Account change guard**: kv `broker_account` remembers the account id the journal
  belongs to (first sighting is just recorded). On a change (sim → paper "DU…" → live
  "U…"): a new journal epoch (generalising the SIM-restart epoch: fills before it are not
  matched, today's P&L baseline restarts, a stale target halt is cleared), open journal
  orders → CANCELLED ("broker account changed: left with the previous account"), pending
  approvals cancelled, all positions_meta/position_extra dropped (the engine never manages
  positions it did not open on this account), live disarmed, audit `account_changed`
  (masked ids, e.g. "DU...456") + high-priority notification. `approve_order` cancels an
  order when the account changed since it was proposed.
- **IBKR error 10197** ("No market data during competing live session": paper and live
  share data and only one session gets it): the instrument (or, without a contract, every
  subscribed instrument) is flagged; `quote()` raises `MarketDataUnavailable` (a
  `BrokerError`) until a tick newer than the last 10197 arrives, re-subscribing every
  `TICKER_RETRY_AFTER` to detect recovery; `Broker.data_warning` (new, default None)
  names the symbols for 15 minutes; the engine mirrors it into `EngineStatus.data_warning`
  and `last_error`. A reconnect clears the flags.
- **Frontend**: Settings shows `decision_mode` as a select (help text, 2FA when leaving
  consensus) and a note about missing keys; the dashboard shows an AI chip ("AI: Claude +
  Grok", "AI: Claude only", ...), `decision_warning` and `data_warning`; the decision log
  shows each model's vote ("How each model voted") for consensus batches; a note says Run
  cycle now works while the bot is stopped. Service worker cache bumped to v3.
- **Tests**: consensus truth table (`tests/test_consensus.py`), Grok request/response
  parsing over `httpx.MockTransport` (`tests/test_grok.py`), engine modes, fallbacks,
  budget, quote sanity, breach confirmation, account changes and data warnings
  (`tests/test_engine_modes.py`), 10197 (`tests/test_ibkr.py`), 1-hour cache pricing
  (`tests/test_claude.py`), settings step-up (`tests/test_web.py`), votes/migration
  (`tests/test_db_votes.py`).

### 7.5 Round 5: review fixes (2026-10-23)

Contracts changed by the confirmed review findings (each has a regression test).

- **Live arm** (SEC-1/OPS-1): `Engine.__init__` clears `flag:live_armed` when
  `config.allow_live` is False (audit `live_disarmed`, reason "TRADER_ALLOW_LIVE=false at
  startup"), so re-opening the gate always needs a fresh Arm + TOTP. `python -m trader
  disarm` (new) and `logout-all` / `reset-2fa` / `reset-password` clear it too (audit
  `live_disarmed`, actor "cli"). An arm still survives a restart while the gate stays open.
- **Shadow mode** (OPS-2, then owner decision 2026-10-23): entries are blocked; exits for
  the engine's own positions keep running (see section 1). The Disarm confirmation says so.
- **Loosening rules** (SEC-2): `loss_streak_halve_after` is no longer in
  `LOWER_IS_LOOSER` (a higher N halves later or never: raising it needs step-up) and is
  capped at 10 (`HARD_CAPS`, also in the backtester).
- **BUY at send time** (ENG-1/ENG-2): `_handle_buy` re-reads `broker.quote/account/
  positions/open_orders` (merged with entries sent earlier in the batch) and the clock
  inside `_entry_lock`, tracks halts, re-prices a model-less entry at the fresh ask and
  runs `size_entry`/`check` with `now` = send time. It blocks while a halt is being
  confirmed and when positions_meta exists for the instrument (an entry in flight or a
  position still held). A BUY whose context is older than `DECISION_MAX_AGE` (3 min) is
  dropped ("decision too old"); the reviewers' 2 x `monitor_seconds` would drop most LLM
  answers, and the fresh re-check already covers the entry window, halts and the collar.
- **AI CLOSE** (ENG-3): refused for positions without positions_meta (journal BLOCKED "not
  opened by the bot"); such positions alone are not worth an LLM call; the prompt marks a
  position without a stop "not opened by the bot: never CLOSE". An owner-approved pending
  CLOSE is unaffected.
- **Re-protect** (ENG-4): `_reprotect` subtracts the unfilled quantity of working market
  SELL exits (e.g. a close queued for the open) and protects only the remainder; bracket
  take-profit limits do not count.
- **Breach spacing** (ENG-5): see 7.4 (`BREACH_MIN_SPACING`).
- **Entry TTL** (ENG-6): measured from when the order was sent (7.3).
- **IBKR 1101** (BRK-1): `connected` is False and data/order calls raise `BrokerError` until
  `connect()` has disconnected and reconnected (re-running ib_async's startup syncs);
  1102 needs no reconnect.
- **PDT lag** (BRK-2): `Engine._with_day_trades` lowers a reported
  `day_trades_remaining` by the bot's same-day stock round trips whose closing fill is
  younger than `PDT_REFRESH` (5 min), for the decision context, autopilot BUYs and
  approvals.
- **Simulator inside-spread limits** (BRK-3): `_Order.inside_spread` (see 5.9).
- **Delayed data** (BRK-4): `IBKRBroker.data_delayed` is True only for market data types
  3/4; 10167 marks that instrument's quotes `delayed` (no contract: every current
  subscription); 354 is logged as "no data" for the instrument (its quote stays empty).
- **Completed orders after a restart** (BRK-5): `_find_trade` recovers an order's permId from
  the executions ib_async loads at connect.
- **Execution corrections** (BRK-6): `trader.models.exec_revision(exec_id)` parses IBKR ids
  (`<base>.<revision>`); `fills_since` returns only the latest revision per base;
  `Database.correct_fill` replaces stored earlier revisions (returning the replaced fill so
  the engine moves the fee), and `insert_fill` ignores a revision older than a stored one.
  A trade journaled before the correction arrived is not recomputed.
- **Logging** (SEC-3): `cli.configure_logging` keeps the `httpx`/`httpcore` loggers at
  WARNING so request URLs (the secret ntfy topic) never reach the logs.
- **Compose networks** (SEC-4): `edge` (cloudflared, trader) and `broker` (trader,
  ib-gateway; not internal, the gateway must reach IBKR).
- **Idle sessions** (SEC-5): `Authenticator.get_session(token, touch=...)`;
  `require_session` passes `touch=False` for a GET carrying `X-Background: 1`, which the
  dashboard's status poll sends.
- **Cloudflare Access config** (SEC-6): `AppConfig` validation refuses exactly one of
  `cf_access_team_domain` / `cf_access_aud`; the CLI prints configuration errors without
  values; `create_app` logs "Cloudflare Access check: enabled (team ..., aud ...)" or
  "disabled".
- **Docs/UI** (OPS-3/4/5): `setup` in a container says to apply the key with
  `docker compose up -d` (restart keeps the old environment); consensus cost is ~1.5x
  (Opus) / ~2x (Sonnet) / ~20x (Haiku) of Claude alone; the AI budget counts both models
  and, once spent, consensus mode only manages exits. Service worker cache bumped to v4.


### 7.6 Round 6: owner deployment fixes (2026-10-23)
- **Equity curve restarts** with a new broker account (or a fresh simulator run):
  `Database.start_equity_history(ts)` stores kv `equity_since`; `list_equity` hides earlier
  points. Older journals start the curve at the last `account_changed` audit entry.
- **External cash flows** (deposits, withdrawals, paper resets): when the account has had no
  position, working order or fill for `CASH_FLOW_QUIET` (10 min), an equity change of at
  least $1 between monitor ticks is applied to `DayStats.start_equity` (and any resume
  baseline) instead of counting as P&L; audit `cash_flow`, notification "Balance change
  detected"; a change of 50%+ restarts the equity curve. Changes while a position is open or
  within 10 minutes of a fill still count (IBKR account values can lag a closed trade).
  **Untraded days**: a change the tick comparison never saw (made while the app was
  stopped, or before this check existed) is caught too: with no trades, wins, losses,
  realized P&L or fills since the trading day began, and the account flat for 10 minutes,
  `start_equity` (and any resume baseline) is set to the current equity (audit `cash_flow`
  with `source: "untraded day"`); the curve restarts after the newest jump of 50%+ between
  two consecutive points that day (gradual moves are never hidden), also backfilled at
  startup from the newest `cash_flow` audit. A halt stays until the owner resumes.
- **Loss limit after a resume**: `DecisionContext.loss_baseline` carries the resume baseline
  so the prompt's "left before the halt" matches the engine's halt check instead of showing
  $0 for the rest of the day.
- **Fees in the prompt**: each candidate line carries the round-trip fee estimate (same fee
  model and `DecisionContext.commission_plan` as the risk check) at the largest size the
  notional, position and risk limits allow, as a % of the reward at target with the
  `max_fee_to_reward_pct` limit. The system prompt no longer states a $1 minimum (that is
  the Fixed plan; Tiered is about $0.38 per small order).
- **IBKR password**: letters and digits only (a `$` was cut off on the way to the
  gateway); `.env.example` and DEPLOY show a length check that prints no secret.
