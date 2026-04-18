"""Tipos e utilitários compartilhados entre os módulos."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

MacAddress = str

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def is_valid_mac(value: str) -> bool:
    return bool(MAC_RE.match(value.upper()))


def normalize_mac(value: str) -> MacAddress:
    """Aceita formatos com _ ou -, devolve sempre XX:XX:XX:XX:XX:XX em maiúsculas."""

    candidate = value.replace("_", ":").replace("-", ":").upper()
    if not is_valid_mac(candidate):
        raise ValueError(f"MAC inválido: {value!r}")
    return candidate


def mac_to_pw_address(mac: MacAddress) -> str:
    """PipeWire usa `XX_XX_XX_XX_XX_XX` em nomes de nó (bluez_output.XX_..._.a2dp-sink)."""

    return normalize_mac(mac).replace(":", "_")


DeviceState = Literal["unknown", "paired", "connected", "disconnected"]


@dataclass(slots=True)
class BluetoothDevice:
    """Representação leve de um dispositivo BT, como visto pelo BlueZ."""

    mac: MacAddress
    name: str
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    alias: str | None = None
    icon: str | None = None

    @property
    def display_name(self) -> str:
        return self.alias or self.name or self.mac

    @property
    def state(self) -> DeviceState:
        if self.connected:
            return "connected"
        if self.paired:
            return "paired"
        return "unknown"


@dataclass(slots=True)
class DeviceRecord:
    """Estado persistido em ~/.config/blt_multi/devices.toml por MAC."""

    mac: MacAddress
    name: str
    latency_offset_ns: int = 0
    volume: float | None = None
    enabled: bool = True
    last_seen: datetime | None = None
    notes: str = ""

    def to_toml_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "mac": self.mac,
            "name": self.name,
            "latency_offset_ns": int(self.latency_offset_ns),
            "enabled": bool(self.enabled),
            "notes": self.notes,
        }
        if self.volume is not None:
            data["volume"] = float(self.volume)
        if self.last_seen is not None:
            data["last_seen"] = self.last_seen.isoformat()
        return data

    @classmethod
    def from_toml_dict(cls, data: dict[str, object]) -> DeviceRecord:
        mac = normalize_mac(str(data["mac"]))
        last_seen_raw = data.get("last_seen")
        last_seen: datetime | None = None
        if isinstance(last_seen_raw, str):
            try:
                last_seen = datetime.fromisoformat(last_seen_raw)
            except ValueError:
                last_seen = None
        elif isinstance(last_seen_raw, datetime):
            last_seen = last_seen_raw

        volume_raw = data.get("volume")
        volume = float(volume_raw) if isinstance(volume_raw, (int, float)) else None

        return cls(
            mac=mac,
            name=str(data.get("name", mac)),
            latency_offset_ns=int(data.get("latency_offset_ns", 0) or 0),
            volume=volume,
            enabled=bool(data.get("enabled", True)),
            last_seen=last_seen,
            notes=str(data.get("notes", "")),
        )


@dataclass(slots=True)
class PipeWireSink:
    """Representação mínima de um sink BT exposto pelo PipeWire."""

    node_id: int
    node_name: str
    description: str
    mac: MacAddress | None
    latency_offset_ns: int = 0
    default: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_bluetooth(self) -> bool:
        return self.node_name.startswith("bluez_output.") or self.mac is not None
