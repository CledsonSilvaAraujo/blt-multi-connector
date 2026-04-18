"""Persistência das configurações por dispositivo em TOML.

Arquivo padrão: `~/.config/blt_multi/devices.toml`. Pode ser sobrescrito via
variável de ambiente `BLT_MULTI_CONFIG` (útil em testes).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

import tomlkit

from .models import DeviceRecord, MacAddress, normalize_mac

log = logging.getLogger(__name__)


def default_config_path() -> Path:
    override = os.environ.get("BLT_MULTI_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "blt_multi" / "devices.toml"


@dataclass(slots=True)
class Settings:
    """Configuração global (separada dos devices)."""

    default_sink_name: str = "blt_multi_combined"
    default_sink_description: str = "BLT Multi Combined"
    sample_rate: int = 48000
    keep_alive: bool = False

    def to_toml_dict(self) -> dict[str, object]:
        return {
            "default_sink_name": self.default_sink_name,
            "default_sink_description": self.default_sink_description,
            "sample_rate": int(self.sample_rate),
            "keep_alive": bool(self.keep_alive),
        }

    @classmethod
    def from_toml_dict(cls, data: dict[str, object]) -> Settings:
        return cls(
            default_sink_name=str(data.get("default_sink_name", "blt_multi_combined")),
            default_sink_description=str(
                data.get("default_sink_description", "BLT Multi Combined")
            ),
            sample_rate=int(data.get("sample_rate", 48000) or 48000),
            keep_alive=bool(data.get("keep_alive", False)),
        )


@dataclass
class Store:
    """Gerencia leitura/escrita atômica do TOML + cache em memória."""

    path: Path = field(default_factory=default_config_path)
    settings: Settings = field(default_factory=Settings)
    _devices: dict[MacAddress, DeviceRecord] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False)

    # -- Ciclo de vida ----------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> Store:
        path = path or default_config_path()
        store = cls(path=path)
        store._read_from_disk()
        return store

    def _read_from_disk(self) -> None:
        with self._lock:
            if not self.path.exists():
                log.info("Store vazio (arquivo %s ainda não existe).", self.path)
                return

            try:
                doc = tomlkit.parse(self.path.read_text(encoding="utf-8"))
            except tomlkit.exceptions.TOMLKitError as exc:
                raise ValueError(f"TOML inválido em {self.path}: {exc}") from exc

            settings_data = doc.get("settings") or {}
            if isinstance(settings_data, dict):
                self.settings = Settings.from_toml_dict(dict(settings_data))

            devices_data = doc.get("devices") or []
            self._devices.clear()
            if isinstance(devices_data, list):
                for raw in devices_data:
                    if not isinstance(raw, dict):
                        continue
                    record = DeviceRecord.from_toml_dict(dict(raw))
                    self._devices[record.mac] = record

            log.debug("Store carregado: %d devices.", len(self._devices))

    def save(self) -> None:
        """Grava atomicamente (rename em cima do arquivo final)."""

        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            doc = tomlkit.document()
            doc.add(tomlkit.comment("Gerado automaticamente por blt-multi. Edite com cuidado."))
            doc["settings"] = self.settings.to_toml_dict()

            devices_array = tomlkit.aot()
            for record in sorted(self._devices.values(), key=lambda d: d.mac):
                devices_array.append(record.to_toml_dict())
            doc["devices"] = devices_array

            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
            os.replace(tmp_path, self.path)
            log.debug("Store salvo em %s (%d devices).", self.path, len(self._devices))

    # -- API de devices ---------------------------------------------------

    def __contains__(self, mac: str) -> bool:
        return normalize_mac(mac) in self._devices

    def __iter__(self) -> Iterator[DeviceRecord]:
        with self._lock:
            return iter(list(self._devices.values()))

    def __len__(self) -> int:
        return len(self._devices)

    def get(self, mac: str) -> DeviceRecord | None:
        return self._devices.get(normalize_mac(mac))

    def list(self) -> list[DeviceRecord]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: d.name.lower())

    def upsert(self, record: DeviceRecord) -> DeviceRecord:
        with self._lock:
            record.mac = normalize_mac(record.mac)
            existing = self._devices.get(record.mac)
            if existing is not None:
                # Preserva offset quando o caller não explicita — só sobrescreve
                # se vier um offset != 0 ou campo explicitamente alterado.
                if record.latency_offset_ns == 0 and existing.latency_offset_ns != 0:
                    record.latency_offset_ns = existing.latency_offset_ns
                if record.volume is None:
                    record.volume = existing.volume
            self._devices[record.mac] = record
            return record

    def update_offset(self, mac: str, offset_ns: int) -> DeviceRecord:
        with self._lock:
            mac = normalize_mac(mac)
            record = self._devices.get(mac)
            if record is None:
                record = DeviceRecord(mac=mac, name=mac)
                self._devices[mac] = record
            record.latency_offset_ns = int(offset_ns)
            return record

    def mark_seen(self, mac: str, name: str | None = None) -> DeviceRecord:
        with self._lock:
            mac = normalize_mac(mac)
            record = self._devices.get(mac)
            now = datetime.now(UTC)
            if record is None:
                record = DeviceRecord(mac=mac, name=name or mac, last_seen=now)
                self._devices[mac] = record
            else:
                record.last_seen = now
                if name and record.name in {"", record.mac}:
                    record.name = name
            return record

    def remove(self, mac: str) -> bool:
        with self._lock:
            return self._devices.pop(normalize_mac(mac), None) is not None
