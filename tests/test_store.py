from __future__ import annotations

from pathlib import Path

from blt_multi.models import DeviceRecord
from blt_multi.store import Store


def test_store_cria_arquivo_atomicamente(tmp_store_path: Path) -> None:
    store = Store.load()
    assert len(store) == 0

    store.upsert(
        DeviceRecord(mac="AA:BB:CC:DD:EE:01", name="Headphone A", latency_offset_ns=10_000_000)
    )
    store.upsert(
        DeviceRecord(mac="AA:BB:CC:DD:EE:02", name="Speaker B", latency_offset_ns=0)
    )
    store.save()

    assert tmp_store_path.exists()
    reopened = Store.load()
    assert len(reopened) == 2
    assert reopened.get("AA:BB:CC:DD:EE:01").latency_offset_ns == 10_000_000  # type: ignore[union-attr]
    assert reopened.get("aa:bb:cc:dd:ee:02") is not None  # normalização


def test_update_offset_cria_record_quando_ausente(tmp_store_path: Path) -> None:
    store = Store.load()
    record = store.update_offset("AA:BB:CC:DD:EE:03", 5_000_000)
    assert record.mac == "AA:BB:CC:DD:EE:03"
    assert record.latency_offset_ns == 5_000_000
    store.save()

    reopened = Store.load()
    assert reopened.get("AA:BB:CC:DD:EE:03").latency_offset_ns == 5_000_000  # type: ignore[union-attr]


def test_upsert_preserva_offset_existente(tmp_store_path: Path) -> None:
    store = Store.load()
    store.upsert(DeviceRecord(mac="AA:BB:CC:DD:EE:04", name="X", latency_offset_ns=42_000_000))
    # novo upsert com offset=0 não deve zerar o salvo
    store.upsert(DeviceRecord(mac="AA:BB:CC:DD:EE:04", name="X renomeado", latency_offset_ns=0))
    record = store.get("AA:BB:CC:DD:EE:04")
    assert record is not None
    assert record.latency_offset_ns == 42_000_000
    assert record.name == "X renomeado"


def test_mark_seen_atualiza_timestamp(tmp_store_path: Path) -> None:
    store = Store.load()
    record = store.mark_seen("AA:BB:CC:DD:EE:05", name="novo")
    assert record.last_seen is not None
    assert record.name == "novo"


def test_remove_devolve_true_quando_existia(tmp_store_path: Path) -> None:
    store = Store.load()
    store.upsert(DeviceRecord(mac="AA:BB:CC:DD:EE:06", name="Y"))
    assert store.remove("AA:BB:CC:DD:EE:06") is True
    assert store.remove("AA:BB:CC:DD:EE:06") is False
