"""Hierarquia de exceções do projeto."""

from __future__ import annotations


class BltMultiError(Exception):
    """Erro base do blt-multi-connector."""


class BluetoothStackError(BltMultiError):
    """Problemas com BlueZ / bluetoothctl / D-Bus."""


class PipeWireError(BltMultiError):
    """Problemas com PipeWire / wpctl / pactl."""


class DeviceNotFoundError(BltMultiError):
    """Dispositivo solicitado não encontrado."""


class SinkNotFoundError(PipeWireError):
    """Sink PipeWire correspondente ao dispositivo BT não localizado."""


class CalibrationError(BltMultiError):
    """Falha no fluxo de calibração (mic, chirp, correlação)."""
