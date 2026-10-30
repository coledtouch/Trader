# Security

AI Trader can move real money, so it is built in layers: getting past one layer should
never be enough to place a real order. This page explains who can reach what, where the
secrets live, what gets logged, and what to do when a device is lost.

## What we protect

1. **The money in the IBKR account**: no one but you (and the bot within its limits) may
   place orders.
2. **Credentials**: the IBKR password, the Claude and xAI (Grok) API keys, the Cloudflare
   Tunnel token, the app's password and 2FA secret.
3. **Your trading data**: balances, positions and the journal.

## The layers, from the internet inward

```
 Internet
   |
 [1] Cloudflare Access ------- only your e-mail address, one-time PIN by e-mail
   |
 [2] Cloudflare Tunnel ------- outbound connection from the droplet; no open ports
   |
 [3] Access token check ------ the app verifies Cloudflare's signed token (when configured)
   |
 [4] App login --------------- password (argon2id) + 6-digit 2FA code, rate limited
                               (or the code alone: see "Code-only sign-in" below)
   |
 [5] Session protections ----- secure cookie, CSRF token + Origin check, idle timeout
   |
 [6] Step-up 2FA ------------- fresh code to arm live trading, resume after a halt,
   |                           loosen risk, re-enable a setup, or enable autopilot while live
 [7] Live trading gate ------- TRADER_ALLOW_LIVE=true AND a live account AND "armed"
   |
 [8] Risk manager ------------ hard caps in code, long only, stop on every entry
   |
 [9] IBKR -------------------- stop-loss orders held at the broker; IB Gateway API
                               reachable only inside the server's private Docker network
```

## Threats and controls

| Threat | Controls |
|---|---|
| A stranger finds `desk.kaxo.dev` | Cloudflare Access shows a PIN page and only sends PINs to your address. Behind it, the app needs your password **and** a 2FA code (only the code with code-only sign-in, below). Failed logins: 5 per IP per 15 minutes, then all logins lock after 10 failures in 15 minutes. Every attempt is audited. |
| Someone reaches the droplet directly, skipping Cloudflare | Nothing listens on the internet except SSH: ufw allows only SSH, the tunnel is an outbound connection, and the app's port is published on `127.0.0.1` only. With `TRADER_CF_ACCESS_TEAM_DOMAIN`/`TRADER_CF_ACCESS_AUD` set, the app also refuses any request without a valid, signed Cloudflare Access token for your application (checked against your team's public keys, audience, issuer and expiry). Those two values are required when `TRADER_ALLOW_LIVE=true`: the app will not start without them. Paper and simulator mode can leave them empty. Login rate limits use `CF-Connecting-IP` only; a missing header falls back to the socket peer, not `X-Forwarded-For`. |
| Your password leaks | Useless without the 2FA code. Each code works only once (replay protection). |
| Another website tries to act with your logged-in browser | Session cookie is `HttpOnly`, `Secure`, `SameSite=Strict` with the `__Host-` prefix; every change needs a CSRF token and a matching `Origin`. Sessions end after 30 minutes without you using the app (the dashboard's automatic refresh does not count, so an unattended open tab signs out too) or after 12 hours. |
| Someone picks up your unlocked phone with the app open | Arming live trading, resuming after a halt, loosening risk limits (including raising "halve size after N losses"), re-enabling a disabled setup, enabling autopilot while live, leaving consensus decisions and turning off "flatten stocks before the close" all need a **fresh** 2FA code. Disarm, stop, KILL and closing one position need no code: they never open a position. **Disarm** blocks new entries; the bot keeps managing exits for the positions it opened (stops, targets, time stops, flatten before the close, the daily-loss flatten, re-placing a lost stop), because exits only reduce risk. Positions you opened yourself are never touched. |
| A bug or a misbehaving AI | Real orders need all three gates in [7]. Every order passes the risk manager; hard caps in code clamp any setting; the AI can only shrink size; every stock entry carries a stop at IBKR; daily loss limit halts trading. See [STRATEGY.md](STRATEGY.md). |
| Abuse of the IB Gateway API (it has no password of its own) | The gateway publishes no ports and sits on its own Docker network (`broker`) that only the trader and crypto-watchdog containers join ([gnzsnz: leaving localhost](https://github.com/gnzsnz/ib-gateway-docker#leaving-localhost)). The watchdog publishes no ports, runs as the same non-root user, and is not on `edge`. cloudflared is on a separate network (`edge`) with the trader only, so even a tunnel route added in the Cloudflare dashboard (by mistake or by someone with access to your Cloudflare account) cannot reach the gateway's API, VNC, or the watchdog. Never add a `ports:` line to them except the temporary loopback-only VNC in DEPLOY.md, and never put cloudflared on the `broker` network. |
| Docker bypassing the firewall | Docker-published ports skip ufw ([Docker docs](https://docs.docker.com/engine/network/packet-filtering-firewalls/)). Only `127.0.0.1:8000` is published. An optional DigitalOcean Cloud Firewall sits outside the droplet as a second wall. |
| Attacks on SSH | Keys only, no root login, `AllowUsers trader`, fail2ban, automatic security updates (DEPLOY.md, Appendix A). |
| Secrets leaking through Git or images | `.env` and `data/` are in `.gitignore`; the Docker build context is an allow-list (only `requirements.txt` and `trader/`); the app never logs secrets, tokens or codes (the HTTP client's request log, which would show the secret ntfy topic URL, is switched off). |
| Tampered dependencies or images | Python packages pinned to exact versions; Docker images pinned to version tags; CI runs lint and the offline test suite on every push. |
| Someone takes over your e-mail | Cloudflare Access trusts whoever reads the PIN e-mail, so **your e-mail account is the front door**: give it a strong password and 2FA. The app's own password + 2FA still stand behind it. With code-only sign-in (`TRADER_LOGIN_PASSWORD_REQUIRED=false`) only the 6-digit code does, capped by `TRADER_LOGIN_DAILY_FAILURES`. |

Also turn on 2FA for the accounts around the app: Cloudflare, DigitalOcean, GitHub,
Anthropic (Claude Console), xAI (console.x.ai) and your e-mail.

## Code-only sign-in (optional)

`TRADER_LOGIN_PASSWORD_REQUIRED=false` drops the password: the 6-digit code from your
authenticator app is enough to sign in. The sign-in page then shows only the code box.

**Where it applies.** Only behind Cloudflare Access, or on a paper or simulator account.
With Access off, the password is required anyway when `TRADER_IB_PORT` is a live port
(4003, as in DEPLOY.md Part C, or 4001/7496), when `TRADER_IB_ACCOUNT` or the account the
app last connected to is not a paper ("DU") account, or when no account is known yet.
Shadow mode starts without Access, so this is what keeps a live account behind the
password. With Access off, the app also ends every session when it first connects to a
live account (in either mode: sessions outlive restarts), so none opened with the code
alone on paper carries over. You sign in once more.

What it costs: the code becomes the only secret. At any moment 3 of the 1,000,000 possible
codes are valid, so a stranger who guesses has odds of 3 in a million per try. To keep
that small, failed codes are capped at `TRADER_LOGIN_DAILY_FAILURES` (default 20) per
24 hours, counted over all addresses, on top of the 15-minute limits. Your own mistyped
2FA codes in the app (for loosening limits and the like) count toward it too. At 20 guesses a day
that is about a 2% chance per year that a determined guesser gets in.

- **The lockout also blocks you, and a stranger can keep it going.** Once 20 codes have
  failed in 24 hours, every sign-in is refused, even with the right code, until failures
  age out. Someone sending about 20 wrong codes a day can keep it that way. Sessions you
  already have keep working, including the 2FA codes for loosening limits and resuming
  (the daily cap counts against sign-ins only), until they end (`TRADER_SESSION_IDLE_MINUTES`
  without use, or `TRADER_SESSION_MAX_HOURS`). The audit log lists every failure
  (`login_failed`) with its address.
- **To end a lockout:** run `docker compose exec trader python -m trader unlock` (it
  forgets the failed attempts). If it keeps happening, someone has your address: put
  Cloudflare Access back, or set `TRADER_LOGIN_PASSWORD_REQUIRED=true` and run
  `docker compose up -d`.
- **2FA codes for sensitive actions are unchanged.** Loosening risk, arming live trading
  and the other sensitive actions still need a fresh code, and each code works only once.

To also drop the Cloudflare PIN (paper only): delete the `desk.kaxo.dev` application in
Cloudflare Zero Trust (Access, Applications), then remove `TRADER_CF_ACCESS_TEAM_DOMAIN`
and `TRADER_CF_ACCESS_AUD` from `.env` and run `docker compose up -d`. Leaving them set
makes the app refuse every request, because Cloudflare no longer sends the Access token.
The tunnel stays, so the site is still reachable, now by anyone. **Put Access back before
you switch the gateway to your live username** (DEPLOY.md Part C, shadow mode), not only
before `TRADER_ALLOW_LIVE=true`: create the Access application again and put its new
Audience (AUD) tag in `TRADER_CF_ACCESS_AUD` (a new application has a new tag).

## Where secrets live

| Secret | Stored in | Protection |
|---|---|---|
| IBKR username/password | `~/Trader/.env` on the droplet | File mode 600 (owner only). Passed to the ib-gateway container. |
| Cloudflare Tunnel token | `.env` | Mode 600. Passed to the cloudflared container. Anyone with it can run your tunnel, so rotate it if exposed. |
| Claude API key | `.env` | Mode 600. Prepaid credit with auto-reload off caps the damage of a leak. |
| xAI (Grok) API key | `.env` (`XAI_API_KEY`) | Mode 600. Prepaid credits with auto top-up off (and a spending limit if the console offers one) cap the damage. Sent only in the `Authorization` header to `api.x.ai`; scrubbed from error messages, never logged. |
| `TRADER_SECRET_KEY` | `.env` | Mode 600. Encrypts the 2FA secret (Fernet: AES with an integrity check). Back it up in your password manager. |
| App password | `data/trader.db` | Only an argon2id hash is stored. |
| App 2FA secret | `data/trader.db` | Encrypted with `TRADER_SECRET_KEY`. |
| Login sessions | `data/trader.db` | Only a SHA-256 hash of each session token is stored. |
| ntfy topic | `.env` | Mode 600. Alerts contain trading events, not secrets. |
| App push key (VAPID) | `data/trader.db` | Created on first use, encrypted with `TRADER_SECRET_KEY`. Whoever has it could send notifications to subscribed devices, nothing more. |
| Push subscriptions | `data/trader.db` | Each device's endpoint at its browser's push service and its message keys. The server only posts to known push services (Google, Mozilla, Microsoft, Apple), so a subscription cannot point it at another address. Endpoints are never logged or returned by the API. Messages are encrypted end to end (RFC 8291): the push service cannot read them. |
| Dead-man's switch ping URL | `.env` (`TRADER_HEALTHCHECK_URL`) | Mode 600, never logged. Whoever has it can fake pings (hide an outage), not read anything. |
| Database backups | `data/backups/` | Directory 700, files 600. Same contents as `data/trader.db` (journal, password hash, the 2FA secret encrypted with `TRADER_SECRET_KEY`). Keep laptop copies private too. |

The mode-600 statements above describe the Linux droplet. On Windows, protect the
project directory, `.env`, journal and backups with Windows ACLs, including inherited
permissions from the parent directory; Unix `0600` is not a Windows access guarantee.
The test suite asserts POSIX permission bits only on Unix. Passing Windows tests does
not verify Linux filesystem modes or the deployment's parent-directory permissions.

Research frames are stored in the same private journal and retain up to 2,500 compact
scanner snapshots containing account identifiers, quotes, original candidates, indicator
features, settings and model outputs. Historical bars are not duplicated in snapshots;
these captures cannot be historically rescanned. They contain no API credentials but are still
private trading data: protect copies and exports like the journal. Offline replay uses
in-memory broker portfolios and cached opinions; it connects to no broker and calls no
AI API. The replay POST requires the ordinary session, Origin and CSRF checks and is
audited; it cannot arm trading or bypass entry risk controls.

Entry eligibility runs before paid entry analysis and repeats before sends/approvals.
Delayed stock data blocks PAPER and LIVE entries, independent of receipt age. Turning a
setup off invalidates its pending entry approvals; exits for held positions continue.
Failed ntfy deliveries remain immediately retryable, while in-flight duplicates and
successfully delivered duplicate titles are suppressed. Delivery failures never stop
position protection.

Honest limits:

- Root and members of the `docker` group on the droplet can read everything above. That
  is why SSH access is locked down.
- The trader container mounts the whole `.env` (it writes `TRADER_SECRET_KEY` there during
  setup), so it can read the IBKR password and tunnel token. Code running inside it could
  also place orders through the gateway. It is the best-guarded piece (behind layers 1-5),
  runs as a non-root user with a read-only filesystem, no Linux capabilities and
  `no-new-privileges`.
- Cloudflare terminates HTTPS, so Cloudflare can see the traffic between your browser and
  the app.
- Each AI call sends balances, positions, P&L and market data to Anthropic and, in
  consensus or Grok mode, to xAI (no account number, name or credentials). The xAI request
  asks xAI not to store the response (`store: false`); xAI's own retention policy still
  applies to what it receives.

## Audit log

Every owner action is recorded with time, action, details and IP address (taken from
Cloudflare's `CF-Connecting-IP`): logins (successful, failed, rate limited), logout,
start / stop / kill / resume, arm / disarm, order approvals and rejections, settings
changes, failed or rate-limited 2FA step-ups. Command-line actions (`setup`,
`reset-password`, `reset-2fa`, `logout-all`, `unlock`, `disarm`) are recorded with actor `cli`, and
the engine's own actions (halts, flattening, reconnects, disarming at startup because
`TRADER_ALLOW_LIVE` is off) with actor `engine`.

Read it in the app under **Settings > Audit log**. Look at it after anything unusual: an
unknown IP or a login you did not make means changing the password and 2FA (below).

## If you lose your phone

Your phone holds the AI Trader authenticator, IB Key, IBKR Mobile, the app's phone alerts
(and ntfy, if you use it), probably your e-mail, and possibly a logged-in AI Trader app.
Work through this list from your laptop.

1. **Sign out every AI Trader session** (PowerShell: `ssh trader@DROPLET_IP`, then):
   ```bash
   cd ~/Trader
   docker compose exec trader python -m trader logout-all
   ```
   This also **disarms live trading** at once (as do `reset-2fa`, `reset-password` and
   `python -m trader disarm`): the bot drops to shadow mode and opens nothing new. It keeps
   closing the positions it opened (stops, targets, time stops, closing stocks before the
   bell, the daily-loss flatten). Check open positions in IBKR Mobile or the Client Portal
   and close anything else you do not want to hold.
   **With code-only sign-in on**, the lost phone's authenticator alone signs straight back
   in. Also set `TRADER_LOGIN_PASSWORD_REQUIRED=true` in `.env` and run
   `docker compose up -d` now (or do step 5 at once).
2. **Stop alerts to the lost phone.** They need no session, so it would keep showing them
   on its lock screen. Your other devices keep getting them:
   ```bash
   docker compose exec trader python -m trader push-devices
   docker compose exec trader python -m trader push-remove THE-PHONE-ID
   ```
   (`push-devices` lists each device's id with a name such as `Android · Chrome`.) If you
   use **ntfy**, make a new topic now too: `openssl rand -hex 16`, put it in
   `TRADER_NTFY_URL` in `.env`, `docker compose up -d`, and subscribe to it on the new
   phone. The old topic would keep reaching the lost phone's ntfy app. If healthchecks.io
   sends to ntfy too (DEPLOY.md, dead-man's switch), change its **Integrations > ntfy** to
   the new topic URL.
3. **Lock the server gate** so nobody can arm again (the phone holds your 2FA codes): set
   `TRADER_ALLOW_LIVE=false` in `.env` (`nano .env`), then `docker compose up -d`. Starting
   with the gate off also clears any stored arm, so when you later set it back to `true`,
   real orders start only after you press **Arm** again with a fresh 2FA code.
4. **Revoke Cloudflare Access sessions**: Zero Trust > **Access** > **Applications** >
   **desk** > **Revoke existing tokens**
   ([Cloudflare: session management](https://developers.cloudflare.com/cloudflare-one/identity/users/session-management/)).
   If the phone's e-mail app was signed in, sign it out from your e-mail provider's
   security page and change the e-mail password: the Access PIN goes to that mailbox.
5. **New AI Trader 2FA** on the replacement phone (or an authenticator restored from its
   backup):
   ```bash
   docker compose exec trader python -m trader reset-2fa
   ```
   Scan the QR code, type the code. This also signs out all sessions. No new phone yet?
   Skip this step only while the password is required (the default, or set back in step 1):
   the lost phone's codes alone do not sign in, and the bot keeps running inside its
   limits. Keep `TRADER_LOGIN_PASSWORD_REQUIRED=true` until you have run `reset-2fa`.
   On the new phone, install the app, sign in, and turn alerts on: **Settings > Phone
   alerts > Turn on alerts on this device** (a test alert follows).
6. **IBKR**: move IB Key to the new phone. In IBKR Mobile on the new phone, choose the
   option to migrate or reactivate IB Key, log in, and enter the code IBKR texts to your
   registered mobile number ([IBKR: reactivate or transfer IB Key](https://ibkb.interactivebrokers.com/es/node/3073),
   [IBKR: migrate IB Key to a new smartphone](https://ibkb.interactivebrokers.com/article/3283)).
   No access to that number, or worried someone has the phone and your IBKR password? Call
   IBKR Client Services. **Verify on IBKR's site.** Until IB Key works again, the gateway's
   weekly login (live username) cannot be approved and the bot loses its connection at the
   next full login; stock stops stay active at IBKR.
7. **If the phone held copies of secrets** (screenshots, notes, a password manager without
   a strong lock), rotate them:
   - **Tunnel token**: Cloudflare dashboard > **Networking** > **Tunnels** > **desk** >
     **Rotate token** (older dashboards: **Refresh token**), copy the new `eyJ...` token
     into `TUNNEL_TOKEN` in `.env`, then `docker compose up -d`
     ([Cloudflare: tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/)).
   - **Claude API key**: Claude Console > **API keys**: delete the old key, create a new
     one, put it in `ANTHROPIC_API_KEY`, `docker compose up -d`.
   - **xAI API key**: console.x.ai > **API Keys**: delete the old key, create a new one, put
     it in `XAI_API_KEY`, `docker compose up -d`.
   - **IBKR password**: change it in the Client Portal, update `TWS_PASSWORD`, then
     `docker compose up -d` (the gateway logs in again).
   - **ntfy**: make a new topic (`openssl rand -hex 16`), update `TRADER_NTFY_URL`,
     `docker compose up -d`, subscribe on the new phone, point healthchecks.io's ntfy
     integration at the new topic if you use it, and check it with
     `docker compose exec trader python -m trader notify-test`.
8. **Check what happened**: the app's audit log, and trades and account activity in the
   IBKR Client Portal.

## If you lose your laptop

1. Its SSH key can log in to the droplet. From another computer, or DigitalOcean's web
   console (Droplet > **Access** > **Launch Droplet Console**), remove that key's line from
   `/home/trader/.ssh/authorized_keys`, then add the new laptop's key
   (`ssh-keygen -t ed25519` on the new laptop).
2. Sign out sessions: `docker compose exec trader python -m trader logout-all`, and revoke
   Cloudflare Access sessions (step 4 above).
3. Stop alerts to the lost laptop: `docker compose exec trader python -m trader
   push-devices`, then `push-remove` with the laptop's id (a name such as `Windows · Edge`).
4. If the laptop stored secrets in plain files, rotate them (step 7 above).

## If you think the server is compromised

1. In the app: **KILL** (disarm alone only blocks new entries, see the table above).
   Then in IBKR Mobile or the Client Portal, check that all orders are cancelled and close
   what you want closed.
2. Change the IBKR password and rotate every secret in step 7 of "If you lose your phone".
   Delete the GitHub deploy key (repository > Settings > Deploy keys).
3. Rebuild on a fresh droplet (DEPLOY.md, Appendix A) and restore `data/` from a backup made
   before the incident.
4. **Reset phone alerts**: the intruder could read the app's push key and your devices'
   subscriptions, and so send fake notifications that look like the app's. On the new
   server run `docker compose exec trader python -m trader push-reset`. Each device drops
   its old subscription the next time it opens the app (and subscribes again if alerts
   were on), so the old key can no longer reach it. Then open the app on each of your
   devices and check **Settings > Phone alerts** says "Alerts are on for this device"
   (turn them on if not: the restored backup may predate it), and send a test alert.

## Reporting a problem

This is a private project. If you find a security issue in the code, fix it on a branch
and run the tests; do not post details publicly.
