from __future__ import annotations

from unittest.mock import patch

import pytest

from blt_multi import discovery
from blt_multi.errors import DeviceNotFoundError
from blt_multi.runner import CommandResult


@pytest.fixture()
def fake_run() -> dict:
    """Patch `blt_multi.discovery.run`. Retorna dict cmd->CommandResult esperado."""

    mapping: dict[tuple[str, ...], CommandResult] = {}

    def fake(args, **kwargs):  # type: ignore[no-untyped-def]
        key = tuple(args)
        if key in mapping:
            return mapping[key]
        # Default: comando sem saída, sucesso.
        return CommandResult(args=key, returncode=0, stdout="", stderr="")

    with patch("blt_multi.discovery.run", side_effect=fake) as mock:
        yield {"mapping": mapping, "mock": mock}


def _ok(stdout: str) -> CommandResult:
    return CommandResult(args=(), returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> CommandResult:
    return CommandResult(args=(), returncode=1, stdout="", stderr=stderr)


class TestListDevices:
    def test_parseia_linhas_device(self, fake_run: dict) -> None:
        fake_run["mapping"][("bluetoothctl", "--version")] = _ok("5.66\n")
        fake_run["mapping"][("bluetoothctl", "devices")] = _ok(
            "Device AA:BB:CC:DD:EE:01 Headphone One\n"
            "Device aa:bb:cc:dd:ee:02 Speaker Two\n"
            "Não é device\n"
        )
        fake_run["mapping"][("bluetoothctl", "info", "AA:BB:CC:DD:EE:01")] = _ok(
            "Device AA:BB:CC:DD:EE:01\n"
            "\tName: Headphone One\n"
            "\tAlias: JBL\n"
            "\tPaired: yes\n"
            "\tTrusted: yes\n"
            "\tConnected: yes\n"
        )
        fake_run["mapping"][("bluetoothctl", "info", "AA:BB:CC:DD:EE:02")] = _ok(
            "Device AA:BB:CC:DD:EE:02\n"
            "\tName: Speaker Two\n"
            "\tPaired: no\n"
            "\tConnected: no\n"
        )

        devices = discovery.list_devices()
        assert len(devices) == 2

        a, b = devices
        assert a.mac == "AA:BB:CC:DD:EE:01"
        assert a.connected is True
        assert a.paired is True
        assert a.alias == "JBL"
        assert a.display_name == "JBL"

        assert b.mac == "AA:BB:CC:DD:EE:02"
        assert b.connected is False
        assert b.paired is False


class TestGetDevice:
    def test_falha_quando_device_desconhecido(self, fake_run: dict) -> None:
        fake_run["mapping"][("bluetoothctl", "info", "AA:BB:CC:DD:EE:99")] = _fail(
            "Device not found"
        )
        with pytest.raises(DeviceNotFoundError):
            discovery.get_device("AA:BB:CC:DD:EE:99")

    def test_parse_info_completa(self, fake_run: dict) -> None:
        fake_run["mapping"][("bluetoothctl", "info", "AA:BB:CC:DD:EE:10")] = _ok(
            "Device AA:BB:CC:DD:EE:10\n"
            "\tName: TESTE\n"
            "\tIcon: audio-headphones\n"
            "\tPaired: yes\n"
            "\tTrusted: no\n"
            "\tConnected: yes\n"
        )
        dev = discovery.get_device("AA:BB:CC:DD:EE:10")
        assert dev.icon == "audio-headphones"
        assert dev.trusted is False
        assert dev.paired is True
