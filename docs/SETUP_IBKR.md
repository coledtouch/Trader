# Your setup checklist: IBKR, market data, Claude, Grok, alerts

This page picks up from where you are now. Tick the boxes as you go. Menu names at IBKR,
Anthropic and xAI change often; where a step says **verify on IBKR's site**, the idea is right
but the exact button may be named differently on your screen.

Once the boxes in "Still to do" are ticked, continue with [DEPLOY.md](DEPLOY.md). You can
deploy the simulator on the droplet before any of this is finished.

## Already done

- [x] IBKR **Pro** account, **Tiered** pricing, **No Borrow Margin**, all trading
  permissions including crypto. (Pro is required: IBKR Lite has no API access,
  [IBKR: Lite vs Pro](https://www.interactivebrokers.com/en/general/compare-lite-pro.php).)
- [x] $510 deposited (it may still be clearing: "equity with loan value" showed $10).
- [x] Domain `kaxo.dev` on Cloudflare.
- [x] Cloudflare Zero Trust Access application **desk** for `desk.kaxo.dev`, with an Allow
  policy for your e-mail address (one-time PIN).
- [x] Cloudflare Tunnel **desk** with the public hostname `desk.kaxo.dev` ->
  `http://trader:8000`. Token saved privately.
- [x] DigitalOcean droplet (Ubuntu 24.04, NYC), user `trader`, SSH keys only, ufw (SSH only),
  fail2ban, unattended-upgrades, Docker.

## Still to do, in this order

### 1. Wait for the deposit to clear

- [ ] In IBKR's Client Portal, the account balance ("Net Liquidation Value" / "Equity with
  Loan Value") shows about $510 instead of $10.

Bank transfers usually take a few business days. Nothing below that costs money will work
before the funds have settled.

### 2. Top up to about $650 (recommended)

- [ ] Deposit about $140 more.

Why: IBKR only delivers market data to the API when the account has **at least $500 of
equity plus the cost of the subscription**
([IBKR: minimum equity for market data](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/market-data-subscription-minimum-equity-balance-requirements),
[IBKR API market data](https://www.interactivebrokers.com/campus/ibkr-api-page/market-data-subscriptions/)).
At $510, one bad day or the monthly data fee drops you under the line and the bot goes
back to delayed prices. About $650 leaves a cushion of roughly three maximum-loss days
(the default daily loss limit is 6%, about $39).

### 3. Turn on IB Key (IBKR Mobile) on your Android phone

IB Key is IBKR's two-factor login. The bot's gateway needs it for every full login with
your live username (about once a week).

- [ ] Install **IBKR Mobile** (publisher: Interactive Brokers) from the Google Play Store.
  Needs Android 7 or newer and a phone that is not rooted.
- [ ] Open the app and tap **Register Two-Factor** on the login screen (some versions:
  **Authenticate** > **Register**).
- [ ] Log in with your IBKR username and password, choose the phone number for the SMS,
  and enter the activation code from the text message.
- [ ] Create a 4-6 digit PIN (you can allow fingerprint unlock too).
- [ ] Test it: log in to the Client Portal on your laptop. A notification should arrive on
  the phone: tap it and confirm with the PIN or fingerprint.
- [ ] Make sure the pushes keep arriving: Android **Settings > Apps > IBKR Mobile** >
  Notifications **on**, and Battery > **Unrestricted** (so Android does not put it to sleep).

Sources: [IBKR Knowledge Base: activating IB Key](https://ibkb.interactivebrokers.com/article/3862),
[IBKR Campus: IB Key on Android](https://www.interactivebrokers.eu/campus/trading-lessons/ib-key-two-factor-authentication-android/).
If the app says the user is already enrolled, IB Key is already active. **Verify on IBKR's
site** if the menus differ.

### 4. Request the paper trading account

The paper account trades with fake money at real prices. It has its **own username and
password**, separate from your live login.

- [ ] Log in to the Client Portal (live account).
- [ ] Open **Settings** (person icon) > **Account Settings** > **Paper Trading Account**
  and request one. Pick a password for the paper username it shows. **Verify on IBKR's site.**
- [ ] Save the paper username and password in your password manager.

A request made before 4 pm ET on a business day is usually ready the next day
([IBKR Campus: request a paper trading account](https://www.interactivebrokers.com/campus/trading-lessons/request-paper-trading-account/)).
- [ ] Shrink the play money. Paper accounts start with $1,000,000
  ([IBKR Guides](https://www.ibkrguides.com/clientportal/accountmanagementforpapertradingaccount.htm)).
  In the paper account's Client Portal, use the paper account reset and pick the smallest
  amount the dropdown offers ([IBKR Campus](https://www.interactivebrokers.com/campus/trading-lessons/using-ibkrs-paper-trading-account/)).
  **Verify on IBKR's site.**

Why it matters: the app's percentage limits (2% risk per trade, 6% daily loss) are
computed from the account's equity. On a $1,000,000 paper account they never bind; only
the $250 maximum order keeps trades the size they will be on your real account.

### 5. Subscribe to US market data (after steps 1-2 have cleared)

Free data is delayed by 15 minutes. That is fine for the simulator and for a first look at
paper trading, but the app refuses stock entries on a live account with delayed data.

- [ ] Client Portal > **Settings** > **User Settings** > **Market Data Subscriptions** >
  Configure. **Verify on IBKR's site.**
- [ ] Answer the subscriber questionnaire. Choose **Non-Professional** if you trade for
  yourself only (professional data costs much more).
- [ ] Subscribe to **US Securities Snapshot and Futures Value Bundle** (non-professional,
  $10.00/month, waived in any month with at least $30 in commissions) and **US Equity and
  Options Add-On Streaming Bundle** ($4.50/month) for streaming quotes
  ([IBKR: US Securities Snapshot bundle](https://ibkb.interactivebrokers.com/fr/node/2840),
  [IBKR market data pricing](https://www.interactivebrokers.com/en/pricing/research-news-marketdata.php)).
- [ ] Crypto quotes: check in the same screen whether your crypto (ZEROHASH) quotes are
  real time without a subscription. **Verify on IBKR's site**; this was not confirmed.

At this account size the $30 commission waiver needs about 35 stock round trips a month,
so plan on paying the full $14.50.

### 6. Share the market data with the paper account

By default the paper account does **not** use your subscriptions.

- [ ] Client Portal > **Settings** > **Paper Trading Account**: set **Share real-time market
  data with paper trading account** to **Yes** and pick your live username. **Verify on
  IBKR's site.**

Catch: with sharing on, only one of the two users can receive live data at a time. While
the bot runs on paper during market hours, logging in to your live account (IBKR Mobile,
TWS, Client Portal trading) can take the data away from the paper session
([IBKR Knowledge Base: market data for paper accounts](https://ibkb.interactivebrokers.com/article/1719)).
IB Gateway then reports error **10197** ("No market data during competing live session").
The bot treats the affected symbols as having no usable data: no new entries in them, a
yellow warning on the dashboard and the message under **Last error**. Broker-side stops keep
working. Log out of the live session (or close IBKR Mobile) and the data comes back within
a minute; see [DEPLOY.md troubleshooting](DEPLOY.md#no-market-data-error-10197).

### 7. Create a Claude API key (needed for consensus; the bot works rules-only without AI keys)

- [ ] Sign in at <https://platform.claude.com> (the Claude Console; older links say
  console.anthropic.com).
- [ ] Billing: add a small prepaid credit, for example $10, and leave **auto-reload off**.
  The credit balance is then a hard ceiling on what the bot can spend.
- [ ] If the Console offers a monthly spend limit (Settings > Limits), set one too.
- [ ] **API keys** > **Create key**, name it `desk`, copy it (it is shown only once) and keep
  it in your password manager until you paste it into `.env` on the droplet
  ([DEPLOY.md step 4](DEPLOY.md#4-create-the-settings-file-env)).
- [ ] Read the [AI cost estimate](#ai-api-cost-estimate) below and pick a daily budget
  in the app (Settings > `llm_daily_budget_usd`; the default is $3.00/day for both models
  together).

Menu names in the Console may differ slightly.

### 7b. Create an xAI (Grok) API key (needed for consensus)

The default decision mode is **consensus**: a BUY goes out only when Claude **and** Grok
both pick the same stock. Your **SuperGrok subscription does not include API access**; the
API is billed separately with prepaid credits.

- [ ] Sign in at <https://console.x.ai> with the same X/xAI account (or a new one).
- [ ] Billing: buy a small amount of **prepaid credits**, for example $10, and leave any
  auto top-up off, so the balance is a hard ceiling. If the console offers a monthly
  **spending limit**, set one as well. **Verify on xAI's site**; menu names were not
  confirmed.
- [ ] **API Keys** > **Create API key**, name it `desk`. If the console lets you restrict a
  key to certain models or endpoints, allow only the Grok model the bot uses
  (`grok-4.7` by default). Copy the key (shown once) into your password manager, then into
  `.env` on the droplet as `XAI_API_KEY=...`
  ([DEPLOY.md step 4](DEPLOY.md#4-create-the-settings-file-env)).
- [ ] Optional: check the model name and prices on the console's models page. If they
  differ from `grok-4.7` at $2 / $6 per million tokens (the figures the app assumes; they
  come from third-party price lists, not from xAI directly), set `TRADER_XAI_MODEL` and
  `TRADER_XAI_PRICE_INPUT` / `TRADER_XAI_PRICE_CACHED_INPUT` / `TRADER_XAI_PRICE_OUTPUT` in
  `.env` so the daily AI budget counts Grok correctly.

With only one of the two keys the bot falls back to that model alone and the dashboard
says so ("Consensus mode needs both API keys..."); with neither it makes rules-only
decisions. Nothing pretends to be a consensus that is not one.

### 8. Phone alerts with ntfy (optional, recommended)

- [ ] Install **ntfy** from the Google Play Store (or F-Droid).
- [ ] Make a topic name nobody can guess. On the droplet run `openssl rand -hex 16` and copy
  the result. Anyone who knows the topic can read your alerts, so treat it like a password.
- [ ] In the ntfy app tap **+**, enter the topic, keep the server `ntfy.sh`, and subscribe.
- [ ] In `.env` on the droplet: `TRADER_NTFY_URL=https://ntfy.sh/<your-topic>`, then
  `docker compose up -d`.
- [ ] Send a test alert: `docker compose exec trader python -m trader notify-test`. It
  should show on the phone within a few seconds.

The Google Play version delivers ntfy.sh messages instantly through Google's push service
([ntfy docs: phone app](https://docs.ntfy.sh/subscribe/phone)). Alerts cover each buy and
each closed trade (with its P&L), halts and the kill switch, broker disconnects longer than
a minute, positions left without a working stop, balance changes, and orders waiting for
your approval.

### 9. An authenticator app for the AI Trader login

- [ ] Install a 6-digit code app (Google Authenticator, Microsoft Authenticator, Aegis...).
  This is separate from IB Key: IB Key protects your IBKR login, the authenticator protects
  the AI Trader app. Choose one with an encrypted backup so a lost phone is not a lockout
  (and see [SECURITY.md](SECURITY.md#if-you-lose-your-phone)).

## Monthly costs

| Item | Monthly cost | Notes |
|---|---|---|
| DigitalOcean droplet, Basic 2 GB | $12 | Enough with a swap file ([DEPLOY.md](DEPLOY.md#2-add-a-swap-file-2-gb-droplets)); 4 GB is $24 ([2026 price list](https://www.cloudhim.com/cloud-costs/digitalocean-droplet-pricing-2026)). Hetzner raised US prices in June 2026 ([Hetzner notice](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)) |
| Market data | $0 delayed, or $14.50 | $10 bundle (waived at $30 commissions) + $4.50 streaming add-on |
| AI APIs (Claude + Grok) | $0-90 | Depends on decision mode, model, effort, crypto on/off and the daily budget (which covers both); see below |
| Commissions (example) | ~$17 | 20 stock round trips of ~$220 at ~$0.84 each (Tiered) |
| Commissions (example) | ~$40 | 20 crypto round trips of $100 at ~$2.00 each (2%) |
| Cloudflare Tunnel + Access | $0 | Zero Trust free plan |
| ntfy | $0 | Public ntfy.sh server |

Three realistic totals before any trading profit or loss:

| Setup | Per month | Share of a $650 account |
|---|---|---|
| Paper, delayed data, rules only | $12 | 1.8% |
| Paper or live with data, Claude Haiku 5.5 alone | ~$28 | 4.3% |
| Paper or live with data, consensus Haiku 5.5 + Grok, quiet days | ~$35-40 | 5.5-6% |
| Live with data, consensus Opus 5.5 + Grok on busy days (budget-capped) | ~$90 | 14% |

The bot has to earn these costs back before it makes you anything. On a $650 account,
every $6.50 of monthly cost is 1% of the account.

## AI API cost estimate

Claude prices per million tokens ([Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)):

| Model | Input | Output | Cached input read | Cache write (1 hour) |
|---|---|---|---|---|
| `claude-opus-5-5` (app default) | $4.00 | $20.00 | $0.20 | $8.00 |
| `claude-sonnet-5-5` | $2.00 | $10.00 | $0.20 | $4.00 |
| `claude-haiku-5-5` | $0.10 | $0.50 | $0.01 | $0.20 |

Grok (`grok-4.7`, the app default): about **$2.00 input, $0.50 cached input, $6.00 output**
per million tokens, reasoning tokens billed as output. These are third-party figures that
were not confirmed against xAI's own price page; check console.x.ai and override them in
`.env` if they differ.

**One Claude call.** Measured from this app's prompt code: the fixed instructions are about
1,300 tokens (about 1,700 with the answer format), and one cycle's market snapshot is about
1,000-1,500 tokens. The answer, including the model's thinking at the default `medium`
effort, is assumed to be about 2,000 tokens. The instructions are cached for **1 hour**
(cycles are 5 minutes apart but Claude is skipped when nothing qualifies, so a 5-minute
cache would often expire): one cache write per hour at 2x the input price, then cheap cache
reads:

| Model | Instructions (read; write once an hour) | Snapshot | Answer | **Per call** |
|---|---|---|---|---|
| Opus 5.5 | 1,700 x $0.20/M = $0.0003 (write $0.0136) | 1,500 x $4/M = $0.006 | 2,000 x $20/M = $0.040 | **~$0.047** |
| Sonnet 5.5 | $0.0003 (write $0.0068) | $0.003 | $0.020 | **~$0.024** |
| Haiku 5.5 | $0.00002 (write $0.0003) | $0.00015 | $0.001 | **~$0.0012** |

**One Grok call** (consensus asks Grok too, at the same moment): about 3,200 prompt tokens
(assumed uncached) x $2/M = $0.0064 plus 2,000-4,000 answer and reasoning tokens x $6/M =
$0.012-0.024, so **~$0.02-0.03 per call**.

| Decision mode | Per call | vs. Claude alone |
|---|---|---|
| Consensus, Opus 5.5 + Grok | ~$0.07 | ~1.5x |
| Consensus, Sonnet 5.5 + Grok | ~$0.05 | ~2x |
| Consensus, Haiku 5.5 + Grok | ~$0.026 | ~20x (Grok dominates) |

**How many calls.** The engine runs a cycle every 5 minutes while something is tradable
(stocks 09:30-16:00 ET on trading days, crypto around the clock if enabled), but it only
asks the AI when a scanner signal scores at least `llm_min_candidate_score` (0.5) **or a
position is open**. Most setups score above 0.5, and an open position means a call every
cycle. Rough scenarios (Claude alone; multiply by the factor above for consensus):

| Scenario | Calls | Opus 5.5 | Sonnet 5.5 | Haiku 5.5 |
|---|---|---|---|---|
| Stocks only, quiet days | 20/day x 21 days | $20/month | $10/month | $0.50/month |
| Stocks only, busy days (a position open most of the day) | 60/day x 21 days | $59/month | $30/month | $1.50/month |
| Stocks + 24/7 crypto | 150/day x 30 days | $212 -> **capped at $90** | $108 -> **capped at $90** | $5.40/month |

The daily budget is the real ceiling, and it covers **both** models: at the default
`llm_daily_budget_usd = 3.00` the AI is not asked again that day once $3 is spent, so the
worst case is about $63/month (stocks only) or $90/month (with crypto). What happens then
depends on the mode: with one model the bot falls back to the rules (which do open
positions); **in consensus mode the rules only manage exits**, because a BUY needs both
models, so no new trades open for the rest of the day. On busy days consensus with Opus
reaches $3 after roughly 40 calls (early afternoon); raise the budget, use Sonnet, or
accept the quieter afternoons. Your actual numbers appear on the dashboard (AI cost today)
and on every decision in Activity; recheck this table after a week of paper trading.

**Recommendation (your decision):**

1. **Paper weeks 1-4:** keep Opus 5.5 or Sonnet 5.5 with `llm_effort = low` and a budget of
   **$1.00/day** (at most about $21-30/month). Purpose: find out whether Claude's choices beat
   the plain rules. The signal ledger records both.
2. **Leave crypto off** (`trade_crypto = false`, the default) unless you specifically want it: it adds
   round-the-clock calls, and at $100-250 per trade the 1.4-2% round-trip crypto fee blocks
   nearly every crypto trade anyway ([STRATEGY.md](STRATEGY.md#fee-math-for-a-small-account)).
3. **Live, account under about $1,500:** switch to **Haiku 5.5 alone**
   (`decision_mode = claude`) or **rules-only** (`decision_mode = rules`) unless paper
   results showed the AI-approved signals clearly beating the skipped ones after fees.
   Haiku **with** Grok (consensus) costs about $0.026 per call, i.e. roughly $11/month on
   quiet days and $33/month on busy days (20-60 calls/day x 21 days): 2-5 times the 1% rule
   below, and the $3 daily budget does not stop it. Compare the modes with the signal
   ledger, not with one good week: consensus trades less, which also means fewer chances
   to find out whether it works. A useful rule: keep the AI bill under about 1% of
   the account per month ($6.50 at $650).
4. Go back to a bigger model only when the account (or a proven edge) makes the cost small
   relative to results.

## Rules that matter for a small account

- **Pattern day trader rule.** Under the old FINRA rule a margin account under $25,000 gets
  3 day trades per 5 business days. FINRA replaced it with an intraday margin framework
  effective June 4, 2026, but brokers may phase it in until October 20, 2027
  ([TradeStation summary](https://www.tradestation.com/insights/2026/05/06/day-trade-rule-change/),
  [Investor.gov](https://www.investor.gov/introduction-investing/investing-basics/glossary/pattern-day-trader)).
  Whether IBKR still counts day trades on your No Borrow Margin account: **verify on IBKR's
  site**. The bot reads IBKR's own counter and stops opening stock trades when it reaches
  0. Every stock trade the bot makes is a day trade (it closes stocks before the bell).
  Crypto is not affected.
- **Cash only.** No Borrow Margin never borrows. The bot sizes from the cash IBKR reports
  as available and never uses leverage.
- **Crypto at IBKR** routes through ZEROHASH and accepts only limit and market orders, so
  crypto stop-losses are watched by the bot, not held at IBKR. Stock stops are real orders
  at IBKR and keep working. The `crypto-watchdog` container (its own gateway client id)
  sells crypto the bot opened if the app stops heartbeating or the kill switch is on.
  Stopping the app container for longer than the flatten timeout does that sell; a
  redeploy that is back within 45 seconds does not. A live account with
  `TRADER_ALLOW_LIVE=false` gets no watchdog sells (alerts only). Start with
  `TRADER_WATCHDOG_ORDERS=off` for a few days. If the gateway or IBKR is down, that
  watchdog cannot sell either: there is no crypto stop resting at the broker.
- **One trading session per username.** IBKR checks for an existing session whenever you
  log in to trade. The gateway is set to keep its session (`primary`), so logging in to
  IBKR Mobile or the Client Portal with the same username while the bot runs may be
  refused or may interrupt one of the sessions
  ([IBC settings](https://github.com/IbcAlpha/IBC/blob/master/userguide.md)). **Verify on
  IBKR's site.** Use the AI Trader app to watch the bot; IB Key approvals are not trading
  logins and are fine.

## Only if you run IB Gateway yourself (without the Docker image)

The Docker setup in [DEPLOY.md](DEPLOY.md) configures all of this automatically. If you
ever run IB Gateway or TWS by hand on the same machine as the app:

1. Download **IB Gateway (stable)** from IBKR and log in with **IB API** selected, choosing
   **Paper** or **Live**.
2. Open **Configure > Settings > API > Settings** and set:
   - **Enable ActiveX and Socket Clients**: on (TWS only; IB Gateway always has it on).
   - **Read-Only API**: off to allow orders (on is a good extra lock during shadow mode).
   - **Socket port**: 4002 for paper, 4001 for live (TWS: 7497 paper, 7496 live).
   - **Allow connections from localhost only**: on. **Trusted IPs**: `127.0.0.1`.
   - Leave the order **Precautions** at their defaults.
3. Under **Lock and Exit**, set an **Auto restart** time outside market hours so the
   gateway stays logged in all week with one 2FA approval
   ([IBKR: daily and weekly reauthentication](https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/daily-weekly-reauthentication)).
4. In the app's `.env`: `TRADER_BROKER=ibkr`, `TRADER_IB_HOST=127.0.0.1`,
   `TRADER_IB_PORT=4002` (or `4001` live).

Source for the API settings: [IBKR: TWS / IB Gateway API configuration](https://www.interactivebrokers.com/docs/excel/rtd/download-tws-or-ib-gateway/tws-configuration-for-excel.md).
The settings screens move between versions, so **verify on IBKR's site**.
