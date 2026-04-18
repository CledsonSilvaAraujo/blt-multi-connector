"""Painel web mínimo para gerenciar o blt-multi.

Arquitetura: FastAPI + HTMX (server-side rendered, zero JS bundler).
Registra o sub-comando `web` no Typer da CLI.
"""

from __future__ import annotations

from typing import Annotated

import typer


def register(app: typer.Typer) -> None:
    @app.command()
    def web(
        host: Annotated[str, typer.Option(help="Interface (bind).")] = "127.0.0.1",
        port: Annotated[int, typer.Option(help="Porta.")] = 8765,
        reload: Annotated[bool, typer.Option(help="Auto-reload (dev).")] = False,
    ) -> None:
        """Sobe o painel web em http://host:port/."""

        import uvicorn

        uvicorn.run(
            "blt_multi.web.app:app",
            host=host,
            port=port,
            reload=reload,
            log_level="info",
        )
