"""Wrapper fino em torno de subprocess usado pelo projeto.

Concentrar aqui facilita mock em testes e padroniza logging/timeouts.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    args: Sequence[str],
    *,
    timeout: float | None = 15.0,
    input_text: str | None = None,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Executa um comando capturando stdout/stderr como texto."""

    log.debug("exec: %s", shlex.join(args))
    try:
        completed = subprocess.run(  # noqa: S603 - args é lista controlada
            list(args),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"executável ausente: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"timeout ({timeout}s) executando: {shlex.join(args)}"
        ) from exc

    result = CommandResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if check and not result.ok:
        raise RuntimeError(
            f"comando falhou ({result.returncode}): {shlex.join(args)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result
