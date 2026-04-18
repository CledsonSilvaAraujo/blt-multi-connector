"""CLI do blt-multi-connector.

Ponto de entrada declarado em pyproject.toml como `blt-multi = blt_multi.cli:app`.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import discovery, pairing, sinks
from .errors import BltMultiError
from .models import DeviceRecord, normalize_mac
from .store import Store

app = typer.Typer(
    help="Espelha o áudio do sistema em múltiplos dispositivos Bluetooth.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()
err_console = Console(stderr=True, style="red")


# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=err_console, rich_tracebacks=True, markup=False)],
    )


@app.callback()
def _root(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Logs em DEBUG.")] = False,
) -> None:
    _configure_logging(verbose)


def _die(message: str, code: int = 1) -> None:
    err_console.print(f"[bold red]erro:[/bold red] {message}")
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# pair
# ---------------------------------------------------------------------------


@app.command()
def pair(
    mac: Annotated[
        str | None,
        typer.Argument(help="MAC do dispositivo. Se omitido, faz scan interativo."),
    ] = None,
    duration: Annotated[float, typer.Option(help="Duração do scan (s).")] = 10.0,
    trust: Annotated[bool, typer.Option(help="Marcar device como trusted após parear.")] = True,
) -> None:
    """Pareia um dispositivo Bluetooth (scan + pair + trust)."""

    try:
        if mac is None:
            console.print(f"[cyan]Scan BT por {duration:.0f}s...[/cyan]")
            devices = discovery.scan(duration=duration)
            if not devices:
                _die("Nenhum dispositivo encontrado no scan.")
            table = Table(title="Dispositivos detectados")
            table.add_column("#", justify="right")
            table.add_column("MAC")
            table.add_column("Nome")
            table.add_column("Pareado")
            table.add_column("Conectado")
            for idx, dev in enumerate(devices, start=1):
                table.add_row(
                    str(idx),
                    dev.mac,
                    dev.display_name,
                    "sim" if dev.paired else "não",
                    "sim" if dev.connected else "não",
                )
            console.print(table)
            choice = typer.prompt(
                "Escolha o # para parear (0 para cancelar)", type=int, default=0
            )
            if choice == 0:
                raise typer.Exit(0)
            if not 1 <= choice <= len(devices):
                _die("Escolha inválida.")
            mac = devices[choice - 1].mac

        mac_norm = normalize_mac(mac)
        device = pairing.pair(mac_norm, trust=trust)

        store = Store.load()
        store.upsert(
            DeviceRecord(
                mac=device.mac,
                name=device.display_name,
            )
        )
        store.save()

        console.print(
            f"[green]OK[/green] {device.display_name} ({device.mac}) pareado "
            f"(trusted={device.trusted})."
        )
    except BltMultiError as exc:
        _die(str(exc))


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


@app.command()
def connect(
    macs: Annotated[
        list[str] | None, typer.Argument(help="MACs a conectar. Vazio = todos do store.")
    ] = None,
    all_stored: Annotated[bool, typer.Option("--all", help="Conectar todos os do store.")] = False,
) -> None:
    """Conecta dispositivos já pareados."""

    store = Store.load()
    if not macs:
        if not all_stored and len(store) == 0:
            _die("Nenhum MAC informado e store vazio.")
        macs = [rec.mac for rec in store.list()]
        if not macs:
            _die("Store vazio. Rode `blt-multi pair` primeiro.")

    failed: list[str] = []
    for mac in macs:
        try:
            device = pairing.connect(mac)
            console.print(f"[green]✓[/green] {device.display_name} ({device.mac}) conectado.")
            store.mark_seen(device.mac, device.display_name)
        except BltMultiError as exc:
            err_console.print(f"[red]✗[/red] {mac}: {exc}")
            failed.append(mac)
    store.save()
    if failed:
        raise typer.Exit(1)


@app.command()
def disconnect(
    macs: Annotated[list[str], typer.Argument(help="MACs a desconectar.")],
) -> None:
    """Desconecta dispositivos (sem remover pareamento)."""

    for mac in macs:
        try:
            device = pairing.disconnect(mac)
            console.print(f"[yellow]○[/yellow] {device.display_name} ({device.mac}) desconectado.")
        except BltMultiError as exc:
            err_console.print(f"[red]✗[/red] {mac}: {exc}")


@app.command()
def forget(
    mac: Annotated[str, typer.Argument(help="MAC do dispositivo a esquecer.")],
) -> None:
    """Remove o pareamento BT e apaga o registro do store."""

    store = Store.load()
    try:
        pairing.unpair(mac)
    except BltMultiError as exc:
        err_console.print(f"[yellow]aviso[/yellow] unpair falhou: {exc}")
    if store.remove(mac):
        store.save()
        console.print(f"[yellow]○[/yellow] Registro de {mac} removido do store.")


# ---------------------------------------------------------------------------
# status / devices
# ---------------------------------------------------------------------------


@app.command()
def status() -> None:
    """Mostra estado combinado: BT devices + sinks PipeWire + store."""

    store = Store.load()

    try:
        bt_devices = discovery.list_devices(paired_only=False)
    except BltMultiError as exc:
        err_console.print(f"[red]bluez:[/red] {exc}")
        bt_devices = []

    try:
        bt_sinks = sinks.list_bt_sinks()
    except BltMultiError as exc:
        err_console.print(f"[red]pipewire:[/red] {exc}")
        bt_sinks = []

    sinks_by_mac = {s.mac: s for s in bt_sinks if s.mac is not None}
    records_by_mac = {r.mac: r for r in store.list()}
    all_macs = set(records_by_mac) | {d.mac for d in bt_devices if d.paired}

    table = Table(title="blt-multi status")
    table.add_column("MAC")
    table.add_column("Nome")
    table.add_column("BlueZ", justify="center")
    table.add_column("Sink PW", justify="center")
    table.add_column("Offset (ms)", justify="right")
    table.add_column("Padrão?", justify="center")

    for mac in sorted(all_macs):
        device = next((d for d in bt_devices if d.mac == mac), None)
        sink = sinks_by_mac.get(mac)
        record = records_by_mac.get(mac)

        name = (
            (device.display_name if device else None)
            or (record.name if record else None)
            or mac
        )

        bluez_state = "—"
        if device:
            if device.connected:
                bluez_state = "[green]conectado[/green]"
            elif device.paired:
                bluez_state = "[yellow]pareado[/yellow]"
            else:
                bluez_state = "visto"

        sink_state = "[green]presente[/green]" if sink else "—"
        offset_ms = (record.latency_offset_ns / 1_000_000) if record else 0.0
        is_default = "✓" if sink and sink.default else ""

        table.add_row(
            mac,
            name,
            bluez_state,
            sink_state,
            f"{offset_ms:+.1f}",
            is_default,
        )

    console.print(table)

    try:
        default = sinks.get_default_sink()
        console.print(f"[dim]default sink: {default}[/dim]")
    except BltMultiError:
        pass


@app.command()
def devices() -> None:
    """Lista os dispositivos registrados no store."""

    store = Store.load()
    if not store.list():
        console.print("[dim]store vazio[/dim]")
        return
    table = Table(title=f"store: {store.path}")
    table.add_column("MAC")
    table.add_column("Nome")
    table.add_column("Offset (ms)", justify="right")
    table.add_column("Volume", justify="right")
    table.add_column("Ativo?")
    table.add_column("Último visto")
    for rec in store.list():
        table.add_row(
            rec.mac,
            rec.name,
            f"{rec.latency_offset_ns / 1_000_000:+.1f}",
            f"{rec.volume:.2f}" if rec.volume is not None else "—",
            "sim" if rec.enabled else "não",
            rec.last_seen.isoformat(timespec="seconds") if rec.last_seen else "—",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# sync / unsync
# ---------------------------------------------------------------------------


@app.command()
def sync(
    only_active: Annotated[
        bool, typer.Option("--only-active/--all", help="Combinar só BTs já conectados.")
    ] = True,
    make_default: Annotated[
        bool, typer.Option("--default/--no-default", help="Definir combine-sink como default.")
    ] = True,
) -> None:
    """Aplica offsets salvos e (re)cria o combine-sink do PipeWire.

    Esta é a operação central. Para cada device registrado:
      1. Aplica `latency_offset_ns` via `pactl set-port-latency-offset`.
      2. Inclui o sink no combine-sink se estiver conectado.
    """

    store = Store.load()
    records = [r for r in store.list() if r.enabled]
    if not records:
        _die("Store vazio ou todos desativados. Rode `blt-multi pair` primeiro.")

    bt_sinks = {s.mac: s.node_name for s in sinks.list_bt_sinks() if s.mac is not None}

    slaves: list[str] = []
    for rec in records:
        offset_ms = rec.latency_offset_ns / 1_000_000
        if rec.mac in bt_sinks:
            try:
                sinks.set_latency_offset(rec.mac, rec.latency_offset_ns)
                console.print(
                    f"[green]✓[/green] {rec.name} ({rec.mac}) offset={offset_ms:+.1f} ms"
                )
                slaves.append(bt_sinks[rec.mac])
                store.mark_seen(rec.mac, rec.name)
            except BltMultiError as exc:
                err_console.print(f"[red]✗[/red] {rec.name}: {exc}")
        else:
            if only_active:
                console.print(
                    f"[dim]- {rec.name} ({rec.mac}) não conectado, pulando.[/dim]"
                )
            else:
                err_console.print(
                    f"[yellow]aviso[/yellow] {rec.name} sem sink PW (conecte antes)."
                )

    if not slaves:
        _die("Nenhum dispositivo BT conectado para combinar.")

    module_id = sinks.ensure_combined_sink(
        store.settings.default_sink_name,
        slaves,
        description=store.settings.default_sink_description,
        rate=store.settings.sample_rate,
        make_default=make_default,
    )
    store.save()
    console.print(
        f"[bold green]OK[/bold green] combine-sink '{store.settings.default_sink_name}' "
        f"(module {module_id}) com {len(slaves)} slave(s)."
    )


@app.command()
def unsync() -> None:
    """Descarrega o combine-sink sem mexer em pareamentos."""

    store = Store.load()
    name = store.settings.default_sink_name
    if sinks.unload_combined_sink(name):
        console.print(f"[yellow]○[/yellow] combine-sink '{name}' descarregado.")
    else:
        console.print(f"[dim]combine-sink '{name}' não estava carregado.[/dim]")


# ---------------------------------------------------------------------------
# offset direto (usado manualmente ou pela calibração)
# ---------------------------------------------------------------------------


@app.command()
def offset(
    mac: Annotated[str, typer.Argument(help="MAC do dispositivo.")],
    ms: Annotated[float, typer.Argument(help="Offset em milissegundos (pode ser negativo).")],
    apply_now: Annotated[
        bool, typer.Option("--apply/--no-apply", help="Aplicar imediatamente no PW.")
    ] = True,
) -> None:
    """Define o latency-offset de um dispositivo e persiste no store."""

    store = Store.load()
    offset_ns = int(round(ms * 1_000_000))
    record = store.update_offset(mac, offset_ns)
    store.save()
    console.print(
        f"[green]✓[/green] {record.name} ({record.mac}) offset salvo = {ms:+.1f} ms "
        f"({offset_ns} ns)."
    )
    if apply_now:
        try:
            sinks.set_latency_offset(record.mac, offset_ns)
            console.print("[dim]Offset aplicado no PipeWire.[/dim]")
        except BltMultiError as exc:
            err_console.print(f"[yellow]aviso[/yellow] não apliquei agora: {exc}")


# ---------------------------------------------------------------------------
# Extensões carregadas de outros módulos (registradas abaixo)
# ---------------------------------------------------------------------------


def _register_optional_commands() -> None:
    # calibrate
    try:
        from .calibration import register as register_calibrate

        register_calibrate(app)
    except ImportError as exc:  # pragma: no cover
        logging.getLogger(__name__).debug("calibration não carregado: %s", exc)

    # daemon
    try:
        from .daemon import register as register_daemon

        register_daemon(app)
    except ImportError as exc:  # pragma: no cover
        logging.getLogger(__name__).debug("daemon não carregado: %s", exc)

    # web
    try:
        from .web import register as register_web

        register_web(app)
    except ImportError as exc:  # pragma: no cover
        logging.getLogger(__name__).debug("web não carregado: %s", exc)


_register_optional_commands()


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        err_console.print("\n[dim]interrompido[/dim]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
