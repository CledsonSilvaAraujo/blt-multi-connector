"""Fixtures compartilhadas para os testes."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_store_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isola o caminho do TOML do store em um arquivo temporário por teste."""

    path = tmp_path / "devices.toml"
    monkeypatch.setenv("BLT_MULTI_CONFIG", str(path))
    return path
