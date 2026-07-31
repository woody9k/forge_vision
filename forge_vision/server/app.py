"""FastAPI application: HTTP + WebSocket routing over the Runtime.

Run with:  uvicorn forge_vision.server.app:app --port 8347
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from fastapi import (Body, FastAPI, HTTPException, Query, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..devices.base import ConfigurationError
from ..safety import SafetyViolation
from . import schemas as S
from .runtime import Runtime

log = logging.getLogger("forge_vision.server")

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
def rescan(body: S.RescanRequest = S.RescanRequest()):
    """Probe for hardware without restarting.

    Surveys every candidate transport, groups those that are the same physical
    radio, and registers the fastest — one entry per board. `prefer` overrides
    the choice; `uri` skips the survey and opens exactly that transport.
    """
    return runtime.rescan_hardware(body.uri, prefer=body.prefer,
                                   measure=body.measure)


@app.get("/api/devices/transports")
def device_transports(measure: bool = True):
    """Every way into every reachable radio, measured, registering nothing."""
    return runtime.survey_transports(measure=measure)


@app.get("/api/radios")
def radio_addresses():
    """Saved radio addresses, editable without touching a config file."""
    return runtime.list_radio_addresses()


@app.post("/api/radios")
def add_radio_address(body: S.RadioAddressRequest):
    try:
        return runtime.add_radio_address(body.address, label=body.label)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/radios/{radio_id}/update")
def update_radio_address(radio_id: str, body: S.RadioAddressUpdateRequest):
    try:
        return runtime.update_radio_address(radio_id, body.model_dump(exclude_none=True))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/radios/{radio_id}/delete")
def remove_radio_address(radio_id: str):
    try:
        return runtime.remove_radio_address(radio_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/devices/{device_id}/forget")
def forget_device(device_id: str):
    """Drop a radio from this session. The next scan will find it again."""
    try:
        return runtime.forget_device(device_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/devices/{device_id}/switch_transport")
def switch_transport(device_id: str, body: S.SwitchTransportRequest):
    """Reach the same radio over a different transport."""
    try:
        return runtime.switch_transport(device_id, body.uri)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


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
def configure(device_id: str, cfg: S.DeviceConfigRequest):
    try:
        return runtime.configure(device_id, cfg.set_fields())
    except (ConfigurationError, Exception) as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/devices/{device_id}/tx")
def set_tx(device_id: str, body: S.TxRequest):
    try:
        return runtime.set_tx(device_id, body.enable, body.waveform)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- safety ------------------------------------------------------------------
@app.post("/api/safety/arm")
def arm(body: S.ArmRequest):
    try:
        runtime.safety.arm(body.operator, body.acknowledgement)
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
def confirm_checklist(body: S.ChecklistRequest):
    try:
        if body.reset:
            return runtime.safety.reset_checklist()
        return runtime.safety.confirm_checklist_item(body.id, body.confirmed)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/survey")
def survey(body: S.SurveyRequest = S.SurveyRequest()):
    """Receive-only band occupancy survey. Transmits nothing."""
    try:
        return runtime.band_survey(
            device_id=body.device_id, start_hz=body.start_hz,
            stop_hz=body.stop_hz, step_hz=body.step_hz,
            sample_rate_hz=body.sample_rate_hz, rx_gain_db=body.rx_gain_db,
            samples=body.samples, name=body.name, operator=body.operator)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/safety/audit")
def audit(n: int = 200):
    return runtime.safety.audit_tail(n)


@app.post("/api/safety/profile")
def set_profile(body: S.ProfileRequest):
    name = body.profile
    if name not in runtime.safety.limits.frequency_profiles:
        raise HTTPException(404, f"unknown frequency profile: {name}")
    runtime.safety.limits.active_profile = name
    runtime.safety.audit("frequency_profile_changed", profile=name)
    return runtime.safety.status()


# -- jobs (FR-API-003) --------------------------------------------------------
@app.get("/api/jobs")
def jobs(kind: str = "", active_only: bool = False):
    return {"jobs": runtime.jobs.list(kind=kind, active_only=active_only),
            "summary": runtime.jobs.summary()["counts"]}


@app.post("/api/jobs")
def submit_job(body: S.JobRequest):
    try:
        return runtime.submit_job(body.kind, body.params)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, include_result: bool = False):
    try:
        return runtime.job_status(job_id, include_result=include_result)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    try:
        return runtime.jobs.cancel(job_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    try:
        return runtime.jobs.retry(job_id).to_dict()
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- RF chain (FR-RFC-006) ----------------------------------------------------
@app.get("/api/rf_chain")
def rf_chain():
    return {"declared": runtime.rf_chain, "resolved": runtime.current_chain()}


@app.post("/api/rf_chain")
def set_rf_chain(body: S.RfChainRequest):
    try:
        return runtime.set_rf_chain(
            tx_ids=body.tx_ids, rx_ids=body.rx_ids,
            antenna_tx=body.antenna_tx, antenna_rx=body.antenna_rx)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- saved chain configurations (FR-RFC-006) ----------------------------------
@app.get("/api/chains")
def chain_configs():
    """Saved antenna/cable configurations; one is active."""
    return runtime.list_chain_configs()


@app.post("/api/chains")
def save_chain_config(body: S.ChainConfigRequest):
    """Save the working chain under a name and make it active."""
    try:
        return runtime.save_chain_config(body.name, notes=body.notes)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/chains/detach")
def detach_chain_config():
    """Work from an unsaved chain instead of a named configuration."""
    return runtime.detach_chain_config()


@app.get("/api/chains/{config_id}")
def chain_config(config_id: str):
    """A configuration plus every measurement taken with it."""
    try:
        return runtime.chain_config_measurements(config_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/chains/{config_id}/activate")
def activate_chain_config(config_id: str):
    """Make this configuration the baseline for subsequent work."""
    try:
        return runtime.activate_chain_config(config_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/chains/{config_id}/delete")
def delete_chain_config(config_id: str):
    try:
        return runtime.delete_chain_config(config_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- receive-path protection (FR-SAF-005/006) ---------------------------------
@app.get("/api/safety/rx_protection")
def rx_protection(device_id: str = "sim-pluto-0"):
    try:
        cfg = runtime.device(device_id).config
        return runtime.safety.rx_protection(cfg.tx_gain_db, cfg.rx_gain_db)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/safety/path_attenuation")
def path_attenuation(body: S.PathAttenuationRequest):
    try:
        return runtime.safety.declare_path_attenuation(body.attenuation_db)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- position sources (FR-POS-001/002, UX-SCN-002) ----------------------------
@app.get("/api/position")
def position_status():
    return runtime.position_status()


@app.post("/api/position/source")
def set_position_source(body: S.PositionSourceRequest):
    try:
        return runtime.set_position_source(body.kind, **body.options())
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/position/ports")
def list_serial_ports():
    """Candidate serial ports for a position rig."""
    try:
        from serial.tools import list_ports
        return {"available": True, "ports": [
            {"device": p.device, "description": p.description,
             "hwid": p.hwid} for p in list_ports.comports()]}
    except ImportError:
        return {"available": False, "ports": [],
                "error": "pyserial is not installed; run `pip install pyserial`"}


# -- calibration -------------------------------------------------------------
@app.get("/api/calibration/{device_id}")
def calibration(device_id: str, waveform: str = ""):
    try:
        return runtime.calibration_status(device_id, waveform)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/calibration/{device_id}/cable_delay")
def cable_delay(device_id: str, body: S.CableDelayRequest):
    try:
        return runtime.set_cable_delay(device_id, body.delay_s)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/calibration/{device_id}/background")
def background(device_id: str, body: S.BackgroundRequest = S.BackgroundRequest()):
    try:
        return runtime.capture_background(
            device_id, body.waveform, body.chirps, body.operator)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- range lab ---------------------------------------------------------------
@app.post("/api/range/run")
def range_run(body: S.RangeRunRequest = S.RangeRunRequest()):
    try:
        return runtime.range_run(
            device_id=body.device_id, waveform_name=body.waveform,
            chirps=body.chirps, medium=body.medium,
            use_background=body.use_background, name=body.name,
            operator=body.operator, tags=body.tags,
            pipeline_overrides=body.pipeline_overrides,
            parent_id=body.parent_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/stepped/run")
def stepped_run(body: S.SteppedRunRequest = S.SteppedRunRequest()):
    """Stepped-frequency synthesis: sweep the LO, combine chunks coherently."""
    try:
        return runtime.stepped_run(
            device_id=body.device_id, start_hz=body.start_hz,
            stop_hz=body.stop_hz, waveform_name=body.waveform,
            overlap=body.overlap, chirps=body.chirps, medium=body.medium,
            correction=body.correction, max_range_m=body.max_range_m,
            name=body.name, operator=body.operator)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- scan studio -------------------------------------------------------------
@app.post("/api/scan/start")
def scan_start(body: S.ScanStartRequest):
    try:
        return runtime.scan_start(body.device_id, body.plan.model_dump(),
                                  body.operator)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/scan/{scan_id}/point")
def scan_point(scan_id: str, body: S.ScanPointRequest = S.ScanPointRequest()):
    try:
        return runtime.scan_point(scan_id, body.x_m, body.operator_override)
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
def capture(body: S.CaptureRequest = S.CaptureRequest()):
    try:
        return runtime.record_capture(
            device_id=body.device_id, num_samples=body.num_samples,
            segments=body.segments, name=body.name, operator=body.operator,
            waveform_name=body.waveform, tags=body.tags)
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
def annotate(exp_id: str, body: S.AnnotationRequest):
    try:
        return runtime.store.annotate(exp_id, body.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/experiments/{exp_id}/verify")
def verify(exp_id: str):
    try:
        return runtime.store.verify(exp_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/experiments/{exp_id}/replay")
def replay(exp_id: str, body: S.ReplayRequest = S.ReplayRequest()):
    try:
        return runtime.replay(exp_id, medium=body.medium,
                              pipeline_overrides=body.pipeline_overrides)
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


# -- sites / World View (release 0.4) -----------------------------------------
@app.get("/api/sites")
def sites():
    return runtime.sites.list()


@app.post("/api/sites")
def create_site(body: S.SiteRequest):
    try:
        return runtime.sites.create(
            name=body.name, coordinate_system=body.coordinate_system,
            notes=body.notes)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/sites/{site_id}")
def site(site_id: str):
    try:
        return runtime.sites.load(site_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/sites/{site_id}/register")
def register_scan(site_id: str, body: S.RegisterScanRequest):
    try:
        return runtime.sites.register_scan(
            site_id, body.experiment_id, origin_x_m=body.origin_x_m,
            origin_y_m=body.origin_y_m, heading_deg=body.heading_deg,
            label=body.label,
            position_uncertainty_m=body.position_uncertainty_m)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/sites/{site_id}/unregister")
def unregister_scan(site_id: str, body: S.UnregisterScanRequest):
    try:
        return runtime.sites.unregister_scan(site_id, body.experiment_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/sites/{site_id}/delete")
def delete_site(site_id: str):
    try:
        runtime.sites.delete(site_id)
        return {"deleted": site_id}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/sites/{site_id}/scene")
def site_scene(site_id: str, tolerance_m: float = 0.6,
               slice_depth_m: float | None = None):
    try:
        return runtime.site_scene(site_id, tolerance_m=tolerance_m,
                                  slice_depth_m=slice_depth_m)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/sites/{site_id}/report")
def site_report_endpoint(site_id: str, tolerance_m: float = 0.6):
    try:
        return runtime.site_report(site_id, tolerance_m=tolerance_m)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- SAGE assistant (release 0.5, §8) -----------------------------------------
@app.post("/api/sage/ask")
def sage_ask(body: S.SageAskRequest):
    try:
        return runtime.sage_ask(body.question, site_id=body.site_id,
                                experiment_id=body.experiment_id,
                                narrate=body.narrate)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/sage/narrate")
def sage_narrate(body: dict = Body(...)):
    """Second phase: narrate an answer the client already has. Kept separate
    so a slow local model never delays the instrument's own findings.

    Deliberately untyped: the body is an answer this API produced, echoed
    back verbatim, so constraining it here would mean maintaining a second
    copy of the fact structure.
    """
    try:
        return runtime.sage_narrate(body)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- optional LLM narration endpoints ------------------------------------
@app.get("/api/llm")
def llm_list():
    return runtime.llm_list()


@app.post("/api/llm")
def llm_put(body: S.LlmEndpointRequest):
    try:
        return runtime.llm_put(body.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/llm/{name}/delete")
def llm_remove(name: str):
    try:
        return runtime.llm_remove(name)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/llm/{name}/health")
def llm_health(name: str):
    try:
        return runtime.llm_health(name)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/sage/experiment/{exp_id}")
def sage_experiment(exp_id: str):
    try:
        return runtime.sage_experiment(exp_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/sage/site/{site_id}/finding/{index}")
def sage_explain(site_id: str, index: int):
    try:
        return runtime.sage_explain(site_id, index)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/sage/site/{site_id}/recommend")
def sage_recommend(site_id: str):
    try:
        return runtime.sage_recommend(site_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/sage/compare")
def sage_compare(a: str, b: str):
    try:
        return runtime.sage_compare(a, b)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- RF components / Antenna Lab (FR-RFC-*) -----------------------------------
@app.get("/api/components")
def components(kind: str = ""):
    return runtime.components.list(kind=kind)


@app.post("/api/components")
def create_component(body: S.ComponentRequest):
    try:
        return runtime.components.create(
            kind=body.kind, name=body.name, connector=body.connector,
            claimed_band=body.claimed_band, polarization=body.polarization,
            notes=body.notes, nominal_loss_db=body.nominal_loss_db,
            nominal_delay_ns=body.nominal_delay_ns)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.get("/api/components/{comp_id}")
def component(comp_id: str):
    try:
        return runtime.components.load(comp_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/components/{comp_id}/update")
def update_component(comp_id: str, body: S.ComponentUpdateRequest):
    try:
        return runtime.components.update(comp_id, body.set_fields())
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/components/{comp_id}/delete")
def delete_component(comp_id: str):
    try:
        runtime.components.delete(comp_id)
        return {"deleted": comp_id}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/components/{comp_id}/adopt_loss")
def adopt_measured_loss(comp_id: str, body: S.AdoptLossRequest = S.AdoptLossRequest()):
    """Set nominal loss from a two-port sweep already imported (FR-RFC-004)."""
    try:
        return runtime.components.adopt_measured_loss(comp_id, body.freq_hz)
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
def sim_caps(device_id: str, body: S.SimCapsRequest):
    """Emulate a specific hardware class (pluto_plus | pluto_rev_b)."""
    try:
        return runtime.set_caps_profile(device_id, body.profile)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


@app.post("/api/sim/{device_id}/scene")
def sim_scene(device_id: str, body: S.SimSceneRequest = S.SimSceneRequest()):
    try:
        return runtime.set_sim_scene(
            device_id, preset=body.preset, targets=body.targets,
            medium=body.medium, noise_floor_dbfs=body.noise_floor_dbfs,
            leakage_amplitude=body.leakage_amplitude)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc)


# -- live stream (FR-API-002: UI rate decoupled from acquisition) ------------
@app.websocket("/ws/live")
async def live(ws: WebSocket, device_id: str = Query("sim-pluto-0"),
               fps: float = Query(5.0)):
    await ws.accept()
    interval = 1.0 / max(0.5, min(fps, 15.0))
    loop = asyncio.get_event_loop()

    # A client that goes away — tab closed, page navigated, laptop asleep — is
    # otherwise only noticed when a send finally fails, which can take a long
    # time or never. Until then the handler keeps asking the radio for frames,
    # and because only one caller at a time may hold the device, stale streams
    # pile up and starve the live one. Watch for the disconnect directly.
    async def watch_for_disconnect():
        try:
            while True:
                await ws.receive()
        except Exception:  # noqa: BLE001 - any failure here means "gone"
            return

    watcher = asyncio.create_task(watch_for_disconnect())
    try:
        while not watcher.done():
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
    except Exception as exc:  # noqa: BLE001 - a dying stream must not leave TX on
        # Never silently. A stream that stops without saying why leaves the
        # operator staring at a frozen waterfall with nothing to act on.
        log.exception("live stream for %s failed", device_id)
        runtime.safety.audit("live_stream_fault", device=device_id,
                             error=f"{type(exc).__name__}: {exc}")
        try:
            await ws.send_json({"error": f"{type(exc).__name__}: {exc}",
                                "device_id": device_id, "fatal": True})
        except Exception:  # noqa: BLE001 - the client may already be gone
            pass
        for dev in runtime.devices.values():
            if dev.tx_enabled:
                dev.force_tx_off()
                runtime.safety.notify_tx_stopped(dev.device_id,
                                                 reason="live_stream_fault")
    finally:
        watcher.cancel()


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
