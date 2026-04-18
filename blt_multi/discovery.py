"""Descoberta de dispositivos Bluetooth via `bluetoothctl`.

Optou-se por subprocess em vez de D-Bus puro aqui porque `bluetoothctl` já
expõe um formato estável e fácil de parsear e funciona em qualquer distro
com BlueZ, sem depender de permissões D-Bus adicionais.
"""

from __future__ import annotations

import logging
import re
import time

from .errors import BluetoothStackError, DeviceNotFoundError
from .models import BluetoothDevice, MacAddress, normalize_mac
from .runner import run

log = logging.getLogger(__name__)

_DEVICE_LINE_RE = re.compile(r"^Device\s+([0-9A-F:]{17})\s+(.*)$", re.IGNORECASE)
_INFO_KEY_RE = re.compile(r"^\s*([A-Za-z ]+):\s*(.*)$")


def _ensure_bluetoothctl() -> None:
    probe = run(["bluetoothctl", "--version"], timeout=5.0)
    if not probe.ok:
        raise BluetoothStackError(
            "bluetoothctl indisponível. Instale o pacote `bluez` "
            "(setup/install.sh faz isso)."
        )


def list_devices(paired_only: bool = False) -> list[BluetoothDevice]:
    """Devolve devices conhecidos pelo BlueZ (histórico + scan atual)."""

    _ensure_bluetoothctl()
    subcmd = "paired-devices" if paired_only else "devices"
    result = run(["bluetoothctl", subcmd], timeout=5.0)
    if not result.ok:
        raise BluetoothStackError(f"bluetoothctl {subcmd} falhou: {result.stderr}")

    devices: list[BluetoothDevice] = []
    for line in result.stdout.splitlines():
        match = _DEVICE_LINE_RE.match(line.strip())
        if not match:
            continue
        mac = normalize_mac(match.group(1))
        name = match.group(2).strip()
        devices.append(BluetoothDevice(mac=mac, name=name))

    # Hidrata com info detalhada (paired/connected/trusted).
    hydrated: list[BluetoothDevice] = []
    for device in devices:
        try:
            hydrated.append(get_device(device.mac))
        except DeviceNotFoundError:
            hydrated.append(device)
    return hydrated


def get_device(mac: MacAddress) -> BluetoothDevice:
    """Retorna o estado detalhado de um device a partir de `bluetoothctl info`."""

    mac = normalize_mac(mac)
    result = run(["bluetoothctl", "info", mac], timeout=5.0)
    if not result.ok:
        raise DeviceNotFoundError(f"Device {mac} desconhecido pelo BlueZ.")

    info: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = _INFO_KEY_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip().lower()
        info[key] = match.group(2).strip()

    if not info:
        raise DeviceNotFoundError(f"Device {mac} sem dados retornados.")

    def flag(key: str) -> bool:
        return info.get(key, "").lower() == "yes"

    return BluetoothDevice(
        mac=mac,
        name=info.get("name", mac),
        paired=flag("paired"),
        trusted=flag("trusted"),
        connected=flag("connected"),
        alias=info.get("alias") or None,
        icon=info.get("icon") or None,
    )


def scan(duration: float = 8.0) -> list[BluetoothDevice]:
    """Liga o scan por `duration` segundos e devolve a lista completa.

    Usa `bluetoothctl --timeout` quando disponível para rodar de forma não
    interativa. Em versões antigas do BlueZ, o flag é ignorado e o scan
    continua ativo — por isso forçamos um `scan off` depois.
    """

    _ensure_bluetoothctl()
    log.info("Iniciando scan BT por %.1fs...", duration)
    timeout_value = max(int(duration) + 5, 10)
    # bluetoothctl --timeout executa o comando seguinte e sai ao fim do timeout.
    run(
        ["bluetoothctl", "--timeout", str(int(duration)), "scan", "on"],
        timeout=float(timeout_value),
    )
    # Garantia: desliga scan se ainda estiver ativo.
    run(["bluetoothctl", "scan", "off"], timeout=5.0)
    # Pequena folga para o BlueZ popular propriedades.
    time.sleep(0.5)
    return list_devices(paired_only=False)
