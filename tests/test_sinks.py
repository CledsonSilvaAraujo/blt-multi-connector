from __future__ import annotations

from unittest.mock import patch

import pytest

from blt_multi import sinks
from blt_multi.errors import PipeWireError, SinkNotFoundError
from blt_multi.runner import CommandResult


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(args=(), returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> CommandResult:
    return CommandResult(args=(), returncode=1, stdout="", stderr=stderr)


PACTL_CARDS_SAMPLE = """\
Card #42
\tName: bluez_card.AA_BB_CC_DD_EE_01
\tDriver: bluez5
\tOwner Module: n/a
\tActive Profile: a2dp-sink-sbc
\tPorts:
\t\theadphone-output: Headphone (type: Headphones, priority: 200, latency offset: 25000 usec, availability unknown)
\t\t\tPart of profile(s): a2dp-sink-sbc, a2dp-sink-aac
\t\tspeaker-output: Speaker (type: Speaker, priority: 100, latency offset: 0 usec, availability unknown)
\tProfiles:
\t\ta2dp-sink-sbc: ...
Card #43
\tName: alsa_card.pci-0000_00_1f.3
\tDriver: alsa
\tActive Profile: off
"""


class TestListBTCards:
    def test_parseia_card_bt(self) -> None:
        with patch("blt_multi.sinks.run") as mock_run:
            mock_run.return_value = _ok(PACTL_CARDS_SAMPLE)
            cards = sinks.list_bt_cards()

        assert len(cards) == 1
        card = cards[0]
        assert card.name == "bluez_card.AA_BB_CC_DD_EE_01"
        assert card.mac == "AA:BB:CC:DD:EE:01"
        assert card.active_profile == "a2dp-sink-sbc"
        assert card.has_a2dp
        assert "headphone-output" in card.output_ports
        assert "speaker-output" in card.output_ports


class TestSetLatencyOffset:
    def test_aplica_em_todas_as_portas_output(self) -> None:
        def fake(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[:3] == ["pactl", "list", "cards"]:
                return _ok(PACTL_CARDS_SAMPLE)
            return _ok("")

        with patch("blt_multi.sinks.run", side_effect=fake) as mock_run:
            sinks.set_latency_offset("AA:BB:CC:DD:EE:01", 40_000_000)

        called = [tuple(c.args[0]) for c in mock_run.call_args_list]
        assert (
            "pactl",
            "set-port-latency-offset",
            "bluez_card.AA_BB_CC_DD_EE_01",
            "headphone-output",
            "40000",
        ) in called
        assert (
            "pactl",
            "set-port-latency-offset",
            "bluez_card.AA_BB_CC_DD_EE_01",
            "speaker-output",
            "40000",
        ) in called

    def test_falha_quando_card_ausente(self) -> None:
        with patch("blt_multi.sinks.run") as mock_run:
            mock_run.return_value = _ok("")  # nenhum card
            with pytest.raises(SinkNotFoundError):
                sinks.set_latency_offset("AA:BB:CC:DD:EE:FF", 10_000_000)


class TestCombinedSink:
    def test_load_module_retorna_id(self) -> None:
        def fake(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[:2] == ["pactl", "list"] and args[2] == "modules":
                return _ok("")  # nenhum existente
            if args[:2] == ["pactl", "load-module"]:
                return _ok("1234\n")
            return _ok("")

        with patch("blt_multi.sinks.run", side_effect=fake):
            module_id = sinks.create_combined_sink(
                "blt_multi_combined",
                ["bluez_output.AA_BB_CC_DD_EE_01.a2dp-sink"],
            )
        assert module_id == 1234

    def test_create_recarrega_se_ja_existia(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake(args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(tuple(args))
            if args[:3] == ["pactl", "list", "modules"]:
                return _ok(
                    "100\tmodule-combine-sink\tsink_name=blt_multi_combined slaves=foo\n"
                )
            if args[:2] == ["pactl", "unload-module"]:
                return _ok("")
            if args[:2] == ["pactl", "load-module"]:
                return _ok("200\n")
            return _ok("")

        with patch("blt_multi.sinks.run", side_effect=fake):
            module_id = sinks.create_combined_sink(
                "blt_multi_combined",
                ["bluez_output.AA_BB_CC_DD_EE_02.a2dp-sink"],
            )

        assert module_id == 200
        assert ("pactl", "unload-module", "100") in calls

    def test_falha_sem_slaves(self) -> None:
        with pytest.raises(PipeWireError):
            sinks.create_combined_sink("nada", [])


class TestListSinks:
    def test_parseia_sinks_short(self) -> None:
        def fake(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[:4] == ["pactl", "list", "sinks", "short"]:
                return _ok(
                    "42\tbluez_output.AA_BB_CC_DD_EE_01.a2dp-sink\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n"
                    "43\talsa_output.pci\tPipeWire\ts16le 2ch 48000Hz\tIDLE\n"
                )
            if args[:2] == ["pactl", "get-default-sink"]:
                return _ok("bluez_output.AA_BB_CC_DD_EE_01.a2dp-sink\n")
            return _ok("")

        with patch("blt_multi.sinks.run", side_effect=fake):
            result = sinks.list_sinks()

        assert len(result) == 2
        bt = [s for s in result if s.is_bluetooth][0]
        assert bt.mac == "AA:BB:CC:DD:EE:01"
        assert bt.default is True
