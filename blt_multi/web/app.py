"""Aplicação FastAPI do painel blt-multi."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import discovery, pairing, sinks
from ..errors import BltMultiError
from ..models import DeviceRecord, normalize_mac
from ..store import Store

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="blt-multi-connector")

static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _store() -> Store:
    return Store.load()


def _collect_rows() -> list[dict]:
    store = _store()
    try:
        bt_devices = {d.mac: d for d in discovery.list_devices(paired_only=False)}
    except BltMultiError:
        bt_devices = {}
    try:
        bt_sinks = {s.mac: s for s in sinks.list_bt_sinks() if s.mac is not None}
    except BltMultiError:
        bt_sinks = {}

    rows: list[dict] = []
    for record in store.list():
        device = bt_devices.get(record.mac)
        sink = bt_sinks.get(record.mac)
        rows.append(
            {
                "mac": record.mac,
                "name": record.name,
                "offset_ms": record.latency_offset_ns / 1_000_000,
                "enabled": record.enabled,
                "paired": bool(device and device.paired),
                "connected": bool(device and device.connected),
                "has_sink": sink is not None,
                "is_default": bool(sink and sink.default),
                "last_seen": record.last_seen.isoformat(timespec="seconds")
                if record.last_seen
                else None,
            }
        )
    return rows


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    store = _store()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "rows": _collect_rows(),
            "settings": store.settings,
            "default_sink": _safe_default_sink(),
        },
    )


def _safe_default_sink() -> str:
    try:
        return sinks.get_default_sink()
    except BltMultiError:
        return "?"


@app.get("/partials/devices", response_class=HTMLResponse)
async def devices_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_devices.html",
        {"rows": _collect_rows()},
    )


@app.post("/devices/{mac}/connect", response_class=HTMLResponse)
async def connect_device(request: Request, mac: str) -> HTMLResponse:
    try:
        pairing.connect(normalize_mac(mac))
    except BltMultiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await devices_partial(request)


@app.post("/devices/{mac}/disconnect", response_class=HTMLResponse)
async def disconnect_device(request: Request, mac: str) -> HTMLResponse:
    try:
        pairing.disconnect(normalize_mac(mac))
    except BltMultiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await devices_partial(request)


@app.post("/devices/{mac}/offset", response_class=HTMLResponse)
async def set_offset(
    request: Request,
    mac: str,
    offset_ms: float = Form(...),
) -> HTMLResponse:
    mac = normalize_mac(mac)
    offset_ns = int(round(offset_ms * 1_000_000))
    store = _store()
    record = store.update_offset(mac, offset_ns)
    store.save()
    try:
        sinks.set_latency_offset(record.mac, offset_ns)
    except BltMultiError as exc:
        log.warning("offset não aplicado (device provavelmente offline): %s", exc)
    return await devices_partial(request)


@app.post("/devices/{mac}/enabled", response_class=HTMLResponse)
async def toggle_enabled(request: Request, mac: str) -> HTMLResponse:
    mac = normalize_mac(mac)
    store = _store()
    record = store.get(mac)
    if record is None:
        raise HTTPException(status_code=404, detail="device desconhecido")
    record.enabled = not record.enabled
    store.upsert(record)
    store.save()
    return await devices_partial(request)


@app.post("/sync", response_class=HTMLResponse)
async def do_sync(request: Request) -> HTMLResponse:
    store = _store()
    bt_sinks = {s.mac: s.node_name for s in sinks.list_bt_sinks() if s.mac is not None}

    slaves: list[str] = []
    for record in store.list():
        if not record.enabled or record.mac not in bt_sinks:
            continue
        try:
            sinks.set_latency_offset(record.mac, record.latency_offset_ns)
        except BltMultiError as exc:
            log.warning("offset %s falhou: %s", record.mac, exc)
            continue
        slaves.append(bt_sinks[record.mac])

    if slaves:
        try:
            sinks.ensure_combined_sink(
                store.settings.default_sink_name,
                slaves,
                description=store.settings.default_sink_description,
                rate=store.settings.sample_rate,
                make_default=True,
            )
        except BltMultiError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return await devices_partial(request)


@app.post("/unsync")
async def do_unsync() -> RedirectResponse:
    store = _store()
    sinks.unload_combined_sink(store.settings.default_sink_name)
    return RedirectResponse(url="/", status_code=303)


@app.post("/devices/{mac}/forget")
async def forget_device(mac: str) -> RedirectResponse:
    store = _store()
    mac = normalize_mac(mac)
    with contextlib.suppress(BltMultiError):
        pairing.unpair(mac)
    store.remove(mac)
    store.save()
    return RedirectResponse(url="/", status_code=303)


@app.post("/scan")
async def scan_endpoint(duration: float = Form(8.0)) -> dict:
    try:
        devs = discovery.scan(duration=duration)
    except BltMultiError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    store = _store()
    for d in devs:
        if d.paired:
            store.upsert(DeviceRecord(mac=d.mac, name=d.display_name))
    store.save()
    return {
        "found": [
            {
                "mac": d.mac,
                "name": d.display_name,
                "paired": d.paired,
                "connected": d.connected,
            }
            for d in devs
        ]
    }
