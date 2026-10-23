"""AppConfig values arrive as strings from the environment / .env file."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trader.config import AppConfig


@pytest.mark.parametrize("raw", ["1", "3", "4"])
def test_market_data_type_from_env(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("TRADER_IB_MARKET_DATA_TYPE", raw)
    assert AppConfig(_env_file=None).ib_market_data_type == int(raw)


@pytest.mark.parametrize("raw", ["0", "5", "live"])
def test_market_data_type_out_of_range_is_refused(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("TRADER_IB_MARKET_DATA_TYPE", raw)
    with pytest.raises(ValidationError):
        AppConfig(_env_file=None)


def test_env_file_values_and_lists(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = {
        "TRADER_ALLOWED_HOSTS": "desk.kaxo.dev, localhost",
        "TRADER_PUBLIC_ORIGINS": "https://desk.kaxo.dev/",
        "TRADER_IB_MARKET_DATA_TYPE": "1",
        "TRADER_CF_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com",
        "TRADER_CF_ACCESS_AUD": "abc",
        "ANTHROPIC_API_KEY": "sk-test",
    }
    for name in [*lines, "TRADER_ANTHROPIC_API_KEY"]:
        monkeypatch.delenv(name, raising=False)  # the process environment would win
    env = tmp_path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in lines.items()), encoding="utf-8")
    config = AppConfig(_env_file=env)
    assert config.allowed_hosts_list == ["desk.kaxo.dev", "localhost"]
    assert config.public_origins_list == ["https://desk.kaxo.dev"]
    assert config.ib_market_data_type == 1
    assert config.cf_access_enabled
    assert config.anthropic_api_key is not None
    assert config.anthropic_api_key.get_secret_value() == "sk-test"
    assert config.db_path == config.data_dir / "trader.db"
