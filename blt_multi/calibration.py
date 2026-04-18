"""Calibração do latency-offset por dispositivo.

Dois modos:

- **manual**: UI no terminal (prompt_toolkit) com barra de progresso e teclas
  de seta para ajustar o offset em tempo real enquanto o usuário toca música
  em outra janela. Ao pressionar Enter, salva no store.

- **mic**: toca um chirp curto em cada dispositivo, grava com o microfone
  padrão e calcula o atraso relativo via cross-correlation. O device mais
  lento é a referência (offset=0); os mais rápidos recebem offset positivo
  para alinhar.

O módulo registra os sub-comandos `calibrate manual` e `calibrate mic` no
`typer.Typer` da CLI via a função `register(app)` (chamada em cli.py).
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import pairing, sinks
from .errors import BltMultiError, CalibrationError
from .models import MacAddress, normalize_mac
from .runner import run
from .store import Store

log = logging.getLogger(__name__)
console = Console()
err_console = Console(stderr=True, style="red")

CHIRP_SAMPLE_RATE = 48000
CHIRP_DURATION_S = 0.5
CHIRP_F0 = 100.0
CHIRP_F1 = 4000.0
RECORD_PAD_S = 0.8
MAX_REASONABLE_DELAY_S = 2.0  # rejeita picos impossíveis (ruído)


# ---------------------------------------------------------------------------
# Manual (slider via prompt_toolkit)
# ---------------------------------------------------------------------------


def manual_calibrate(mac: MacAddress, store: Store, step_ms: float = 2.0) -> None:
    """Abre UI interativa para ajustar offset com setas <-/-> ou +/-.

    Q/Enter: salvar e sair. Esc: cancelar.
    """

    try:
        from prompt_toolkit import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
    except ImportError as exc:  # pragma: no cover
        raise CalibrationError(
            "prompt_toolkit ausente. Rode `uv sync` para instalar as deps."
        ) from exc

    mac = normalize_mac(mac)
    record = store.get(mac)
    if record is None:
        raise CalibrationError(f"{mac} não está no store. Pareie antes.")

    # Estado local do slider. Persistimos só ao sair com Enter.
    current_ms = record.latency_offset_ns / 1_000_000

    def apply_live() -> None:
        try:
            sinks.set_latency_offset(mac, int(round(current_ms * 1_000_000)))
        except BltMultiError as exc:
            log.warning("não foi possível aplicar offset ao vivo: %s", exc)

    kb = KeyBindings()

    @kb.add("right")
    @kb.add("+")
    @kb.add("l")
    def _(event) -> None:  # noqa: ANN001 - assinatura do PT
        nonlocal current_ms
        current_ms += step_ms
        apply_live()

    @kb.add("left")
    @kb.add("-")
    @kb.add("h")
    def _(event) -> None:  # noqa: ANN001
        nonlocal current_ms
        current_ms -= step_ms
        apply_live()

    @kb.add("up")
    @kb.add("pageup")
    def _(event) -> None:  # noqa: ANN001
        nonlocal current_ms
        current_ms += step_ms * 5
        apply_live()

    @kb.add("down")
    @kb.add("pagedown")
    def _(event) -> None:  # noqa: ANN001
        nonlocal current_ms
        current_ms -= step_ms * 5
        apply_live()

    @kb.add("0")
    def _(event) -> None:  # noqa: ANN001
        nonlocal current_ms
        current_ms = 0.0
        apply_live()

    @kb.add("enter")
    @kb.add("q")
    def _(event) -> None:  # noqa: ANN001
        event.app.exit(result="save")

    @kb.add("escape")
    @kb.add("c-c")
    def _(event) -> None:  # noqa: ANN001
        event.app.exit(result="cancel")

    def render() -> str:
        bar_width = 40
        # mapa -200..+200 ms para a barra
        clamped = max(-200.0, min(200.0, current_ms))
        pos = int(((clamped + 200.0) / 400.0) * bar_width)
        bar = "[" + "─" * pos + "●" + "─" * (bar_width - pos - 1) + "]"
        return (
            f"\n  Calibração manual de {record.name} ({record.mac})\n\n"
            f"  Offset atual: {current_ms:+7.1f} ms    {bar}\n\n"
            "  ←/h/-   diminuir {step} ms\n"
            "  →/l/+   aumentar {step} ms\n"
            "  ↑/↓     pular 5x ({big} ms)\n"
            "  0       zerar\n"
            "  Enter/q salvar e sair\n"
            "  Esc     cancelar\n"
        ).format(step=step_ms, big=step_ms * 5)

    body = Window(content=FormattedTextControl(render), always_hide_cursor=True)
    layout = Layout(HSplit([body]))
    app_ui = Application(layout=layout, key_bindings=kb, full_screen=False, refresh_interval=0.1)

    # Aplica o valor atual antes para o usuário ouvir imediatamente.
    apply_live()
    result = app_ui.run()
    if result == "save":
        store.update_offset(mac, int(round(current_ms * 1_000_000)))
        store.save()
        console.print(
            f"[green]✓[/green] offset salvo para {record.name}: {current_ms:+.1f} ms"
        )
    else:
        # Restaura o valor que estava salvo antes.
        with contextlib.suppress(BltMultiError):
            sinks.set_latency_offset(mac, record.latency_offset_ns)
        console.print("[yellow]cancelado[/yellow] (offset no PW restaurado).")


# ---------------------------------------------------------------------------
# Mic-based (chirp + cross-correlation)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DelayMeasurement:
    mac: MacAddress
    name: str
    delay_s: float
    confidence: float  # magnitude normalizada do pico da correlação


def _generate_chirp(
    sample_rate: int = CHIRP_SAMPLE_RATE, duration: float = CHIRP_DURATION_S
):
    import numpy as np  # import local para manter import leve do pacote
    from scipy.signal import chirp as scipy_chirp

    t = np.linspace(0.0, duration, int(sample_rate * duration), endpoint=False)
    sig = scipy_chirp(t, f0=CHIRP_F0, f1=CHIRP_F1, t1=duration, method="logarithmic")
    # Envelope Hann para evitar click de borda.
    window = np.hanning(len(sig))
    return (sig * window * 0.7).astype("float32")


def _write_wav_float32(path: Path, data, sample_rate: int) -> None:
    import numpy as np

    samples = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def _measure_one(
    sink_name: str,
    mac: MacAddress,
    name: str,
    reference,
    sample_rate: int,
) -> DelayMeasurement:
    """Mede o atraso de um único device usando mic default."""

    import numpy as np
    try:
        import sounddevice as sd
    except ImportError as exc:  # pragma: no cover
        raise CalibrationError(
            "sounddevice indisponível. Instale libportaudio2 + `uv sync`."
        ) from exc
    from scipy.signal import correlate

    total_duration = len(reference) / sample_rate + RECORD_PAD_S
    n_frames = int(total_duration * sample_rate)

    log.info("Gravando %.2fs para medir %s (%s)...", total_duration, name, mac)
    try:
        recording = sd.rec(
            n_frames,
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocking=False,
        )
    except Exception as exc:  # noqa: BLE001 - API do sounddevice
        raise CalibrationError(f"falha ao iniciar captura: {exc}") from exc

    # Pequena folga para abrir o stream de entrada antes de tocar.
    time.sleep(0.1)

    with tempfile.TemporaryDirectory(prefix="blt_multi_") as tmpdir:
        wav = Path(tmpdir) / "chirp.wav"
        _write_wav_float32(wav, reference, sample_rate)
        play = run(
            ["paplay", f"--device={sink_name}", str(wav)],
            timeout=CHIRP_DURATION_S + 5.0,
        )
        if not play.ok:
            sd.stop()
            raise CalibrationError(
                f"paplay falhou em {sink_name}: {play.stderr.strip()}"
            )

    sd.wait()
    mono = recording.flatten().astype("float32")

    # Normaliza para evitar dominância por DC/ruído baixo.
    mono -= float(mono.mean())
    norm = float(np.linalg.norm(mono))
    if norm > 0:
        mono /= norm

    ref = reference - float(reference.mean())
    ref_norm = float(np.linalg.norm(ref))
    if ref_norm > 0:
        ref = ref / ref_norm

    corr = correlate(mono, ref, mode="valid")
    if corr.size == 0:
        raise CalibrationError("correlação vazia (gravação curta demais?).")
    peak_idx = int(np.argmax(np.abs(corr)))
    peak_val = float(np.abs(corr[peak_idx]))

    delay_s = peak_idx / sample_rate
    if delay_s < 0.0 or delay_s > MAX_REASONABLE_DELAY_S:
        raise CalibrationError(
            f"pico de correlação em {delay_s*1000:.1f} ms é implausível; "
            "microfone pode não estar captando o dispositivo."
        )

    return DelayMeasurement(
        mac=mac,
        name=name,
        delay_s=delay_s,
        confidence=peak_val,
    )


def mic_calibrate(
    macs: list[MacAddress],
    store: Store,
    *,
    repetitions: int = 3,
    reference_mac: MacAddress | None = None,
) -> list[DelayMeasurement]:
    """Mede delays e atualiza offsets no store.

    Algoritmo:
      - Para cada MAC, faz `repetitions` medições e pega a mediana.
      - O device mais lento (maior delay) vira referência (offset=0).
      - Cada outro device recebe offset = delay_referência - delay_device (ns).
        Assim os mais rápidos atrasam para alinhar com o mais lento.

    Pré-requisitos: todos os devices conectados, combine-sink opcional.
    """

    if len(macs) < 1:
        raise CalibrationError("Forneça pelo menos um MAC.")

    normalized = [normalize_mac(m) for m in macs]
    pw_sinks = {s.mac: s.node_name for s in sinks.list_bt_sinks() if s.mac is not None}
    missing = [m for m in normalized if m not in pw_sinks]
    if missing:
        raise CalibrationError(
            "Dispositivos sem sink PW (conecte com `blt-multi connect`): "
            + ", ".join(missing)
        )

    import numpy as np

    reference_signal = _generate_chirp()
    measurements: list[DelayMeasurement] = []

    for mac in normalized:
        record = store.get(mac)
        name = record.name if record else mac
        samples: list[DelayMeasurement] = []
        for i in range(repetitions):
            console.print(f"[cyan]→[/cyan] medindo {name} ({mac}) [{i+1}/{repetitions}]")
            samples.append(
                _measure_one(
                    sink_name=pw_sinks[mac],
                    mac=mac,
                    name=name,
                    reference=reference_signal,
                    sample_rate=CHIRP_SAMPLE_RATE,
                )
            )
            time.sleep(0.3)

        delays = np.array([s.delay_s for s in samples])
        median_delay = float(np.median(delays))
        median_conf = float(np.median([s.confidence for s in samples]))
        measurements.append(
            DelayMeasurement(
                mac=mac,
                name=name,
                delay_s=median_delay,
                confidence=median_conf,
            )
        )
        console.print(
            f"    mediana: {median_delay*1000:.1f} ms (conf {median_conf:.3f})"
        )

    if reference_mac is not None:
        reference_mac = normalize_mac(reference_mac)
        ref = next((m for m in measurements if m.mac == reference_mac), None)
        if ref is None:
            raise CalibrationError(f"Referência {reference_mac} não medida.")
    else:
        ref = max(measurements, key=lambda m: m.delay_s)

    console.print(
        f"[bold]referência:[/bold] {ref.name} ({ref.mac}) com delay "
        f"{ref.delay_s*1000:.1f} ms"
    )

    for m in measurements:
        offset_s = ref.delay_s - m.delay_s
        offset_ns = int(round(offset_s * 1_000_000_000))
        store.update_offset(m.mac, offset_ns)
        try:
            sinks.set_latency_offset(m.mac, offset_ns)
        except BltMultiError as exc:
            err_console.print(f"[yellow]aviso[/yellow] aplicar {m.mac}: {exc}")

    store.save()
    return measurements


# ---------------------------------------------------------------------------
# Registro no Typer
# ---------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    calibrate = typer.Typer(help="Ferramentas de calibração de latência.")

    @calibrate.command("manual")
    def cmd_manual(
        mac: Annotated[str, typer.Argument(help="MAC do dispositivo.")],
        step_ms: Annotated[
            float, typer.Option(help="Incremento por tecla de seta.")
        ] = 2.0,
    ) -> None:
        """Slider interativo de offset. Toque música em outra janela antes."""

        store = Store.load()
        # Conecta preventivamente para o sink existir.
        try:
            pairing.connect(mac)
        except BltMultiError as exc:
            err_console.print(f"[yellow]aviso[/yellow] connect: {exc}")
        try:
            manual_calibrate(mac, store, step_ms=step_ms)
        except BltMultiError as exc:
            err_console.print(f"[red]erro:[/red] {exc}")
            raise typer.Exit(1) from exc

    @calibrate.command("mic")
    def cmd_mic(
        macs: Annotated[
            list[str] | None,
            typer.Argument(help="MACs a calibrar. Vazio = todos os do store conectados."),
        ] = None,
        repetitions: Annotated[int, typer.Option(help="Medições por device.")] = 3,
        reference: Annotated[
            str | None,
            typer.Option(help="MAC que servirá de referência (mais lento). Vazio = auto."),
        ] = None,
    ) -> None:
        """Calibra offsets usando o microfone padrão + chirp."""

        store = Store.load()
        if not macs:
            macs = [r.mac for r in store.list() if r.enabled]
        if not macs:
            err_console.print("[red]erro:[/red] nenhum MAC disponível.")
            raise typer.Exit(1)

        try:
            measurements = mic_calibrate(
                macs=macs,
                store=store,
                repetitions=repetitions,
                reference_mac=reference,
            )
        except BltMultiError as exc:
            err_console.print(f"[red]erro:[/red] {exc}")
            raise typer.Exit(1) from exc

        table = Table(title="Resultados da calibração")
        table.add_column("Nome")
        table.add_column("MAC")
        table.add_column("Delay medido (ms)", justify="right")
        table.add_column("Offset aplicado (ms)", justify="right")
        max_delay = max(m.delay_s for m in measurements)
        for m in measurements:
            offset_ms = (max_delay - m.delay_s) * 1000
            table.add_row(m.name, m.mac, f"{m.delay_s*1000:.1f}", f"{offset_ms:+.1f}")
        console.print(table)

    app.add_typer(calibrate, name="calibrate")
