"""Operações de alto nível sobre sinks do PipeWire via `pactl`.

`pactl` (fornecido por `pulseaudio-utils`) funciona em cima do PipeWire graças
ao `pipewire-pulse`. Escolhemos essa interface por três motivos:

1. `module-combine-sink` já existe e é testado para espelhar um stream em N
   sinks reais, que é exatamente o que precisamos.
2. `set-port-latency-offset` aplica offset em micro-segundos por porta da
   card BT. Esse é o único offset oficialmente suportado (equivalente ao
   `latency offset` que aparece no pavucontrol).
3. Saídas são estáveis entre versões do PipeWire, facilitando parsing.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .errors import PipeWireError, SinkNotFoundError
from .models import MacAddress, PipeWireSink, mac_to_pw_address, normalize_mac
from .runner import run

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Leitura do estado atual
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BTCard:
    """Informação do `bluez_card.XXX` do PipeWire-pulse."""

    index: int
    name: str  # "bluez_card.XX_XX_XX_XX_XX_XX"
    mac: MacAddress
    active_profile: str = ""
    output_ports: list[str] = field(default_factory=list)

    @property
    def has_a2dp(self) -> bool:
        return "a2dp" in self.active_profile.lower()


_CARD_HEADER_RE = re.compile(r"^Card\s+#(\d+)\s*$")
_NAME_RE = re.compile(r"^\s*Name:\s*(\S+)\s*$")
_ACTIVE_PROFILE_RE = re.compile(r"^\s*Active Profile:\s*(.+?)\s*$")
_PORT_LINE_RE = re.compile(r"^\s*([a-zA-Z0-9._:+-]+):\s+[^(]+\(")


def _pactl(args: list[str], *, timeout: float = 10.0, check: bool = True) -> str:
    result = run(["pactl", *args], timeout=timeout)
    if check and not result.ok:
        raise PipeWireError(
            f"pactl {' '.join(args)} falhou ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def get_default_sink() -> str:
    return _pactl(["get-default-sink"]).strip()


def set_default_sink(name: str) -> None:
    _pactl(["set-default-sink", name])


def list_sinks() -> list[PipeWireSink]:
    """Lista todos os sinks (BT ou não) de forma enxuta via `pactl list sinks short`."""

    raw = _pactl(["list", "sinks", "short"]).strip()
    default = ""
    with contextlib.suppress(PipeWireError):
        default = get_default_sink()

    sinks: list[PipeWireSink] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            node_id = int(parts[0])
        except ValueError:
            continue
        node_name = parts[1]
        mac: MacAddress | None = None
        if node_name.startswith("bluez_output."):
            mac_part = node_name.split(".", 2)[1]
            try:
                mac = normalize_mac(mac_part.replace("_", ":"))
            except ValueError:
                mac = None
        sinks.append(
            PipeWireSink(
                node_id=node_id,
                node_name=node_name,
                description=node_name,
                mac=mac,
                default=(node_name == default),
            )
        )
    return sinks


def list_bt_sinks() -> list[PipeWireSink]:
    return [s for s in list_sinks() if s.is_bluetooth]


def find_sink_by_mac(mac: MacAddress) -> PipeWireSink | None:
    mac = normalize_mac(mac)
    for sink in list_bt_sinks():
        if sink.mac == mac:
            return sink
    return None


# ---------------------------------------------------------------------------
# BT cards (usadas para latency offset por porta)
# ---------------------------------------------------------------------------


def list_bt_cards() -> list[BTCard]:
    """Parseia `pactl list cards` extraindo apenas os cards BlueZ."""

    text = _pactl(["list", "cards"])
    cards: list[BTCard] = []
    current_index: int | None = None
    current_name: str = ""
    current_profile: str = ""
    in_ports: bool = False
    in_profiles: bool = False
    output_ports: list[str] = []

    def flush() -> None:
        nonlocal current_index, current_name, current_profile, output_ports, in_ports, in_profiles
        if current_index is not None and current_name.startswith("bluez_card."):
            mac_part = current_name.split(".", 1)[1]
            try:
                mac = normalize_mac(mac_part.replace("_", ":"))
                cards.append(
                    BTCard(
                        index=current_index,
                        name=current_name,
                        mac=mac,
                        active_profile=current_profile,
                        output_ports=list(output_ports),
                    )
                )
            except ValueError:
                log.debug("Card com nome inesperado: %r", current_name)
        current_index = None
        current_name = ""
        current_profile = ""
        output_ports = []
        in_ports = False
        in_profiles = False

    for line in text.splitlines():
        header = _CARD_HEADER_RE.match(line)
        if header:
            flush()
            current_index = int(header.group(1))
            continue

        if current_index is None:
            continue

        name = _NAME_RE.match(line)
        if name and not current_name:
            current_name = name.group(1)
            continue

        profile = _ACTIVE_PROFILE_RE.match(line)
        if profile:
            current_profile = profile.group(1)
            continue

        stripped = line.strip()
        if stripped.startswith("Ports:"):
            in_ports = True
            in_profiles = False
            continue
        if stripped.startswith("Profiles:"):
            in_ports = False
            in_profiles = True
            continue

        if in_ports:
            # Formato do pactl: "\tPorts:" (1 tab), portas em "\t\t<nome>:" (2 tabs),
            # detalhes em "\t\t\t..." (3 tabs).
            if line.startswith("\t\t\t") or line.startswith("            "):
                continue  # linha de detalhe da porta, ignorar
            if line.startswith("\t\t") or line.startswith("        "):
                match = _PORT_LINE_RE.match(line)
                if match:
                    port_name = match.group(1)
                    if "output" in port_name.lower():
                        output_ports.append(port_name)

    flush()
    return cards


def find_bt_card(mac: MacAddress) -> BTCard | None:
    mac = normalize_mac(mac)
    for card in list_bt_cards():
        if card.mac == mac:
            return card
    return None


# ---------------------------------------------------------------------------
# Latency offset por dispositivo
# ---------------------------------------------------------------------------


def set_latency_offset(mac: MacAddress, offset_ns: int) -> None:
    """Aplica `offset_ns` (nanossegundos) em todas as portas de saída do card BT.

    `pactl set-port-latency-offset` recebe microssegundos. Valor positivo
    atrasa a saída daquela porta (usado para alinhar o device mais rápido
    com o mais lento).
    """

    mac = normalize_mac(mac)
    card = find_bt_card(mac)
    if card is None:
        raise SinkNotFoundError(
            f"Card BT {mac} não encontrado. O dispositivo precisa estar conectado."
        )
    if not card.output_ports:
        raise SinkNotFoundError(
            f"Card {card.name} sem portas de saída detectadas (profile ativo: {card.active_profile})."
        )

    offset_us = int(round(offset_ns / 1000))
    for port in card.output_ports:
        log.info("Aplicando offset %d us em %s / %s", offset_us, card.name, port)
        _pactl(["set-port-latency-offset", card.name, port, str(offset_us)])


def get_latency_offsets(mac: MacAddress) -> dict[str, int]:
    """Retorna offset atual (em microssegundos) por porta de saída do card BT.

    Útil para diagnósticos e testes. Parseamos `pactl list cards` procurando
    `latency offset: N usec`.
    """

    mac = normalize_mac(mac)
    card = find_bt_card(mac)
    if card is None:
        raise SinkNotFoundError(f"Card BT {mac} não encontrado.")

    text = _pactl(["list", "cards"])
    result: dict[str, int] = {}
    in_target = False
    current_port: str | None = None
    for line in text.splitlines():
        header = _CARD_HEADER_RE.match(line)
        if header:
            in_target = False
            current_port = None
            continue
        name_match = _NAME_RE.match(line)
        if name_match:
            in_target = name_match.group(1) == card.name
            continue
        if not in_target:
            continue
        stripped = line.strip()
        port_match = _PORT_LINE_RE.match(line)
        if port_match:
            current_port = port_match.group(1)
        if current_port and "latency offset:" in stripped:
            m = re.search(r"latency offset:\s*(-?\d+)\s*usec", stripped)
            if m:
                result[current_port] = int(m.group(1))
    return result


# ---------------------------------------------------------------------------
# Combined sink (espelhamento)
# ---------------------------------------------------------------------------


def find_module_id_for_combined(sink_name: str) -> int | None:
    raw = _pactl(["list", "modules", "short"]).strip()
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        if parts[1] != "module-combine-sink":
            continue
        args = parts[2] if len(parts) >= 3 else ""
        if f"sink_name={sink_name}" in args:
            try:
                return int(parts[0])
            except ValueError:
                return None
    return None


def create_combined_sink(
    sink_name: str,
    slave_sink_names: Iterable[str],
    *,
    description: str = "BLT Multi Combined",
    rate: int = 48000,
) -> int:
    """Carrega `module-combine-sink` unificando os slaves informados.

    Retorna o `module_id` (necessário para unload posterior). Se já existir
    um combine-sink com esse `sink_name`, ele é descarregado antes para
    refletir a nova lista de slaves.
    """

    slaves = [s for s in slave_sink_names if s]
    if not slaves:
        raise PipeWireError("Nenhum sink BT conectado para combinar.")

    existing = find_module_id_for_combined(sink_name)
    if existing is not None:
        log.info("Combined sink %s já existia (mod %d); recarregando.", sink_name, existing)
        unload_module(existing)

    # Escape do espaço em description (pactl exige formato key=value sem espaços).
    safe_desc = description.replace(" ", r"\ ")
    args = [
        "load-module",
        "module-combine-sink",
        f"sink_name={sink_name}",
        f"slaves={','.join(slaves)}",
        f"sink_properties=device.description={safe_desc}",
        f"rate={rate}",
        "channels=2",
    ]
    raw = _pactl(args).strip()
    try:
        return int(raw.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise PipeWireError(f"load-module não retornou module id: {raw!r}") from exc


def ensure_combined_sink(
    sink_name: str,
    slave_sink_names: Iterable[str],
    *,
    description: str = "BLT Multi Combined",
    rate: int = 48000,
    make_default: bool = True,
) -> int:
    """Cria/atualiza o combined sink e opcionalmente define como padrão."""

    module_id = create_combined_sink(
        sink_name,
        slave_sink_names,
        description=description,
        rate=rate,
    )
    if make_default:
        try:
            set_default_sink(sink_name)
        except PipeWireError as exc:
            log.warning("Não foi possível definir sink padrão: %s", exc)
    return module_id


def unload_combined_sink(sink_name: str) -> bool:
    module_id = find_module_id_for_combined(sink_name)
    if module_id is None:
        return False
    unload_module(module_id)
    return True


def unload_module(module_id: int) -> None:
    _pactl(["unload-module", str(module_id)])


# ---------------------------------------------------------------------------
# Helpers de mais alto nível
# ---------------------------------------------------------------------------


def bt_sink_name_for(mac: MacAddress) -> str:
    """Convenção PipeWire: `bluez_output.XX_XX_XX_XX_XX_XX.a2dp-sink` (sufixo varia)."""

    return f"bluez_output.{mac_to_pw_address(mac)}"


def resolve_bt_sink_names(macs: Iterable[MacAddress]) -> list[str]:
    """Dado um iterável de MACs, devolve os `node_name` reais atualmente conectados.

    Útil para alimentar `create_combined_sink`: o sufixo após o MAC
    (`a2dp-sink`, `a2dp-sink-sbc`, ...) varia conforme o codec, então não
    podemos hardcode.
    """

    wanted = {normalize_mac(m) for m in macs}
    existing = {s.mac: s.node_name for s in list_bt_sinks() if s.mac is not None}
    return [existing[m] for m in wanted if m in existing]
