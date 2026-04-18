"""Pair / trust / connect / disconnect via `bluetoothctl`.

Manter aqui a interface imperativa. A camada D-Bus pura é usada no
`daemon.py` (para escutar sinais). Para as ações pontuais o subprocess
do bluetoothctl é o caminho mais robusto entre versões.
"""

from __future__ import annotations

import logging
import time

from .discovery import get_device
from .errors import BluetoothStackError
from .models import BluetoothDevice, MacAddress, normalize_mac
from .runner import run

log = logging.getLogger(__name__)

# Timeouts relativamente generosos porque pair/connect BT real pode estourar 10s.
PAIR_TIMEOUT = 30.0
CONNECT_TIMEOUT = 20.0


def _bctl(*args: str, timeout: float = 10.0) -> str:
    result = run(["bluetoothctl", *args], timeout=timeout)
    if not result.ok:
        raise BluetoothStackError(
            f"bluetoothctl {' '.join(args)} falhou: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def pair(mac: MacAddress, *, trust: bool = True) -> BluetoothDevice:
    """Pareia (e opcionalmente marca como trusted) um device.

    Se já estiver pareado, apenas atualiza o estado e aplica trust.
    """

    mac = normalize_mac(mac)
    current = get_device(mac)
    if not current.paired:
        log.info("Pareando %s...", mac)
        _bctl("pair", mac, timeout=PAIR_TIMEOUT)
        # BlueZ às vezes demora para refletir Paired=true.
        _wait_until(lambda: get_device(mac).paired, timeout=PAIR_TIMEOUT)
    else:
        log.info("%s já estava pareado.", mac)

    if trust:
        log.info("Marcando %s como trusted...", mac)
        _bctl("trust", mac)

    return get_device(mac)


def unpair(mac: MacAddress) -> None:
    mac = normalize_mac(mac)
    log.info("Removendo %s...", mac)
    _bctl("remove", mac)


def connect(mac: MacAddress) -> BluetoothDevice:
    mac = normalize_mac(mac)
    current = get_device(mac)
    if current.connected:
        log.info("%s já está conectado.", mac)
        return current

    log.info("Conectando %s...", mac)
    _bctl("connect", mac, timeout=CONNECT_TIMEOUT)
    _wait_until(lambda: get_device(mac).connected, timeout=CONNECT_TIMEOUT)
    return get_device(mac)


def disconnect(mac: MacAddress) -> BluetoothDevice:
    mac = normalize_mac(mac)
    log.info("Desconectando %s...", mac)
    _bctl("disconnect", mac)
    _wait_until(lambda: not get_device(mac).connected, timeout=10.0)
    return get_device(mac)


def _wait_until(predicate, *, timeout: float, interval: float = 0.25) -> None:
    """Poll simples de uma condição. Deixa a última tentativa lançar se falhar."""

    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001 - propagamos ao fim do timeout
            last_exc = exc
        time.sleep(interval)
    if last_exc is not None:
        raise last_exc
    raise TimeoutError(f"Condição não satisfeita em {timeout:.1f}s.")
