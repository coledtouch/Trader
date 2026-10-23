"""Process-level configuration loaded from environment variables (and an optional .env file).

Secrets live here (never in the database or the UI). Runtime trading settings that
the owner edits from the UI live in the database as BotSettings (see trader.models).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRADER_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- storage -------------------------------------------------------------
    data_dir: Path = Path("./data")

    # --- web / security --------------------------------------------------------
    # Used to encrypt the TOTP secret at rest. Generate with `python -m trader setup`.
    secret_key: SecretStr = SecretStr("")
    host: str = "127.0.0.1"  # keep on localhost; reach it privately through Tailscale
    port: int = 8000
    # Comma-separated Host header allow-list, e.g. "localhost,127.0.0.1,trader.tail1234.ts.net"
    allowed_hosts: str = "localhost,127.0.0.1"
    # Public origin(s) the browser uses, comma-separated, e.g. "https://trader.tail1234.ts.net".
    # Mutating requests whose Origin header is not in this list (or same-host) are rejected.
    public_origins: str = ""
    cookie_secure: bool = True  # set False only for plain-http local development
    session_idle_minutes: int = 30
    session_max_hours: int = 12
    login_max_attempts_per_ip: int = 5  # per 15 minutes
    login_lockout_failures: int = 10  # global failures in 15 minutes -> lock all logins
    # True only behind a proxy you control (cloudflared): the client IP is then taken from
    # CF-Connecting-IP, else the first X-Forwarded-For entry.
    trust_proxy_headers: bool = False
    # Cloudflare Access (optional defense in depth). When BOTH are set, every request except
    # /healthz must carry a valid Cf-Access-Jwt-Assertion for this team and application.
    cf_access_team_domain: str = ""  # e.g. "yourteam.cloudflareaccess.com"
    cf_access_aud: str = ""  # the Access application's Audience (AUD) tag

    # --- broker ----------------------------------------------------------------
    broker: Literal["sim", "ibkr"] = "sim"
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002  # IB Gateway: 4002 paper, 4001 live (TWS: 7497 paper, 7496 live)
    ib_client_id: int = 17
    # 1 = live (needs a paid subscription and >= $500 equity), 3 = delayed (free)
    ib_market_data_type: Literal[1, 2, 3, 4] = 3
    ib_account: str = ""  # optional: pin a specific account id
    # IBKR routes US crypto through ZEROHASH (older accounts: PAXOS).
    ib_crypto_exchange: str = "ZEROHASH"
    # IBKR Pro pricing plan, used for fee estimates in risk checks and the simulator.
    commission_plan: Literal["tiered", "fixed"] = "tiered"

    # Hard gate for real-money orders. Even when True, the owner must also "arm" live
    # trading in the UI with a fresh 2FA code.
    allow_live: bool = False

    # --- AI ----------------------------------------------------------------------
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TRADER_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )

    # --- notifications -------------------------------------------------------------
    # Optional ntfy topic URL, e.g. "https://ntfy.sh/<long-random-topic>"
    ntfy_url: str | None = None
    ntfy_token: SecretStr | None = None

    # --- misc ------------------------------------------------------------------------
    market_timezone: str = "America/New_York"
    log_level: str = "INFO"
    sim_seed: int = 7
    sim_starting_cash: float = 250.0

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def public_origins_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.public_origins.split(",") if o.strip()]

    @property
    def cf_access_enabled(self) -> bool:
        return bool(self.cf_access_team_domain and self.cf_access_aud)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trader.db"


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig()
