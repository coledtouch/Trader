# Strategy: how the bot decides, and how to judge it

This page describes exactly what the code does, with the default settings. Everything
marked *setting* can be changed in the app's **Settings** tab, within the hard caps listed
below. None of this is a promise of profit: the point of paper trading and the signal
ledger is to find out whether these setups have any edge after fees.

## The decision flow

Every **5 minutes** (`cycle_seconds`) while something is tradable:

```
 1. Universe        stocks: F SOFI INTC T PFE AAL SNAP RIVN NU   crypto: BTC ETH
      |             (+ IBKR's top % gainers if dynamic_universe is on; needs live data)
      |             + any position the bot holds, so it can be managed
      v
 2. Universe filter skip leveraged/inverse ETFs (TQQQ, SQQQ, ...) unless enabled
      v
 3. Market data     quote + 5-minute bars per symbol (errors skip that symbol)
      v
 4. Scanner         5 setups -> candidates with a score 0-1, a stop and a target
      v
 5. Entry eligibility + ledger
      |             size each scanner proposal at the largest permitted quantity;
      |             check cash, working orders, exposure, fees, session, setups and quotes;
      |             write EVERY emitted candidate and skip reason, including unaffordable ones
      v
 6. Decision maker  consensus (Claude AND Grok must both BUY), or one model, or the rules;
      |             AI only if an eligible candidate scores >= 0.5 or a bot-owned position
      |             needs review, and today's AI spend is under budget; otherwise plain rules
      v
 7. Risk manager    size and check again on fresh state before sending; any failure = no order
      v
 8. Mode gate       shadow mode -> entries BLOCKED (exits still go out) | autopilot off -> waits for approval
      v
 9. Broker          limit order; for stocks a bracket: stop-loss + target held at IBKR
```

Separately, every **15 seconds** (`monitor_seconds`) the monitor loop keeps the IBKR
connection alive, refreshes balances and positions (valued at the **bid**), applies the
exit rules, checks the daily halts, journals fills and closed trades, and scores old
signals in the ledger.

**Bad ticks.** A quote is ignored for valuing positions and for the bot's own exit checks
when it is crossed (bid above ask) or its bid is more than **5%** (stocks) / **8%**
(crypto) away from the last trade (or the mid when there is no last trade); the broker's
own mark is used instead, and entries on such a quote are refused. The bot's own stops,
targets and time stops, and the daily halts (with the loss-limit flatten), fire only when
the condition shows on **two consecutive monitor checks** at least half a monitor interval
apart (normally 15 seconds), so a single bad print cannot sell a position or flatten the
account; the extra check a decision cycle runs right after the monitor's own never counts
as the second look. While a halt condition is
waiting for its second check, no new entry goes out. Stop orders that live at IBKR are not
affected: IBKR triggers them on its own data.

**No market data (IBKR error 10197).** If you log in to the live account elsewhere while the
paper gateway runs, IBKR may give the shared market data to that session only. The bot then
treats the affected symbols as having no usable data (no entries) until fresh prices arrive,
and the dashboard says so ([DEPLOY.md](DEPLOY.md#no-market-data-error-10197)).

## 1-3. Universe and data

- **Stocks** (`stock_universe`): liquid, mostly lower-priced names, because with about
  $600 and a $250 order cap a share must cost well under $250 (whole shares only, unless
  `allow_fractional_stocks`). An unaffordable scanner proposal is logged with its sizing
  failure and excluded from paid entry analysis.
- **Crypto** (`crypto_universe`, `trade_crypto`): BTC and ETH, traded in fractions, 24/7.
  Off by default; see the fee section before turning it on.
- New stock entries only between **09:45 and 15:30 ET** (`stock_entry_start`,
  `stock_entry_cutoff`): the open is chaotic and thin names fade later in the day. On
  half days the window ends 20 minutes before the 13:00 close.

**Delayed data is diagnostic only for stock entries.** A delayed global feed or a delayed
per-symbol quote blocks stock entries in PAPER and LIVE (also SIM if a supplied quote is
flagged delayed). It also removes that candidate from paid entry analysis. Existing
positions keep their exit management; the bot may still pay for an exit review.

**Exception, paper only:** Settings > Risk limits > "Paper: trade on delayed prices"
(`risk.allow_delayed_paper_entries`, off by default, turning it on needs a 2FA code) lets
the bot enter on delayed data in PAPER mode, to exercise the order plumbing (brackets,
stop moves) before real-time data is subscribed. Decisions then use prices about 15
minutes old while paper fills use current ones, so those trades say nothing about an
edge: each is stored with `delayed_data` and left out of the per-setup results (they still
count in the account totals). LIVE ignores the switch and always blocks.

The quote-age table reports elapsed time since the broker/transport timestamp. It is
not a measurement of how far an exchange-delayed price is behind the market: a quote
received one second ago can still be a delayed quote. Fresh age and a usable two-sided
quote are necessary checks; the separate delayed flag must also be clear. Keep market
data type `3` for connection and signal diagnostics while subscription approval is
pending. Change to type `1` only after approval and paper-account sharing, then verify
actual delivered quotes are not flagged delayed.

## 4. The scanner setups

All setups are **long only**. Each candidate gets a stop below the price, based on the
ATR (average true range: how far the price typically moves per 5-minute bar), and a
target at **2x the risk** above the price (or `min_reward_risk` if that is higher).

`enabled_setups` controls which setups may open positions. All five below are enabled
by default. Disabling a setup keeps its emitted signals in the diagnostic ledger but
prevents paid entry analysis and new orders for it. Re-enabling one requires fresh 2FA.
The final send and approval checks use current settings, so disabling a setup also
invalidates a pending entry approval; existing positions retain their exits.

| Setup | When it fires | Stop | Score |
|---|---|---|---|
| `orb_breakout` (stocks only) | Price breaks the high of the first 15 minutes on relative volume above 1.5x, above VWAP, EMA9 above EMA21. Only fresh breaks (first close above it within the last 3 bars) and at most 1 ATR above the range high | 1.5 ATR | ~0.5-0.85 |
| `vwap_reclaim` | Price crosses back above VWAP after at least 2 of the previous 5 closes below it, with a rising EMA9, and is no more than 0.75 ATR above VWAP | 1.5 ATR | ~0.55-0.75 |
| `trend_pullback` | EMA9 above EMA21, both rising; the pullback touches the EMA21 and holds; the last bar closes up; RSI below 70 | lower of 1.5 ATR or 0.5 ATR under the EMA21 | ~0.6-0.75 |
| `rsi_reversion` | RSI(14) below 30 and turning up, at or below the lower Bollinger band, but not in a steep downtrend (within 2 ATR of the EMA21). Counter-trend, so scored lowest | lower of 1 ATR or 0.25 ATR under the recent low | ~0.35-0.5 |
| `volume_spike` | A rising bar on at least 3x the average volume of the previous 20 bars, closing above its 20- and 50-bar averages and above the session open (crypto: the 24-hour open), with a positive 20-day trend when known and a sane volatility. Stocks: not before 09:45 | 1.5 ATR | ~0.5-0.8 |

VWAP resets each New York session for stocks and is a rolling 24 hours for crypto.

Scanner filters (no candidates at all when any fails): at least 30 bars of history, recent
data (not older than 3 bars), price at least `min_price` ($1), stocks trading at least
`min_avg_volume` (500,000 shares a day on average), and a bid/ask spread no wider than 0.5%.

## 6. The decision makers

**Claude** (model `llm_model`, default `claude-opus-5-5`, effort `llm_effort`) and **Grok**
(xAI, `TRADER_XAI_MODEL`, default `grok-4.7`) get exactly the same input: the account,
today's results, the risk limits, open positions, the candidates, indicator snapshots and
recent trades, with the same instructions: protect capital first; only buy listed
candidates; every buy needs a stop below and a target above; prefer HOLD when the edge is
unclear; never size up to reach a daily target; explain each decision in at most two
sentences; respect delayed-data warnings. Each answer is checked again in code (the same
checks for both). If a model refuses, errors or times out, **it buys nothing** that cycle
(stops still protect open positions; during an API outage the rule-based exits still run).

**Decision mode** (`decision_mode` setting):

| Mode | Who decides | Notes |
|---|---|---|
| `consensus` (default) | Claude **and** Grok, asked at the same time | **BUY** only when both buy the **same** symbol; Claude's limit, stop and target are used; conviction and size are the **smaller** of the two (a lukewarm second opinion can only shrink the trade). **CLOSE** when **either** says close (an exit reduces risk). Anything else is HOLD, and the signal ledger says who stood in the way, e.g. `consensus: Grok said HOLD` or `consensus: Claude did not BUY`. If either model fails or refuses, **nothing is bought** that cycle (the other's closes still count). Both calls are paid for: Grok adds about $0.02-0.03 per call, i.e. **~1.5x** the AI cost with Opus, **~2x** with Sonnet and **~20x** with Haiku |
| `claude` / `grok` | That model alone | |
| `rules` | The rule-based maker below | No AI cost |

Consensus needs both API keys. With only one key the bot uses that model alone, and with
neither it uses the rules; the dashboard shows a yellow warning either way (it never
pretends a consensus that is not there). `llm_enabled` off means rules-only whatever the
mode. Leaving consensus (another mode, or `llm_enabled` off) needs a 2FA code.

What to expect from consensus: **fewer trades**. Two models that must agree filter out
setups where either is unsure, which may avoid some bad trades and will certainly skip some
good ones. Whether that helps after fees is an open question: published evidence that
any single LLM, Claude or Grok, picks intraday trades with a real edge is thin and mixed
(the Grok agent that inspired this app was a paper-trading experiment, not a track
record), and there is no evidence yet that two together do better. Treat consensus as a
risk filter, not an edge, and let the signal ledger decide: every skipped signal is
scored, so after a month you can compare what the models bought with what they let pass.

The AI is only asked when an eligible candidate scores at least
`llm_min_candidate_score` (0.5) or a position the bot opened needs review, and only while
today's AI spend (both models together) is below
`llm_daily_budget_usd`. Otherwise the **rules** decide: buy the highest-scoring candidate
at or above the threshold (limit order at the candidate price, with its stop and target),
and close a position whose setup broke (VWAP setups: price back below VWAP; trend
pullback: EMA9 below EMA21 and price below EMA21). **In consensus mode the rules never
open a position** (a BUY needs both models), so once the budget is spent the bot only
manages exits for the rest of the day. Costs:
[SETUP_IBKR.md](SETUP_IBKR.md#ai-api-cost-estimate).

## 7. The risk manager

### Position size

```
risk in dollars = start-of-day equity x max_risk_per_trade_pct x size_fraction (x 0.5 after 3 losses in a row)
quantity        = the SMALLEST of:
                    risk in dollars / (entry - stop)
                    max_position_pct of equity / entry
                    max_order_notional_usd / entry
                    uncommitted cash / entry        (working buys and commissions reserved)
                    remaining exposure room / entry (positions and working buys included)
                  rounded DOWN to whole shares (crypto: small fractions)
```

`size_fraction` (0-1) is the only size input the AI controls: it can shrink a trade, never
grow it. Nothing in this formula looks at the daily target or at how the day is going,
except the losing-streak rule, which can only make trades smaller.

Example with $600 equity: SOFI at $15.00 with a stop at $14.85 ($0.15 risk per share).
The 2% risk rule allows 80 shares, the 50% position rule 20, the $250 order cap 16. Result:
**16 shares ($240)**, risking $2.40 if the stop is hit. On a small account the order cap
is usually what binds, so most trades risk well under 2%.

### Limits and their hard caps

| Setting | Default | Hard cap | What it does |
|---|---|---|---|
| `daily_profit_target_pct` | 0 (off) | | Optional. With a positive percent and `stop_trading_at_target` on, reaching that gain stops new entries for the day (positions keep their stops). It never increases risk. A setup saved with its own dollar `daily_profit_target_usd` (and no percent) keeps that amount; the untouched old $100 default becomes off |
| `max_daily_loss_pct` | 6% | 25% | Equity down this much from the day's start: halt (and close everything if `flatten_on_halt`). Resume needs a 2FA code |
| `max_risk_per_trade_pct` | 2% | 10% | Loss if the stop is hit, as % of start-of-day equity |
| `max_position_pct` | 50% | 100% | Largest single position, % of equity |
| `max_total_exposure_pct` | 100% | 100% | All positions plus working buy orders: never more than the equity (no leverage) |
| `max_open_positions` | 2 | 10 | Positions plus working entry orders |
| `max_trades_per_day` | 10 | 50 | New entries per trading day |
| `max_order_notional_usd` | $250 | $5,000 | Largest single order |
| `min_price` | $1 | | No penny stocks |
| `price_collar_pct` | 1% | 5% | A limit price must be within this % of the last price |
| `min_reward_risk` | 1.5 | | (target - entry) / (entry - stop); scanner targets use 2.0 |
| `max_fee_to_reward_pct` | 25% | | Round-trip commission must be at most this % of the expected reward |
| `loss_streak_halve_after` | 3 | 10 | Half size after this many losing trades in a row (raising it loosens the rule and needs 2FA) |
| `max_spread_pct` | 0.3% | 2% | No entries when the bid/ask spread is wider |
| `halt_cooldown_minutes` | 15 | | No entries in a stock for this long after a trading halt |
| `allow_leveraged_etfs` | off | | Leveraged/inverse ETFs stay blocked unless turned on |
| `llm_daily_budget_usd` | $3 | $50 | AI spend per day (Claude + Grok together); after that, until tomorrow: consensus mode = exits only (no new entries), single-model mode = rules-only |

Loosening any of these in the app needs a fresh 2FA code; tightening does not.

### Every check before an entry (any failure blocks it)

- **Account:** buy side only; that asset class and scanner setup are enabled; trading not halted today; fewer
  than `max_trades_per_day` entries; not already holding the symbol and no entry order
  already working (the bot **never adds to a position**); open positions plus entries below
  `max_open_positions`; not a leveraged ETF; IBKR's day-trade counter (when reported)
  above 0 for stocks. IBKR updates that counter only every few minutes, so the bot first
  subtracts its own same-day round trips that closed in the last 5 minutes.
- **Market data:** a quote exists, is finite, is fresh (twice the monitor interval for
  non-delayed data), is not timestamped in the future, and is plausible (not crossed,
  bid within 5% / 8% of the last trade); the
  symbol is not halted and was not halted in the last 15 minutes;
  stocks need a two-sided quote with a spread of at most 0.3%; delayed data blocks stock
  entries in every mode regardless of receipt age; stocks only while the market is open and inside the entry
  window; not a symbol you closed by hand today (the Close button).
- **Prices:** a valid entry price; a stop below it; a target above it; reward:risk at
  least `min_reward_risk`; price at least `min_price`; a limit within the collar.
- **Size and money:** quantity above 0 after rounding; order at most
  `max_order_notional_usd`; dollar risk at the stop within `max_risk_per_trade_pct`;
  position within `max_position_pct`; total exposure including working orders within
  `max_total_exposure_pct`; enough available cash including commissions for working buys
  and the proposed purchase; unknown-priced working buys block new entries; round-trip
  fees at most `max_fee_to_reward_pct` of the expected reward.

Exits are always allowed when they reduce a long position, and never sell more than is
held (no accidental short).

## 8-9. Orders and exits

- **Entry:** a limit order at the decision's price (or the ask when the BUY is sent). The
  AI may think for a minute or more, so every BUY is sized and checked again on the clock,
  quote, account, positions and working orders **at the moment it is sent** (an entry that
  filled, an order you approved, a halt or the end of the entry window in the meantime all
  count); a BUY decided on data more than **3 minutes** old is dropped. Unfilled entries
  are cancelled **10 minutes** after they were sent. Autopilot is off by default, so the
  order waits for your approval; approving re-runs every risk check with fresh data, and
  unapproved orders expire after 10 minutes. Rejecting while that check is still loading
  prices cancels the order; it is not sent.
- **AI CLOSE:** only for positions the bot opened. A position you hold yourself (or one
  from before an account change) is never sold by an AI decision and does not trigger AI
  calls on its own.
- **Stocks:** sent as a bracket: the stop-loss and the target are real orders at IBKR,
  good-till-cancelled, so they keep working if the server or the connection is down.
- **Crypto:** IBKR only accepts limit and market crypto orders, so the bot checks the bid
  against the stop and target every 15 seconds and sells when one is hit. These stops only
  work while the bot runs.

Exit rules (`exits` settings; they only touch positions the bot opened):

| Rule | Default | What happens |
|---|---|---|
| Stop-loss / target | from the setup | Stocks: IBKR fills them. Crypto: the bot sells at the bid once the level is breached on two checks in a row |
| Backup stop | always | A stock without a working IBKR stop is sold if the bid falls more than 0.5% below its stop (for example after a reconnect, before protection is re-placed) |
| Re-protect | always | After a reconnect, missing stock stops are placed again |
| Time stop | 45 min, 0.5R | A trade that has not reached +0.5R after 45 minutes is closed: stale trades tie up a small account |
| Breakeven | at +1R | The stop moves up to the entry price (the IBKR stop is modified) |
| Trailing stop | off (`trail_atr_mult` 0) | If set, after breakeven the stop trails that many ATRs below the best price |
| Let winners run | off (`let_winners_run`) | Only with the trailing stop on: new entries are sent without the take-profit order, so a winner runs until the trailing stop (or the time stop, an AI close or the 15:50 flatten) sells it. The entry still needs a target for its reward:risk check |
| Setup broke | rules / Claude | e.g. a VWAP setup back below VWAP: close |
| Flatten before close | 15:50 ET (12:50 on half days) | Stock positions are closed 10 minutes before the bell (`flatten_stocks_before_close`) |
| Daily loss limit | -6% | Halt and close everything (`flatten_on_halt`) once two consecutive checks agree; resume needs 2FA |
| Daily target | off ($0) | If configured positive and reached, no new entries today; open positions keep their stops |
| KILL button | | Cancel every order and close every position |
| Close button | | Close one position at market from **Open positions** (after a confirm): its IBKR stop and target are cancelled first, the stop is put back if the sell is refused, and the bot buys no new shares of that symbol for the rest of the trading day. It works on any position you hold, in shadow mode and while halted; stocks only while the market is open. It is refused while a sell order you placed in TWS or IBKR Mobile works on that symbol (the bot cannot cancel it, and it could sell again after the close): cancel that order there first |

Stops only ever move **up**. P&L, stops and R multiples use the bid, because that is what
a sale would actually get.

**Trading halts.** A stop order cannot fill while a stock is halted (LULD pause or news
halt), and the stock can reopen far below the stop: halts often reopen 30-40% away. The
stop then fills at the first price after the reopen, so the loss can be several times the
planned 1R. The bot does not enter a halted stock, waits 15 minutes after a halt
(`halt_cooldown_minutes`), and never adds to a position; the small position size is the
only real protection against such a gap.

## The signal ledger

Every candidate the scanner produces is written down (at most once per symbol and setup
every 30 minutes), with:

- whether it was **taken** (it became an order) or **why not**: `AI: HOLD`,
  `consensus: Grok said HOLD`, `consensus: Claude did not BUY`, `consensus: Grok failed`,
  `rules: no entry`, `risk: spread 0.80% > 0.30%`, `LLM budget; ...`,
  `not top candidate`, `halt cooldown`, `outside the stock entry window`, `shadow mode`...
- its outcome, filled in after the session close (stocks) or 24 hours later (crypto) from
  1-minute bars, in **R** (1R = the distance from the signal price to its stop):
  - `r_15m`, `r_30m`, `r_60m`: where the price was 15, 30 and 60 minutes later;
  - `r_close`: at the session close (stocks) or 24 hours later (crypto);
  - `mfe_r` / `mae_r`: the best and worst point reached;
  - `first_hit`: `target`, `stop` or `neither` (if one bar touched both, the stop counts).

The outcomes are **gross price excursions before fees and slippage**, not filled-trade
returns. They do not reproduce the bot's breakeven/time/trailing exits, actual entry
fills or the cash constraints of a portfolio. The **Signals** tab labels them as
diagnostics; 30 evaluated signals are a first inspection threshold, not evidence of an
edge. Full eligibility reasons are also journaled with each decision cycle, including
cycles within the signal ledger's 30-minute deduplication window.

## Judging a setup

Freeze rules and costs before testing, keep a separate period for evaluation, and cover
different sessions and market conditions. Neither 30 nor 100 signals establishes an
edge: signals from the same symbol or day can be highly correlated, and trying many
variants makes a lucky winner easy to find.

1. Inspect gross excursions to understand setup behavior and data quality. Do not use
   average R at close or target-first rate as the return of the bot's actual exit rules.
2. Use filled round trips for setup net expectancy and P&L after commissions. The
   separate setup report groups trades by New York exit day and, after five observed
   days, provides an exploratory bootstrap interval by resampling whole days. It uses
   500 resamples; it is not a certification, a guarantee, or a correction for choosing
   the best setup after inspecting many alternatives.
3. Compare rules, single-model and consensus portfolios on the same observations and
   risk settings, including the model costs each arm actually incurred. Missing votes
   or costs keep the comparison incomplete. Vetoed signal excursions alone cannot show
   whether consensus improves executable portfolio returns.
4. Review all-in P&L and drawdown as well as trading P&L. Confirm paper results with
   realistic fills, commissions, data quality and exits on new periods before considering
   any live use. Synthetic backtests test mechanics only.

Use **Settings > Setup selection** to disable a weak setup. Change one thing at a time, retain a
record of the change, and evaluate it on subsequent observations rather than tuning
repeatedly to the same history. Re-enabling a setup requires 2FA.

## All-in performance

The dashboard and `GET /api/performance?days=30` report the current journal account and
account epoch, excluding previous simulator runs and account changes. Trading P&L uses
recorded daily equity changes, including open P&L and broker commissions, with the
engine's recorded cash-flow adjustments. Filled-trade expectancy also already includes
commissions; **do not subtract them twice**.

All-in P&L subtracts recorded AI token-price estimates plus hosting and market-data
estimates. Set `operating_costs.hosting_monthly_usd` and `data_monthly_usd` in Settings.
Both default to unknown (`null`); explicit zero means free. The current hosting
dashboard estimate is $24/month; data pricing is unknown pending subscription approval.
Until both values are supplied, all-in P&L remains unknown and only the result after
known costs is shown. Reconcile AI estimates with provider invoices.

Monthly estimates accrue over the recorded coverage period by elapsed calendar time
and the days in each month, including weekends. The current monthly settings apply to
that period; this is not a historical billing ledger. Reports warn when the first day
is partial. Drawdown is measured from daily equity observations; unobserved intraday
losses can be larger. Filled-trade setup statistics exclude external operating costs,
which are reported separately. No annualized return is inferred from a short sample.

## Offline paired replay

Use **Signals > Captured-decision comparison** or the authenticated API:

```text
POST /api/research/run  {"days": 30}
GET /api/research?days=30
```

POST uses the usual session, Origin and CSRF protection, returns `202` while running,
and refuses an overlapping run. The web report uses up to the latest 1,000 captured
frames from the latest uninterrupted account/settings cohort in the selected period.
It does not combine regimes separated by an account or settings change.

Command-line equivalents and a real-history rules test:

```bash
python -m trader research --database data/trader.db --save-report
python -m trader research --csv F=history.csv --settings frozen.json
```

The CSV uses timezone-aware `ts,open,high,low,close,volume` rows with bar-open timestamps;
`--bar-size` must match the data. `frozen.json` is a complete saved `BotSettings` object.
The CSV route uses the same production `Engine`, scanner, sizing, guards and exits;
it is rules-only because historical prices contain no recorded AI opinions. CSV quotes
infer a spread (`--spread-bps`, default 10) and execution slippage (`--slippage-bps`,
default 5), so results depend on those assumptions rather than actual historical quotes.

Captured-data replay runs independent rules, Claude, Grok and consensus portfolios with
the production engine and consensus logic. It uses only recorded model votes and
recorded per-model costs: **no broker connection, AI API calls or fabricated historical
votes**. Unknown model cost and incomplete comparisons remain `null`, never zero.
Replay all-in results subtract model costs; hosting/data are portfolio-wide estimates
in the primary performance report, not separate incremental charges to every arm.

Capture retains up to 2,500 compact scanner snapshots with the observed quotes,
original scanner candidates, indicator features and model votes/costs. Historical bars
are not repeated in each snapshot; these captures cannot be historically rescanned.
Use the CSV route when you need to recompute the scanner on historical bars.
These are sparse decision-cycle observations, not a complete 15-second monitor trace
or an order book. Crossings, stops, halts and losses between observations may be missed.
Cached AI opinions saw the original account and positions, not the diverging replay
portfolios. Reports label that conditioning, missing votes, unknown costs and open
positions; no forced final liquidation invents unobserved exit fills. Use the comparison
as limited research evidence, then validate changes prospectively with frozen settings.

## Why there is no "make it back" mode

A martingale doubles the bet after each loss so that one win recovers everything. It works
until the losing streak that every strategy eventually has, and then it empties the
account. A daily dollar target creates the same pressure in a quieter form: behind
by 2pm, size up, take worse setups.

So the code makes it impossible:

- size is a function of start-of-day equity, the stop distance and fixed caps, never of
  the target or of today's P&L (the test suite checks that sizing is identical whether the
  bot is ahead or behind);
- after `loss_streak_halve_after` losses in a row, size is **halved**;
- the AI can only scale size down;
- a 6% losing day halts trading and closes positions; reaching the target stops new
  entries instead of encouraging more.

## Fee math for a small account

IBKR Pro **Tiered** stock commissions: $0.0035 per share, minimum $0.35 per order, plus
exchange and clearing fees (about $0.0032 per share when taking liquidity), capped at 1%
of the trade value; sales also pay small regulatory fees. **Fixed**: $0.005 per share,
minimum $1.00. Crypto: 0.18% of the value, minimum $1.75, capped at 1% of the value.
These are the estimates in `trader/fees.py`; check
[IBKR stock commissions](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php)
and [IBKR crypto commissions](https://www.interactivebrokers.com/en/pricing/commissions-cryptocurrencies.php)
for current rates.

| Trade | Value | Round trip, Tiered | % of value | Round trip, Fixed |
|---|---|---|---|---|
| 20 shares at $11 | $220 | $0.84 | 0.38% | $2.01 |
| 10 shares at $25 | $250 | $0.77 | 0.31% | $2.01 |
| 100 shares at $2.50 | $250 | $1.36 | 0.55% | $2.02 |
| BTC | $100 | $2.00 | **2.0%** | |
| BTC | $250 | $3.50 | **1.4%** | |
| BTC | $500 | $3.50 | 0.7% | |

Stay on **Tiered**: at these sizes Fixed costs more than twice as much.

The fee check (`max_fee_to_reward_pct` 25%) compares the round trip with the expected
reward (target minus entry, times quantity), with $600 equity:

| Trade | Stop / target | Reward | Fees | Fees / reward | Result |
|---|---|---|---|---|---|
| 16 SOFI at $15.00 (5-min ATR $0.10) | $14.85 / $15.30 | $4.80 | $0.81 | 17% | allowed |
| 22 F at $11.00 (5-min ATR $0.04) | $10.94 / $11.12 | $2.64 | $0.85 | 32% | **blocked** |
| $250 of BTC (5-min ATR $250) | 1.5 ATR / 2R | $2.29 | $3.50 | 153% | **blocked** |
| $250 of BTC (5-min ATR $800) | 1.5 ATR / 2R | $7.34 | $3.50 | 48% | **blocked** |

What this means:

- With ~$250 trades, a stock setup needs a target roughly $3.40 or more away in total
  (about 1.4% of the position) to pass. Quiet, cheap stocks rarely qualify; stocks in the
  $10-40 range that move enough intraday do.
- Crypto at $100-250 per trade almost never passes: a 2R target would need a move of
  roughly 6-8%. That is why `trade_crypto` is off by default; leave it off until the
  account is much larger.
- Most signals end up blocked. That is the risk manager doing its job: a trade whose fees
  eat the profit is a guaranteed slow loss.
- Under the old pattern-day-trader rule (if IBKR still applies it to your account), stock
  trades are also limited to 3 per 5 business days; the bot stops at IBKR's counter
  ([SETUP_IBKR.md](SETUP_IBKR.md#rules-that-matter-for-a-small-account)).
