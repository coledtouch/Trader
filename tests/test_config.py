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


@pytest.mark.parametrize("name", ["XAI_API_KEY", "TRADER_XAI_API_KEY"])
def test_xai_key_from_either_env_name(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    for other in ("XAI_API_KEY", "TRADER_XAI_API_KEY"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "xai-test")
    config = AppConfig(_env_file=None)
    assert config.xai_api_key is not None
    assert config.xai_api_key.get_secret_value() == "xai-test"
    assert "xai-test" not in repr(config)


def test_xai_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TRADER_XAI_MODEL", "TRADER_XAI_BASE_URL", "TRADER_XAI_PRICE_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    config = AppConfig(_env_file=None)
    assert config.xai_model == "grok-4.7"
    assert config.xai_base_url == "https://api.x.ai/v1"
    assert (config.xai_price_input, config.xai_price_cached_input, config.xai_price_output) == (
        2.0,
        0.5,
        6.0,
    )
    monkeypatch.setenv("TRADER_XAI_MODEL", "grok-5")
    monkeypatch.setenv("TRADER_XAI_PRICE_OUTPUT", "7.5")
    config = AppConfig(_env_file=None)
    assert config.xai_model == "grok-5" and config.xai_price_output == 7.5


@pytest.mark.parametrize(
    ("team", "aud", "missing"),
    [
        ("team.cloudflareaccess.com", "", "TRADER_CF_ACCESS_AUD"),
        ("", "abc123", "TRADER_CF_ACCESS_TEAM_DOMAIN"),
    ],
)
def test_half_configured_cloudflare_access_is_refused(
    tmp_path, monkeypatch: pytest.MonkeyPatch, team: str, aud: str, missing: str
) -> None:
    """SEC-6: e.g. TRADER_CF_ACCESS_AUDIENCE (a typo, silently ignored) would leave the
    Access check off without a word; the app refuses to start instead."""
    for name in ("TRADER_CF_ACCESS_TEAM_DOMAIN", "TRADER_CF_ACCESS_AUD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError, match=f"{missing} is empty"):
        AppConfig(_env_file=None, cf_access_team_domain=team, cf_access_aud=aud)
    env = tmp_path / ".env"
    misspelled = "TRADER_CF_ACCESS_AUDIENCE" if team else "TRADER_CF_ACCESS_TEAM"
    present = "TRADER_CF_ACCESS_TEAM_DOMAIN" if team else "TRADER_CF_ACCESS_AUD"
    env.write_text(f"{present}={team or aud}\n{misspelled}=x\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        AppConfig(_env_file=env)
    assert not AppConfig(_env_file=None).cf_access_enabled  # neither set: allowed, off


def test_cli_reports_a_half_configured_access_check(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trader import cli

    for name in ("TRADER_CF_ACCESS_TEAM_DOMAIN", "TRADER_CF_ACCESS_AUD"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("TRADER_CF_ACCESS_TEAM_DOMAIN=team.cloudflareaccess.com\n", encoding="utf-8")
    assert cli.main(["status", "--env-file", str(env)]) == 1
    err = capsys.readouterr().err
    assert "invalid configuration" in err and "TRADER_CF_ACCESS_AUD is empty" in err
