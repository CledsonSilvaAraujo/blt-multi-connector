"""Daemon que mantém o combine-sink sincronizado conforme devices BT
conectam/desconectam.

Escuta D-Bus do BlueZ (`org.bluez`) via `dbus-next` e age em dois sinais:

1. **InterfacesAdded** em `/org/bluez` — um device apareceu (depois de
   pareamento ou reconexão de cold-start).
2. **PropertiesChanged** na interface `org.bluez.Device1` — transição de
   `Connected`.

Quando um MAC conhecido pelo store fica `Connected`, aguardamos o sink
PipeWire aparecer, reaplicamos o `latency-offset` salvo e reconstruímos o
combine-sink. Em desconexão, rebuildamos o combine-sink com os slaves
restantes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Annotated

import typer
from rich.console import Console

from . import sinks
from .errors import BltMultiError
from .models import MacAddress, normalize_mac
from .store import Store

log = logging.getLogger(__name__)
console = Console()

BLUEZ_BUS = "org.bluez"
OBJECT_MANAGER_PATH = "/"
DEVICE_IFACE = "org.bluez.Device1"

# Quanto esperar após "Connected=true" para o sink PipeWire aparecer/estabilizar.
SINK_APPEAR_DELAY_S = 2.5
# Debounce de reconfiguração (múltiplos eventos em sequência).
REBUILD_DEBOUNCE_S = 1.5


class Daemon:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._rebuild_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._bus = None  # set em start()

    async def start(self) -> None:
        try:
            from dbus_next.aio import MessageBus
            from dbus_next.constants import BusType
            from dbus_next.signature import Variant  # noqa: F401 - usado no type hint lá
        except ImportError as exc:  # pragma: no cover
            raise BltMultiError(
                "dbus-next ausente. Rode `uv sync` para instalar."
            ) from exc

        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        # Introspecta o root para conseguir o proxy do ObjectManager.
        introspection = await self._bus.introspect(BLUEZ_BUS, OBJECT_MANAGER_PATH)
        om_obj = self._bus.get_proxy_object(BLUEZ_BUS, OBJECT_MANAGER_PATH, introspection)
        om = om_obj.get_interface("org.freedesktop.DBus.ObjectManager")

        managed = await om.call_get_managed_objects()
        for path, ifaces in managed.items():
            if DEVICE_IFACE in ifaces:
                await self._watch_device(path, ifaces[DEVICE_IFACE])

        om.on_interfaces_added(self._on_interfaces_added)
        om.on_interfaces_removed(self._on_interfaces_removed)

        console.print(
            f"[green]daemon[/green] ativo. devices no store: {len(self.store)}."
        )
        self._schedule_rebuild(reason="startup")

        # Loop principal até receber sinal de parada.
        await self._stop.wait()

        console.print("[yellow]daemon[/yellow] finalizando...")
        self._bus.disconnect()

    def stop(self) -> None:
        self._stop.set()

    # -- Handlers D-Bus ---------------------------------------------------

    def _on_interfaces_added(self, path: str, interfaces: dict) -> None:
        if DEVICE_IFACE not in interfaces:
            return
        props = _variant_dict(interfaces[DEVICE_IFACE])
        asyncio.create_task(self._watch_device(path, props))

    def _on_interfaces_removed(self, path: str, interfaces: list[str]) -> None:
        if DEVICE_IFACE in interfaces:
            log.info("device removido: %s", path)
            self._schedule_rebuild(reason=f"removed {path}")

    async def _watch_device(self, path: str, props: dict) -> None:
        """Instala handler de PropertiesChanged para um Device1 específico."""

        mac = _mac_from_props(props)
        if mac is None or self.store.get(mac) is None:
            return

        try:
            introspection = await self._bus.introspect(BLUEZ_BUS, path)
            obj = self._bus.get_proxy_object(BLUEZ_BUS, path, introspection)
            props_iface = obj.get_interface("org.freedesktop.DBus.Properties")
        except Exception as exc:  # noqa: BLE001
            log.warning("falha introspectando %s: %s", path, exc)
            return

        def on_changed(iface: str, changed: dict, invalidated: list) -> None:
            if iface != DEVICE_IFACE:
                return
            if "Connected" in changed:
                connected = bool(changed["Connected"].value)
                log.info("%s %s", mac, "conectado" if connected else "desconectado")
                if connected:
                    self._schedule_rebuild(reason=f"{mac} connected", delay=SINK_APPEAR_DELAY_S)
                else:
                    self._schedule_rebuild(reason=f"{mac} disconnected")

        props_iface.on_properties_changed(on_changed)
        log.debug("watching %s (%s)", path, mac)

        # Se já entrou conectado, dispara rebuild preventivo.
        if bool(props.get("Connected", False)):
            self._schedule_rebuild(reason=f"{mac} already connected", delay=SINK_APPEAR_DELAY_S)

    # -- Rebuild do combine-sink ------------------------------------------

    def _schedule_rebuild(self, *, reason: str, delay: float = REBUILD_DEBOUNCE_S) -> None:
        if self._rebuild_task and not self._rebuild_task.done():
            self._rebuild_task.cancel()
        self._rebuild_task = asyncio.create_task(
            self._rebuild_after(delay=delay, reason=reason)
        )

    async def _rebuild_after(self, *, delay: float, reason: str) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._rebuild(reason=reason)

    async def _rebuild(self, *, reason: str) -> None:
        # Recarrega store a cada rebuild — permite que mudanças externas
        # (CLI paralela, edição manual do TOML) sejam respeitadas.
        self.store = Store.load(self.store.path)
        log.info("rebuild: %s", reason)

        try:
            active_sinks = {
                s.mac: s.node_name for s in sinks.list_bt_sinks() if s.mac is not None
            }
        except BltMultiError as exc:
            log.warning("pipewire indisponível: %s", exc)
            return

        slaves: list[str] = []
        for record in self.store.list():
            if not record.enabled:
                continue
            if record.mac not in active_sinks:
                continue
            try:
                sinks.set_latency_offset(record.mac, record.latency_offset_ns)
            except BltMultiError as exc:
                log.warning("offset %s: %s", record.mac, exc)
                continue
            slaves.append(active_sinks[record.mac])
            self.store.mark_seen(record.mac, record.name)

        if not slaves:
            if sinks.unload_combined_sink(self.store.settings.default_sink_name):
                log.info("sem devices ativos, combine-sink descarregado.")
            return

        try:
            sinks.ensure_combined_sink(
                self.store.settings.default_sink_name,
                slaves,
                description=self.store.settings.default_sink_description,
                rate=self.store.settings.sample_rate,
                make_default=True,
            )
            self.store.save()
            log.info("combine-sink atualizado com %d slave(s).", len(slaves))
        except BltMultiError as exc:
            log.warning("falha atualizando combine-sink: %s", exc)


def _variant_dict(props: dict) -> dict:
    """Converte dict[str, Variant] -> dict[str, valor python]."""

    out: dict[str, object] = {}
    for k, v in props.items():
        out[k] = v.value if hasattr(v, "value") else v
    return out


def _mac_from_props(props: dict) -> MacAddress | None:
    addr = props.get("Address")
    if addr is None:
        return None
    if hasattr(addr, "value"):
        addr = addr.value
    try:
        return normalize_mac(str(addr))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Registro no Typer
# ---------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    @app.command()
    def daemon(
        verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,  # noqa: ARG001
    ) -> None:
        """Roda o daemon em foreground (usado pelo systemd user unit)."""

        store = Store.load()
        dmn = Daemon(store)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        for sig in (signal.SIGINT, signal.SIGTERM):
            # Windows/WSL não implementam add_signal_handler — ignoramos silenciosamente.
            with contextlib.suppress(NotImplementedError):  # pragma: no cover
                loop.add_signal_handler(sig, dmn.stop)

        try:
            loop.run_until_complete(dmn.start())
        except BltMultiError as exc:
            console.print(f"[red]daemon:[/red] {exc}")
            raise typer.Exit(1) from exc
        finally:
            loop.close()
