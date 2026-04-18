from __future__ import annotations

from datetime import UTC, datetime

import pytest

from blt_multi.models import (
    DeviceRecord,
    is_valid_mac,
    mac_to_pw_address,
    normalize_mac,
)


class TestMacHelpers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
            ("AA_BB_CC_DD_EE_FF", "AA:BB:CC:DD:EE:FF"),
            ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
            ("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"),
        ],
    )
    def test_normalize_mac_aceita_formatos(self, raw: str, expected: str) -> None:
        assert normalize_mac(raw) == expected

    def test_normalize_mac_rejeita_invalido(self) -> None:
        with pytest.raises(ValueError):
            normalize_mac("not-a-mac")

    def test_is_valid_mac(self) -> None:
        assert is_valid_mac("AA:BB:CC:DD:EE:FF")
        assert not is_valid_mac("ZZ:BB:CC:DD:EE:FF")

    def test_mac_to_pw_address(self) -> None:
        assert mac_to_pw_address("aa:bb:cc:dd:ee:ff") == "AA_BB_CC_DD_EE_FF"


class TestDeviceRecord:
    def test_roundtrip_toml_dict(self) -> None:
        original = DeviceRecord(
            mac="AA:BB:CC:DD:EE:FF",
            name="Fones JBL",
            latency_offset_ns=25_500_000,
            volume=0.8,
            enabled=True,
            last_seen=datetime(2026, 4, 1, 12, 34, 56, tzinfo=UTC),
            notes="testes",
        )
        data = original.to_toml_dict()
        restored = DeviceRecord.from_toml_dict(data)
        assert restored.mac == original.mac
        assert restored.name == original.name
        assert restored.latency_offset_ns == original.latency_offset_ns
        assert restored.volume == original.volume
        assert restored.enabled == original.enabled
        assert restored.last_seen == original.last_seen
        assert restored.notes == original.notes

    def test_from_toml_dict_aceita_campos_ausentes(self) -> None:
        restored = DeviceRecord.from_toml_dict({"mac": "AA:BB:CC:DD:EE:FF"})
        assert restored.mac == "AA:BB:CC:DD:EE:FF"
        assert restored.latency_offset_ns == 0
        assert restored.volume is None
        assert restored.enabled is True
        assert restored.last_seen is None
