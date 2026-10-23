# Running AI Trader on your droplet

At the end of this page the app runs on your DigitalOcean droplet and you open it at
`https://desk.kaxo.dev` from your laptop or phone. You start with the simulator (no IBKR
needed), then switch to the paper account, then to shadow mode, and only then to real
money.

How to read this page:

- Grey boxes are commands. Copy one box, paste it, press Enter, then go to the next one.
- Commands run **on the droplet** (after `ssh`) unless a step says **PowerShell on your
  laptop**.
- Pasting: in Windows Terminal or PowerShell, right-click (or `Ctrl+Shift+V`) pastes.
- Replace words in CAPITALS such as `DROPLET_IP` with your own value.
- **nano** is the text editor on the droplet: move with the arrow keys, paste with
  right-click, save with `Ctrl+O` then `Enter`, quit with `Ctrl+X`.

Contents: [Part A: install and run the simulator](#part-a-install-and-run-the-simulator) ·
[Part B: paper trading](#part-b-paper-trading) ·
[Part C: shadow mode](#part-c-shadow-mode-live-account-orders-blocked) ·
[Part D: live trading](#part-d-live-trading) ·
[Everyday operations](#everyday-operations) ·
[Troubleshooting](#troubleshooting) ·
[Appendix A: droplet from scratch](#appendix-a-preparing-a-droplet-from-scratch) ·
[Appendix B: Tailscale instead of Cloudflare](#appendix-b-tailscale-instead-of-cloudflare)

---

## Part A: install and run the simulator

### 1. Connect to the droplet

**PowerShell on your laptop:**

```powershell
ssh trader@DROPLET_IP
```

Then check the tools and your user id:

```bash
docker --version && docker compose version && id
```

You should see Docker version numbers and `uid=1000(trader) gid=1000(trader) ... docker`.
If `docker` is missing from the list of groups, log out (`exit`) and back in. If your uid
or gid is not 1000, note them for step 4.

### 2. Add a swap file (2 GB droplets)

IB Gateway is a Java program that needs most of a gigabyte. A swap file keeps a 2 GB
droplet from running out of memory. Skip this if `free -h` already shows a Swap line
that is not 0.

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

### 3. Get the code

**If the GitHub repository is public:**

```bash
git clone https://github.com/coledtouch/Trader.git ~/Trader
```

**If it is private (recommended):** use a *deploy key*, a key that can only read this one
repository and nothing else in your GitHub account.

1. Create the key on the droplet and show its public half:
   ```bash
   ssh-keygen -t ed25519 -C "desk droplet deploy key" -f ~/.ssh/trader_deploy -N ""
   cat ~/.ssh/trader_deploy.pub
   ```
2. On GitHub, open the repository > **Settings** > **Deploy keys** > **Add deploy key**.
   Title: `desk droplet`. Paste the line that starts with `ssh-ed25519`. Leave **Allow
   write access** unticked. Click **Add key**.
3. Tell the droplet to use that key for this repository, then clone:
   ```bash
   printf 'Host github-trader\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/trader_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
   chmod 600 ~/.ssh/config
   git clone github-trader:coledtouch/Trader.git ~/Trader
   ```
   The first time, SSH asks whether to trust github.com. Type `yes` if the fingerprint
   shown is `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`
   ([GitHub's published fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)).

A deploy key is simpler and safer than a personal access token: it never expires, cannot
touch your other repositories, and cannot push. If the code you want is on a branch that
is not merged yet: `cd ~/Trader && git checkout BRANCH-NAME`.

### 4. Create the settings file (.env)

Do this **before** any `docker compose` command: Compose refuses to start without `.env`,
and if `data` does not exist Docker creates it as a root-owned folder the app cannot
write to.

```bash
cd ~/Trader
cp .env.example .env
chmod 600 .env
mkdir -p data
nano .env
```

Fill in now (everything else keeps its safe default):

| Setting | Value |
|---|---|
| `TUNNEL_TOKEN` | Your tunnel token: the long `eyJ...` string only. Cloudflare dashboard > **Networking** > **Tunnels** > **desk** > **Add a replica** shows it inside an install command ([Cloudflare: tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/)) |
| `ANTHROPIC_API_KEY` | Your Claude key, if you have one already ([SETUP_IBKR step 7](SETUP_IBKR.md#7-create-a-claude-api-key-needed-for-consensus-the-bot-works-rules-only-without-ai-keys)) |
| `XAI_API_KEY` | Your xAI (Grok) key, if you have one already ([SETUP_IBKR step 7b](SETUP_IBKR.md#7b-create-an-xai-grok-api-key-needed-for-consensus)). Consensus needs both keys; with one the bot uses that model alone, with none it makes rules-only decisions, and the dashboard says which |
| `APP_UID`, `APP_GID` | Only if step 1 showed something other than 1000 |

Leave `TRADER_SECRET_KEY` empty (the next step fills it in), `TRADER_BROKER=sim` and
`COMPOSE_PROFILES=` empty. Save and quit nano, then check that only you can read the file:

```bash
ls -l .env
```

It must start with `-rw-------`.

### 5. Build the app and create your login

```bash
docker compose build
docker compose run --rm trader python -m trader setup
```

The build takes a few minutes the first time. Then `setup`:

1. asks for a password twice (12+ characters; nothing appears while you type),
2. shows a QR code: scan it with your authenticator app (Google Authenticator, Aegis...),
3. asks for the current 6-digit code from the app,
4. writes a new `TRADER_SECRET_KEY` into `.env`.

Check that it worked (this never prints secrets):

```bash
docker compose run --rm trader python -m trader status
```

`TRADER_SECRET_KEY` should say `ok` and `owner credentials` should say `configured`.

**Back up `.env` now.** Show it with `cat .env`, copy the text into a secure note in your
password manager. Without `TRADER_SECRET_KEY` the stored 2FA secret cannot be decrypted.

### 6. Start everything

```bash
docker compose up -d
docker compose ps
```

After about a minute, `trader` shows `healthy` and `cloudflared` shows `Up`. Check that
the tunnel connected:

```bash
docker compose logs --tail 20 cloudflared
```

Look for `Registered tunnel connection`.

### 7. Open the app

1. On your laptop, open <https://desk.kaxo.dev>.
2. Cloudflare asks for your e-mail address and sends a one-time PIN. Enter it.
3. The AI Trader login appears: your password and the 6-digit code from the authenticator.
4. The dashboard shows the **SIM** badge. Try **Start bot**, **Run cycle now** and the
   **KILL** button: it is all fake money. **Run cycle now** also works while the bot is
   stopped: it makes exactly one decision cycle (orders still go through every risk check
   and, with autopilot off, wait for your approval), which is handy for checking a change
   without starting the bot. Stocks only trade 09:30-16:00 ET on weekdays (new
   entries 09:45-15:30); at other times a cycle has nothing to do unless you switch on
   **Trade crypto** in Settings (fine in the simulator; see the fee notes before using it
   for real).

### 8. Install it on your Android phone

1. Open <https://desk.kaxo.dev> in **Chrome** and log in.
2. Tap the **three-dot menu** > **Add to home screen** > **Install** (some versions show
   **Install app** directly).
3. The AI Trader icon opens the app full screen.

When the Cloudflare session expires (the Access application's session duration), the app
shows a stale-data warning: reload it and enter a new e-mail PIN.

### 9. Turn on the Cloudflare Access check (recommended)

The app can verify that every request really came through your Cloudflare Access policy.
This protects you if the tunnel or a Cloudflare setting is ever misconfigured.

1. Cloudflare dashboard > **Zero Trust** > **Settings**: note the **team domain**, like
   `yourteam.cloudflareaccess.com`.
2. **Zero Trust** > **Access** > **Applications** > **desk** > **Configure**: copy the
   **Application Audience (AUD) Tag** (a long hex string)
   ([Cloudflare: validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)).
3. Put both into `.env` and apply:
   ```bash
   nano .env
   ```
   ```
   TRADER_CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
   TRADER_CF_ACCESS_AUD=the-aud-tag
   ```
   ```bash
   docker compose up -d
   ```
4. Check that the check is really on (a value with a typo in its name is otherwise easy to
   miss):
   ```bash
   docker compose exec trader python -m trader status
   ```
   `Cloudflare Access` must say `enabled`; the app log also says
   `Cloudflare Access check: enabled` at every start. If only one of the two values is set,
   the app refuses to start and its log names the missing variable.
5. Reload the app. If every page now says `{"detail":"forbidden"}`, one of the two values is
   wrong: `docker compose logs --tail 20 trader` shows the reason after
   `Cloudflare Access: refused`.

---

## Part B: paper trading

You need the paper account from [SETUP_IBKR.md](SETUP_IBKR.md#4-request-the-paper-trading-account).
Live market data (SETUP_IBKR steps 5-6) is optional at first.

1. Edit the settings:
   ```bash
   cd ~/Trader && nano .env
   ```
   Change these lines (single quotes around the password):
   ```
   COMPOSE_PROFILES=ibkr
   TRADER_BROKER=ibkr
   TRADER_IB_PORT=4004
   TWS_USERID=YOUR-PAPER-USERNAME
   TWS_PASSWORD='YOUR-PAPER-PASSWORD'
   TRADING_MODE=paper
   ```
   **Use a password made of letters and numbers only.** A `$` or other symbol can be cut
   off on its way to the gateway (`$` followed by letters is read as a variable name), and
   IBKR then rejects the login. Change the paper password at IBKR first if needed (Client
   Portal of your real account > Settings > Paper Trading Account). After step 2, check
   that the whole password arrived; this prints only its length:
   ```bash
   docker compose exec ib-gateway sh -c 'printf %s "$TWS_PASSWORD" | wc -c'
   ```
2. Start the gateway and watch it log in (1-2 minutes; `Ctrl+C` stops watching, not the
   gateway):
   ```bash
   docker compose up -d
   docker compose logs -f ib-gateway
   ```
   The paper username needs no phone approval. (If you use your *live* username with
   `TRADING_MODE=paper`, approve the IB Key push on your phone.)
3. In the app the badge changes to **PAPER** and the connection light turns green.
4. In the app's **Settings**, choose the stock universe, the decision mode and the AI
   budget (it covers Claude and Grok together;
   [SETUP_IBKR.md cost advice](SETUP_IBKR.md#ai-api-cost-estimate)) and keep the dollar
   limits sized for your real account (max order $250).
5. Once market data is subscribed and shared with the paper account, switch to live
   prices: in `.env`, set the market data line to 1:
   ```
   TRADER_IB_MARKET_DATA_TYPE=1
   ```
   ```bash
   docker compose up -d
   ```

Let it run for at least 4 weeks. Read [STRATEGY.md](STRATEGY.md#judging-a-setup) for how
to judge the results; the **Signals** tab shows the scorecard.

---

## Part C: shadow mode (live account, orders blocked)

The bot connects to your real account and makes real decisions on real data, but every
new entry is recorded as BLOCKED ("shadow mode") and nothing is bought. This checks live
data, balances and the day-trade counter without risk. (Shadow mode still lets the bot
close positions *it* opened; at this stage it has none.)

1. Edit `.env`:
   ```
   TWS_USERID=YOUR-LIVE-USERNAME
   TWS_PASSWORD='YOUR-LIVE-PASSWORD'
   TRADING_MODE=live
   TRADER_IB_PORT=4003
   TRADER_ALLOW_LIVE=false
   READ_ONLY_API=yes
   TRADER_IB_MARKET_DATA_TYPE=1
   ```
   `READ_ONLY_API=yes` makes the gateway itself refuse orders: a second lock on top of
   shadow mode. (It also means the KILL button cannot sell anything; at this stage the bot
   holds nothing, and anything you bought yourself you manage in IBKR's apps.)
2. Apply, then **approve the IB Key notification on your phone** within about 3 minutes:
   ```bash
   docker compose up -d
   docker compose logs -f ib-gateway
   ```
   Missed it? The gateway tries again by itself and sends a new push.
3. The badge shows **LIVE-SHADOW**. Watch for 1-2 weeks: decisions in Activity, orders
   marked BLOCKED with reason "shadow mode", correct equity and cash.

From now on the gateway needs your phone once a week: see
[Weekly IBKR re-login](#weekly-ibkr-re-login).

---

## Part D: live trading

Only after paper and shadow results convinced you. Start small.

1. In the app's **Settings**, first make the limits tighter than paper, for example:
   autopilot **off** (you approve every order on your phone), max order $100, risk per
   trade 1%, max 3 trades per day. Tightening never needs a 2FA code; loosening later does.
2. In `.env`:
   ```
   TRADER_ALLOW_LIVE=true
   READ_ONLY_API=no
   ```
   ```bash
   docker compose up -d
   ```
   The gateway restarts because `READ_ONLY_API` changed: approve the IB Key push.
3. In the app: **Settings** > **Live trading** > **Arm**, and enter a fresh 6-digit code.
   The badge turns **LIVE-ARMED**. Real orders are now possible.
4. To stop real orders at any time: **Disarm** in the app (instant), or from the server
   `docker compose exec trader python -m trader disarm`. Disarm blocks every new entry;
   the bot keeps closing the positions it opened (stops, targets, time stops, closing
   stocks before the bell, the daily-loss flatten), because exits only reduce risk. To close
   everything now, **KILL**. To lock live trading on the server too,
   set `TRADER_ALLOW_LIVE=false` and run `docker compose up -d`: starting with the gate off
   also clears the arm, so after setting it back to `true` real orders need a fresh
   **Arm** with a 2FA code (step 3).

---

## Everyday operations

**Deposits, withdrawals and paper-account resets.** When the account has had no position,
no working order and no fill for 10 minutes, the bot treats a balance change as money moved
in or out (or a paper reset), not as profit or loss: today's P&L, the daily target and the
loss limit leave it out, and the app sends a "Balance change detected" notice. Move money
while the bot is flat (outside market hours is simplest). A change that arrives while a
position is open, or within 10 minutes of a trade, counts as trading P&L, so a deposit then
could stop the bot for the day at "daily target reached" (press **Resume** to continue).

All of these start with `cd ~/Trader`.

| Task | Command |
|---|---|
| Is everything running? | `docker compose ps` |
| App log, last 100 lines | `docker compose logs --tail 100 trader` |
| Follow a log live (`Ctrl+C` to stop) | `docker compose logs -f trader` (or `ib-gateway`, `cloudflared`) |
| Settings check (no secrets shown) | `docker compose exec trader python -m trader status` |
| Restart only the app (gateway stays logged in) | `docker compose restart trader` (keeps the old `.env` values) |
| Apply `.env` changes | `docker compose up -d` (`docker compose restart` does **not** reload `.env`) |
| Disarm live trading from the server | `docker compose exec trader python -m trader disarm` |
| Stop everything | `docker compose down` |

`docker compose down` logs the gateway out. Stock positions keep their stop-loss orders at
IBKR, but **crypto stops are managed by the bot and stop working** while it is down.

**Stop bot** in the app only stops new decisions. As long as the app runs it keeps watching
open positions (crypto stops, the daily loss limit, closing stocks before the bell), and
after a restart the bot comes back in the same state: running if it was running, otherwise
just watching. That also holds in shadow mode (live account not armed, or disarmed): no
new entries, but exits for the bot's own positions still go out. Only KILL closes
everything, including positions you opened yourself. **Run cycle now** still works while the bot is
stopped: it runs one decision cycle on demand (same risk checks, same autopilot rule).

**Switching accounts** (simulator -> paper -> live, or another paper account): the bot
remembers which broker account its journal belongs to. When the account changes it
forgets the old account's positions and working orders (it will not manage them), cancels
pending approvals, restarts today's P&L from the new account's equity, **disarms live
trading**, writes `account_changed` to the audit log and sends a phone alert.

**Emergency, app unreachable:** `docker compose stop trader` stops the bot. Then manage
positions in IBKR Mobile. If IBKR refuses the phone login because the gateway holds the
session, also run `docker compose stop ib-gateway`.

### Updating

Best outside market hours:

```bash
cd ~/Trader
git pull
docker compose up -d --build
docker image prune -f
```

If an update changes the IB Gateway version, the gateway logs in again (live username:
approve the push). To see whether a new version added settings that your `.env` lacks:

```bash
diff <(grep -o '^[A-Z_]*=' .env.example | sort) <(grep -o '^[A-Z_]*=' .env | sort)
```

Lines starting with `<` are names to copy from `.env.example` into `.env`.

### Backups

Everything the app knows (journal, settings, login) is in `data/trader.db`. Make a
consistent copy while it runs:

```bash
cd ~/Trader
docker compose exec trader python -c "import sqlite3; s = sqlite3.connect('/data/trader.db'); d = sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
```

Copy it to your laptop (**PowerShell on your laptop**):

```powershell
scp trader@DROPLET_IP:Trader/data/backup.db "$HOME\Documents\trader-backup-$(Get-Date -Format yyyy-MM-dd).db"
```

Do this weekly, and keep the copy of `.env` in your password manager current (the backup
is useless without `TRADER_SECRET_KEY`). To restore a backup on the droplet:

```bash
cd ~/Trader
docker compose stop trader
cp data/trader.db data/trader.db.old
cp data/backup.db data/trader.db
rm -f data/trader.db-wal data/trader.db-shm
docker compose start trader
```

### Weekly IBKR re-login

Only with your **live** username (shadow and live trading). IBKR requires a full login
with 2FA about once a week; the gateway restarts itself every night at 11:45 PM ET without
needing your phone in between
([IBKR: daily and weekly reauthentication](https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/daily-weekly-reauthentication),
[ibc2 notes](https://pypi.org/project/ibc2/),
[gnzsnz image docs](https://github.com/gnzsnz/ib-gateway-docker)).

The app is set up to make this predictable (`TWS_COLD_RESTART=10:00` in `.env`):

1. **Sunday 10:00 ET**: the gateway logs in from scratch and IBKR Mobile shows a
   notification. Tap it and confirm with your PIN or fingerprint.
2. Missed it? It retries every few minutes and sends a new push; approve any of them.
3. Check: the app's connection light is green, or
   `docker compose logs --tail 30 ib-gateway` shows no repeating errors.
4. Still stuck: `docker compose restart ib-gateway` sends a fresh push.

The same approval is needed after anything that restarts the gateway: a droplet reboot
(security updates), an update, or a `.env` change to a gateway setting. If you turned on
ntfy, you get an alert when the broker connection has been down for over a minute.

---

## Troubleshooting

| What you see | Likely cause | What to do |
|---|---|---|
| Cloudflare page "Bad gateway" / error 502 | The tunnel works but the app does not answer | `docker compose ps`: is `trader` running and healthy? Read `docker compose logs --tail 100 trader` |
| Cloudflare error 1033 or "tunnel error" | `cloudflared` is not connected | `docker compose logs --tail 50 cloudflared`. Usually a missing or wrong `TUNNEL_TOKEN` in `.env`; fix it and `docker compose up -d` |
| Cloudflare page "You don't have access" (403) | Cloudflare Access refused your e-mail address | Zero Trust > Access > Applications > desk > Policies must allow the address you typed |
| White page with `{"detail":"forbidden"}` (403) | The app's Cloudflare Access check failed | Wrong `TRADER_CF_ACCESS_TEAM_DOMAIN` or `TRADER_CF_ACCESS_AUD`. The trader log says why after `Cloudflare Access: refused`. Empty both values + `docker compose up -d` to switch the check off while you fix it |
| `Invalid host header` (400) | `TRADER_ALLOWED_HOSTS` does not contain `desk.kaxo.dev` | Fix it in `.env`, keep `localhost,127.0.0.1` too, `docker compose up -d` |
| Login always fails with correct password | The phone's clock is off (2FA codes are time based), or too many attempts | Set the phone to automatic date and time. After 5 wrong tries wait 15 minutes |
| Login always fails right after `setup`; the trader log says `logins will fail` and `status` shows `TRADER_SECRET_KEY missing` | The container was created before `setup` wrote the key, and `docker compose restart` keeps the old (empty) value | `docker compose up -d --force-recreate trader` |
| `trader` keeps restarting, log (or `status`) says `Cloudflare Access is half configured` | Only one of `TRADER_CF_ACCESS_TEAM_DOMAIN` / `TRADER_CF_ACCESS_AUD` is set, often a misspelled variable name | Set both (exact names) or neither, then `docker compose up -d` |
| Saving settings fails ("origin" error) | `TRADER_PUBLIC_ORIGINS` is not exactly `https://desk.kaxo.dev` | Fix it, no trailing slash, `docker compose up -d` |
| `trader` is `unhealthy` | The health check uses host `127.0.0.1` | Keep `localhost,127.0.0.1` in `TRADER_ALLOWED_HOSTS`; read the trader log |
| `setup` fails or `trader` keeps restarting with `Permission denied: '/data/trader.db'` | `data` was created by Docker as root | `sudo chown -R $(id -u):$(id -g) ~/Trader/data && docker compose up -d` |
| `env file .../.env not found` | `.env` was never created | Do [step 4](#4-create-the-settings-file-env) |
| `trader` keeps restarting, log says `ib_market_data_type ... Input should be less than or equal to 4` (or similar) | `TRADER_IB_MARKET_DATA_TYPE` is not 1, 2, 3 or 4 | Set it to `3` (delayed, free) or `1` (live, needs a subscription), then `docker compose up -d` |
| Dashboard says disconnected after switching to IBKR | Gateway still logging in, or a mismatch | Wait 2 minutes. Check `COMPOSE_PROFILES=ibkr`, `TRADER_BROKER=ibkr`, and that the port matches the mode: paper `4004`, live `4003` |
| Gateway log stops after `detected dialog entitled: Gateway` (right after `Authenticating...`), then only `socat ... Connection refused` | IBKR rejected the login: usually the password, often cut short at a `$` or other symbol | Run the length check from [Part B](#part-b-paper-trading); if it is shorter than your password, change the password to letters and numbers only, update `TWS_PASSWORD` and `docker compose up -d --force-recreate ib-gateway`. Also test the same username and password on the IBKR website |
| Gateway log repeats login attempts ("login loop") | Wrong username/password; paper username with `TRADING_MODE=live`; IB Key push not approved; IBKR's nightly maintenance (around midnight ET and weekends) | Log in to the Client Portal in a browser with the same username to rule out a locked account or password problem. Check phone notifications. If it persists, look at the gateway screen (below) |
| No IB Key notification arrives | Android blocked notifications or put IBKR Mobile to sleep | Phone settings: notifications on, battery Unrestricted. Open IBKR Mobile, then `docker compose restart ib-gateway` |
| Dashboard shows delayed data | No subscription, not shared with paper, or `TRADER_IB_MARKET_DATA_TYPE` not 1 | See SETUP_IBKR steps 5-6 and [Part B step 5](#part-b-paper-trading). Live accounts refuse stock entries on delayed data |
| Yellow warning "IBKR error 10197: no market data while another session of this login holds it" | You are logged in to the live account elsewhere (IBKR Mobile, TWS, Client Portal trading) while the paper gateway runs; shared market data goes to one session only. See [below](#no-market-data-error-10197) | Log out of the other session. Data returns within a minute and the warning clears; meanwhile the bot opens nothing in the affected symbols |
| Yellow warning "Consensus mode needs both API keys..." | `ANTHROPIC_API_KEY` or `XAI_API_KEY` is empty or misspelled in `.env` | Fill in the missing key and `docker compose up -d`, or pick a single-model mode in Settings. Until then the one model with a key decides alone |
| Every Grok decision shows an error ("HTTP 401", "HTTP 403", "HTTP 429") | Wrong xAI key, key not allowed for the model, or prepaid credits used up | console.x.ai: check the key, its model permissions and the credit balance. In consensus mode nothing is bought while Grok fails; exits still work |
| Alert "Broker account changed" | The gateway logged in to a different account (paper/live switch, or another user) | Expected after a switch. If not: check `TWS_USERID`, `TRADING_MODE` and `TRADER_IB_ACCOUNT` in `.env`. The bot does not manage the previous account's positions |
| Disk full | Old images | `df -h`, then `docker image prune -f`. Container logs are capped at 50 MB each |

### No market data (error 10197)

Your paper account uses the live account's market data subscriptions, and IBKR delivers
that data to **one** session at a time. When you log in to the live account (IBKR Mobile,
TWS, Client Portal trading) during market hours, the paper gateway can lose it and IB
Gateway reports error 10197, "No market data during competing live session". Prices the
bot already had may then simply stop updating, which looks normal but is stale.

What the bot does: every symbol that got a 10197 counts as having **no usable data** until
a fresh price arrives after the error. Those symbols get no new entries (approvals wait),
the dashboard shows a yellow warning naming them, and **Last error** says the same. Every
minute or so the bot re-subscribes to find out whether data is back. Stop orders already
at IBKR keep working; the bot's own checks for those symbols pause.

What to do: log out of the live session (close IBKR Mobile completely, or use it only
outside market hours while paper trading). The warning disappears once prices flow again.
In shadow and live mode the gateway itself is the live session, so this mostly matters
during paper trading.

### See the gateway screen (VNC)

For login problems you cannot explain, look at what IB Gateway shows. VNC stays reachable
only through your SSH connection.

1. On the droplet, give VNC a temporary password and publish it on the droplet's own
   loopback address only:
   ```bash
   cd ~/Trader
   nano .env
   ```
   Set `VNC_SERVER_PASSWORD=SOME-TEMPORARY-PASSWORD`, save, then:
   ```bash
   printf 'services:\n  ib-gateway:\n    ports:\n      - "127.0.0.1:5900:5900"\n' > docker-compose.override.yml
   docker compose up -d
   ```
2. **PowerShell on your laptop**, keep this window open:
   ```powershell
   ssh -L 5900:127.0.0.1:5900 trader@DROPLET_IP
   ```
3. Open a VNC viewer on the laptop (for example RealVNC Viewer or TightVNC) and connect to
   `localhost:5900` with the temporary password.
4. When done, on the droplet:
   ```bash
   rm docker-compose.override.yml
   ```
   Empty `VNC_SERVER_PASSWORD` in `.env` again, then `docker compose up -d`.

The gateway restarts in steps 1 and 4 (live username: approve the push).

---

## Appendix A: preparing a droplet from scratch

You have done this already. It is here so the setup can be repeated on a new server.

1. **Create the droplet** in DigitalOcean: Ubuntu 24.04 (LTS), Basic, Regular, 2 GB /
   $12 per month (4 GB / $24 if you want headroom), a New York region, authentication
   **SSH key**. Create the key in **PowerShell on your laptop** with
   `ssh-keygen -t ed25519`, then paste the contents of `$HOME\.ssh\id_ed25519.pub`.
2. **First login and updates** (PowerShell: `ssh root@DROPLET_IP`):
   ```bash
   apt update && apt -y full-upgrade
   ```
   If a purple screen says a new version of `/etc/ssh/sshd_config` is available, choose
   **keep the local version currently installed** (the default). Your SSH settings live
   in a separate file anyway (step 4).
3. **Create the user `trader`** with sudo and your SSH key:
   ```bash
   adduser trader
   usermod -aG sudo trader
   rsync --archive --chown=trader:trader ~/.ssh /home/trader
   ```
   Open a **new** PowerShell window and check `ssh trader@DROPLET_IP` works before going on.
4. **SSH: keys only, no root login.** The file name matters: DigitalOcean's
   `/etc/ssh/sshd_config.d/50-cloud-init.conf` sets `PasswordAuthentication yes`, and sshd
   uses the **first** value it reads, so your file must sort before it (`00-...`).
   ```bash
   printf 'PermitRootLogin no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\nAllowUsers trader\n' | sudo tee /etc/ssh/sshd_config.d/00-hardening.conf
   sudo sshd -t && sudo systemctl restart ssh
   sudo sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) '
   ```
   Expect `permitrootlogin no`, `passwordauthentication no`,
   `kbdinteractiveauthentication no`, `pubkeyauthentication yes`. Keep your current
   window open and test a new login before closing it.
5. **Firewall: only SSH in.**
   ```bash
   sudo ufw allow OpenSSH
   sudo ufw enable
   sudo ufw status verbose
   ```
   Optional extra layer: a free DigitalOcean **Cloud Firewall** (Networking > Firewalls)
   allowing inbound TCP 22 only, attached to the droplet. It sits outside the server, so
   nothing on the droplet can bypass it.
6. **fail2ban and automatic security updates:**
   ```bash
   sudo apt install -y fail2ban unattended-upgrades
   sudo systemctl enable --now fail2ban
   sudo dpkg-reconfigure -plow unattended-upgrades
   ```
   Answer **Yes**. `sudo fail2ban-client status sshd` should list the jail.
7. **Docker** from Docker's own package repository
   ([Docker: install on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)):
   ```bash
   sudo apt-get install -y ca-certificates curl
   sudo install -m 0755 -d /etc/apt/keyrings
   sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
   sudo chmod a+r /etc/apt/keyrings/docker.asc
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
   sudo apt-get update
   sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   sudo usermod -aG docker trader
   ```
   Log out and back in, then `docker run --rm hello-world`.
8. Continue with [Part A](#part-a-install-and-run-the-simulator).

**Docker and ufw:** ports that Docker publishes skip ufw's rules
([Docker: packet filtering and firewalls](https://docs.docker.com/engine/network/packet-filtering-firewalls/)).
That is why `docker-compose.yml` publishes only `127.0.0.1:8000` (reachable from the
droplet itself, never from the internet) and nothing else. Never add a `ports:` line
without `127.0.0.1:` in front.

---

## Appendix B: Tailscale instead of Cloudflare

[Tailscale](https://tailscale.com) is a private network between your own devices. It
replaces Cloudflare Tunnel + Access: the app is then reachable only from devices signed in
to your Tailscale account.

1. Install Tailscale on the droplet and sign in with the link it prints
   ([Tailscale: install on Linux](https://tailscale.com/kb/1031/install-linux)):
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
2. Install the Tailscale app on your laptop and phone and sign in with the same account.
3. Serve the app over HTTPS inside your tailnet
   ([Tailscale serve](https://tailscale.com/kb/1312/serve)):
   ```bash
   sudo tailscale serve --bg 8000
   ```
   It prints an address like `https://MACHINE.TAILNET.ts.net`.
4. In `.env`:
   ```
   TRADER_ALLOWED_HOSTS=MACHINE.TAILNET.ts.net,localhost,127.0.0.1
   TRADER_PUBLIC_ORIGINS=https://MACHINE.TAILNET.ts.net
   TRADER_TRUST_PROXY_HEADERS=false
   TRADER_CF_ACCESS_TEAM_DOMAIN=
   TRADER_CF_ACCESS_AUD=
   ```
   The Cloudflare Access check must be off: Tailscale requests carry no Cloudflare token.
5. Start without the tunnel:
   ```bash
   docker compose up -d --scale cloudflared=0
   ```
   Use the same `--scale cloudflared=0` every time you run `docker compose up -d`.
