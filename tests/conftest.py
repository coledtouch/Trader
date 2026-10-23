"""Shared, generic test fixtures. Tests run offline: no broker, no LLM, no network."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from trader.config import AppConfig
from trader.db import Database


@pytest.fixture
def db() -> Iterator[Database]:
    """A fresh in-memory journal."""
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """AppConfig with safe defaults that ignores any local .env file."""
    return AppConfig(
        _env_file=None,
        data_dir=tmp_path,
        broker="sim",
        allow_live=False,
        anthropic_api_key=None,
        xai_api_key=None,
        ntfy_url=None,
    )
