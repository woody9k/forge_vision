"""FastAPI application: HTTP + WebSocket routing over the Runtime.

Run with:  uvicorn forge_vision.server.app:app --port 8347
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from fastapi import (Body, FastAPI, HTTPException, Query, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..devices.base import ConfigurationError
from ..safety import SafetyViolation
from .runtime import Runtime

runtime = Runtime()
app = FastAPI(title="Forge Vision", version="0.3.0")


def _fail(exc: Exception) -> HTTPException:
    code = 400
    if isinstance(exc, SafetyViolation):
        code = 403
    elif isinstance(exc, (KeyError, FileNotFoundError)):
        code = 404
    return HTTPException(status_code=code, detail=str(exc))


# -- dashboard / status ------------------------------------------------------
@app.get("/api/status")
def status():
    return runtime.status()


# -- devices -----------------------------------------------------------------
@app.post("/api/devices/rescan")
def rescan(body: dict = Body(default={})):
    """Probe for hardware without restarting; optional explicit URI."""
    return runtime.rescan_hardware(body.get("uri", ""))


@app.post("/api/devices/{device_id}/connect")
def connect(device_id: str):
    try:
        return runtime.connect(device_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/devices/{device_id}/disconnect")
def disconnect(device_id: str):
    try:
        return runtime.disconnect(device_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/devices/{device_id}/configure")
def configure(device_id: str, cfg: dict = Body(...)):
    try:
        return runtime.configure(device_id, cfg)
    except (ConfigurationError, Exception) as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/devices/{device_id}/tx")
def set_tx(device_id: str, body: dict = Body(...)):
    try:
        return runtime.set_tx(device_id, bool(body.get("enable")),
                              body.get("waveform", ""))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- safety ------------------------------------------------------------------
@app.post("/api/safety/arm")
def arm(body: dict = Body(...)):
    try:
        runtime.safety.arm(body.get("operator", ""), body.get("acknowledgement", ""))
        return runtime.safety.status()
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/safety/disarm")
def disarm():
    runtime.safety.disarm()
    return runtime.safety.status()


@app.post("/api/safety/stop")
def emergency_stop():
    """Emergency stop: disables TX on every device (FR-SAF-003)."""
    return runtime.emergency_stop()


@app.get("/api/safety/checklist")
def checklist():
    return runtime.safety.checklist_status()


@app.post("/api/safety/checklist")
def confirm_checklist(body: dict = Body(...)):
    try:
        if body.get("reset"):
            return runtime.safety.reset_checklist()
        return runtime.safety.confirm_checklist_item(
            body.get("id", ""), bool(body.get("confirmed", True)))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/survey")
def survey(body: dict = Body(...)):
    """Receive-only band occupancy survey. Transmits nothing."""
    try:
        return runtime.band_survey(
            device_id=body.get("device_id", "sim-pluto-0"),
            start_hz=float(body.get("start_hz", 902e6)),
            stop_hz=float(body.get("stop_hz", 928e6)),
            step_hz=float(body.get("step_hz", 2e6)),
            sample_rate_hz=float(body.get("sample_rate_hz", 2.5e6)),
            rx_gain_db=float(body.get("rx_gain_db", 40.0)),
            samples=int(body.get("samples", 65536)),
            name=body.get("name", "band survey"),
            operator=body.get("operator", ""))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/safety/audit")
def audit(n: int = 200):
    return runtime.safety.audit_tail(n)


@app.post("/api/safety/profile")
def set_profile(body: dict = Body(...)):
    name = body.get("profile", "")
    if name not in runtime.safety.limits.frequency_profiles:
        raise HTTPException(404, f"unknown frequency profile: {name}")
    runtime.safety.limits.active_profile = name
    runtime.safety.audit("frequency_profile_changed", profile=name)
    return runtime.safety.status()


# -- calibration -------------------------------------------------------------
@app.get("/api/calibration/{device_id}")
def calibration(device_id: str, waveform: str = ""):
    try:
        return runtime.calibration_status(device_id, waveform)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/calibration/{device_id}/cable_delay")
def cable_delay(device_id: str, body: dict = Body(...)):
    try:
        return runtime.set_cable_delay(device_id, float(body.get("delay_s", 0.0)))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/calibration/{device_id}/background")
def background(device_id: str, body: dict = Body(default={})):
    try:
        return runtime.capture_background(
            device_id, body.get("waveform", "fmcw_bench_56M"),
            int(body.get("chirps", 8)), body.get("operator", ""))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- range lab ---------------------------------------------------------------
@app.post("/api/range/run")
def range_run(body: dict = Body(default={})):
    try:
        return runtime.range_run(
            device_id=body.get("device_id", "sim-pluto-0"),
            waveform_name=body.get("waveform", "fmcw_bench_56M"),
            chirps=int(body.get("chirps", 8)),
            medium=body.get("medium"),
            use_background=bool(body.get("use_background", True)),
            name=body.get("name", "range run"),
            operator=body.get("operator", ""),
            tags=body.get("tags"),
            pipeline_overrides=body.get("pipeline_overrides"),
            parent_id=body.get("parent_id"))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- scan studio -------------------------------------------------------------
@app.post("/api/scan/start")
def scan_start(body: dict = Body(...)):
    try:
        return runtime.scan_start(body.get("device_id", "sim-pluto-0"),
                                  body.get("plan", {}), body.get("operator", ""))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/scan/{scan_id}/point")
def scan_point(scan_id: str, body: dict = Body(...)):
    try:
        return runtime.scan_point(scan_id, float(body["x_m"]),
                                  bool(body.get("operator_override", False)))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/scan/{scan_id}/render")
def scan_render(scan_id: str, interpolate: bool = False,
                remove_mean: bool = False):
    try:
        return runtime.scan_render(scan_id, interpolate, remove_mean)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/scan/{scan_id}/resume")
def scan_resume(scan_id: str):
    try:
        return runtime.scan_resume(scan_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/scan/{scan_id}/finalize")
def scan_finalize(scan_id: str):
    try:
        return runtime.scan_finalize(scan_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- capture / experiments ---------------------------------------------------
@app.post("/api/capture")
def capture(body: dict = Body(default={})):
    try:
        return runtime.record_capture(
            device_id=body.get("device_id", "sim-pluto-0"),
            num_samples=int(body.get("num_samples", 262144)),
            segments=int(body.get("segments", 1)),
            name=body.get("name", "raw capture"),
            operator=body.get("operator", ""),
            waveform_name=body.get("waveform", ""),
            tags=body.get("tags"))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/experiments")
def experiments(query: str = "", tag: str = "", kind: str = ""):
    return runtime.store.list(query=query, tag=tag, kind=kind)


@app.get("/api/experiments/{exp_id}")
def experiment(exp_id: str):
    try:
        manifest = runtime.store.load(exp_id)
        manifest["annotations"] = runtime.store.annotations(exp_id)
        return manifest
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/experiments/{exp_id}/derived/{name}")
def derived(exp_id: str, name: str):
    try:
        return runtime.store.load_derived(exp_id, name)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/experiments/{exp_id}/annotate")
def annotate(exp_id: str, body: dict = Body(...)):
    try:
        return runtime.store.annotate(exp_id, body)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/experiments/{exp_id}/verify")
def verify(exp_id: str):
    try:
        return runtime.store.verify(exp_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/experiments/{exp_id}/replay")
def replay(exp_id: str, body: dict = Body(default={})):
    try:
        return runtime.replay(exp_id, medium=body.get("medium"),
                              pipeline_overrides=body.get("pipeline_overrides"))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/experiments/{exp_id}/export")
def export(exp_id: str):
    try:
        dest = os.path.join(tempfile.gettempdir(), f"forge-vision-{exp_id}.zip")
        runtime.store.export(exp_id, dest)
        return FileResponse(dest, filename=f"{exp_id}.zip",
                            media_type="application/zip")
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/import")
async def import_package(file: UploadFile):
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(await file.read())
            path = tmp.name
        manifest = runtime.store.import_package(path)
        os.unlink(path)
        return manifest
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- RF components / Antenna Lab (FR-RFC-*) -----------------------------------
@app.get("/api/components")
def components(kind: str = ""):
    return runtime.components.list(kind=kind)


@app.post("/api/components")
def create_component(body: dict = Body(...)):
    try:
        return runtime.components.create(
            kind=body.get("kind", "antenna"), name=body.get("name", ""),
            connector=body.get("connector", ""),
            claimed_band=body.get("claimed_band", ""),
            polarization=body.get("polarization", ""),
            notes=body.get("notes", ""),
            nominal_loss_db=body.get("nominal_loss_db"),
            nominal_delay_ns=body.get("nominal_delay_ns"))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/components/{comp_id}")
def component(comp_id: str):
    try:
        return runtime.components.load(comp_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/components/{comp_id}/update")
def update_component(comp_id: str, body: dict = Body(...)):
    try:
        return runtime.components.update(comp_id, body)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/components/{comp_id}/delete")
def delete_component(comp_id: str):
    try:
        runtime.components.delete(comp_id)
        return {"deleted": comp_id}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/components/{comp_id}/vna")
async def import_vna(comp_id: str, file: UploadFile):
    """Import a NanoVNA touchstone (.s1p/.s2p) measurement (FR-RFC-003)."""
    try:
        text = (await file.read()).decode("utf-8", errors="replace")
        return runtime.components.import_vna(comp_id, text,
                                             filename=file.filename or "")
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- simulator ---------------------------------------------------------------
@app.post("/api/sim/{device_id}/caps")
def sim_caps(device_id: str, body: dict = Body(...)):
    """Emulate a specific hardware class (pluto_plus | pluto_rev_b)."""
    try:
        return runtime.set_caps_profile(device_id, body.get("profile", ""))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/sim/{device_id}/scene")
def sim_scene(device_id: str, body: dict = Body(default={})):
    try:
        return runtime.set_sim_scene(device_id, preset=body.get("preset", ""),
                                     targets=body.get("targets"),
                                     medium=body.get("medium"),
                                     noise_floor_dbfs=body.get("noise_floor_dbfs"),
                                     leakage_amplitude=body.get("leakage_amplitude"))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- live stream (FR-API-002: UI rate decoupled from acquisition) ------------
@app.websocket("/ws/live")
async def live(ws: WebSocket, device_id: str = Query("sim-pluto-0"),
               fps: float = Query(5.0)):
    await ws.accept()
    interval = 1.0 / max(0.5, min(fps, 15.0))
    loop = asyncio.get_event_loop()
    try:
        while True:
            dev = runtime.devices.get(device_id)
            if dev is None or not dev.connected:
                await ws.send_json({"error": "device not connected",
                                    "device_id": device_id})
                await asyncio.sleep(1.0)
                continue
            frame = await loop.run_in_executor(
                None, runtime.live_frame, device_id)
            frame["safety"] = runtime.safety.status()
            await ws.send_json(frame)
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a dying stream must not leave TX on
        for dev in runtime.devices.values():
            if dev.tx_enabled:
                dev.force_tx_off()
                runtime.safety.notify_tx_stopped(dev.device_id,
                                                 reason="live_stream_fault")


# -- static UI ---------------------------------------------------------------
class _NoCacheStatic(StaticFiles):
    """Serve the UI with revalidation forced.

    The UI is edited in place and reloaded constantly during bench work; a
    stale cached app.js against a fresh index.html silently produces a page
    whose controls do nothing, which is a miserable thing to debug.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return False

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


_UI_DIR = os.path.join(os.path.dirname(__file__), "..", "ui")
app.mount("/", _NoCacheStatic(directory=_UI_DIR, html=True), name="ui")
