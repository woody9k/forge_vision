"""Forge Vision runtime: orchestrates devices, safety, DSP, and storage.

This layer is UI-agnostic (§4.1: hardware access isolated from the UI) and is
exercised directly by the test suite. The FastAPI app in `app.py` is a thin
routing shell over this class.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from ..config import DEFAULT_DATA_DIR, MEDIA_PRESETS, Medium, SafetyLimits
from ..devices.base import DeviceConfig
from ..devices.replay import ReplayDevice
from ..devices.simulated import (SceneTarget, SimScene, SimulatedPluto,
                                 default_bench_scene, default_scan_scene)
from ..dsp import stages as _stages  # noqa: F401  (registers stages)
from ..dsp.pipeline import Pipeline, PipelineContext
from ..dsp.stepped import (stepped_range_profile, stitch_subbands,
                           subband_response)
from ..experiments.store import ExperimentStore
from ..jobs import JobManager
from ..positioning import (ManualSource, PositionSample, ReplaySource,
                           SerialSource, pose_from_sample)
from ..imaging.bscan import BScanBuilder
from ..imaging.migration import focused_targets, migrate_bscan
from ..reports import site_report
from ..devices.book import RadioBook
from ..rfcomponents.chains import ChainStore
from ..rfcomponents.store import ComponentStore
from ..safety import SafetyController
from ..sage.narrate import EndpointStore
from ..sites import SiteStore, depth_slice, fuse_targets, scan_path
from ..waveforms import CATALOG


def _survey_point(freq_hz: float, seg) -> dict:
    """Occupancy statistics for one tuning step of a band survey."""
    iq = seg.iq
    n = 4096
    usable = (len(iq) // n) * n
    if usable == 0:
        return {"center_hz": freq_hz, "noise_floor_dbfs": None,
                "peak_dbfs": None, "occupancy": None, "clipped": seg.clipped}
    segs = iq[:usable].reshape(-1, n) * np.hanning(n)
    psd = 10 * np.log10(
        np.mean(np.abs(np.fft.fft(segs, axis=1)) ** 2, axis=0) / n ** 2 + 1e-20)
    floor = float(np.median(psd))
    peak = float(np.max(psd))
    # fraction of bins more than 10 dB above the floor = how busy the channel is
    occupancy = float(np.mean(psd > floor + 10.0))
    return {
        "center_hz": freq_hz,
        "noise_floor_dbfs": round(floor, 1),
        "peak_dbfs": round(peak, 1),
        "peak_above_floor_db": round(peak - floor, 1),
        "occupancy": round(occupancy, 4),
        "clipped": bool(seg.clipped),
        "loss_events": len(seg.loss_events),
    }


RANGE_PIPELINE = [
    ("dc_remove", {}),
    ("range_profile_fmcw", {"zero_pad_factor": 8, "max_range_m": 40.0}),
    ("background_subtract", {}),
    ("detect_peaks", {"threshold_db": 10.0}),
    ("quality_metrics", {}),
]

LIVE_PIPELINE = [
    ("dc_remove", {}),
    ("spectrum", {"fft_size": 1024}),
    ("quality_metrics", {}),
]


class Runtime:
    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        os.makedirs(self.data_dir, exist_ok=True)
        self.store = ExperimentStore(os.path.join(self.data_dir, "experiments"))
        self.components = ComponentStore(os.path.join(self.data_dir, "components"))
        self.chains = ChainStore(os.path.join(self.data_dir, "chains"))
        self.radios = RadioBook(os.path.join(self.data_dir, "radios.json"))
        self.sites = SiteStore(os.path.join(self.data_dir, "sites"))
        self.llm = EndpointStore(os.path.join(self.data_dir, "llm_endpoints.json"))
        self.llm.load()
        self.safety = SafetyController(
            SafetyLimits(), os.path.join(self.data_dir, "logs", "safety_audit.jsonl"))
        self.devices: dict[str, object] = {}
        self.device_locks: dict[str, threading.Lock] = {}
        self.calibration: dict[str, dict] = {}       # device_id -> assets
        self.scans: dict[str, dict] = {}             # scan experiment_id -> session
        self.jobs = JobManager()                     # FR-API-003
        self._tx_waveforms: dict[str, object] = {}   # device -> live waveform
        # set by emergency stop and shutdown; long loops check it
        self.stop_acquisition = threading.Event()
        self.position_source = ManualSource()        # FR-POS-001 default
        self._register(SimulatedPluto("sim-pluto-0"))
        self._discover_hardware()

    # -- devices -----------------------------------------------------------
    def _register(self, dev) -> None:
        self.devices[dev.device_id] = dev
        self.device_locks[dev.device_id] = threading.Lock()
        self.calibration.setdefault(dev.device_id, {
            "cable_delay_s": 0.0,
            "background": None,
            "leakage_baseline": None,
        })

    def _discover_hardware(self) -> None:
        try:
            from ..devices.pluto import PlutoDevice
            for dev in PlutoDevice.discover(book=tuple(self.radios.uris())):
                self._register(dev)
        except Exception:  # noqa: BLE001 - hardware discovery is best-effort
            pass

    def rescan_hardware(self, uri: str = "", prefer: str = "auto",
                        measure: bool = True) -> dict:
        """Probe for radios without restarting.

        With no explicit URI this surveys every candidate transport, groups the
        ones that are the same physical board, and registers the fastest way in
        — one entry per radio. `prefer` overrides the choice ("usb",
        "network", or an exact URI) for when the fastest link is not the one
        you want, such as debugging the network the radio is on.
        """
        from ..devices.pluto import PlutoDevice, driver_status
        status = driver_status()
        result = {"driver": status, "added": [], "already_present": [],
                  "errors": [], "survey": []}
        if not status["available"]:
            return result

        if uri:
            # An explicit URI is an instruction, not a suggestion: open exactly
            # that transport without surveying or second-guessing it.
            device_id = f"pluto-{uri}"
            if device_id in self.devices:
                result["already_present"].append(device_id)
                return result
            try:
                dev = PlutoDevice(uri)
                dev.connect()
                self._register(dev)
                self.safety.audit("device_discovered", device=device_id, uri=uri)
                result["added"].append(self._describe_device(dev))
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                result["errors"].append({"uri": uri, "error": str(exc)})
            return result

        try:
            found = PlutoDevice.discover(prefer=prefer, measure=measure,
                                         book=tuple(self.radios.uris()))
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"uri": "(survey)", "error": str(exc)})
            return result
        for dev in found:
            info = getattr(dev, "discovery", {}) or {}
            result["survey"].append(info)
            device_id = dev.device_id
            # Already registered under *any* transport of the same board: the
            # whole point of the survey is one entry per radio.
            known = {d.uri for d in self.devices.values() if hasattr(d, "uri")}
            alt_uris = {t.get("uri") for t in info.get("alternatives", [])}
            if device_id in self.devices or (known & alt_uris):
                result["already_present"].append(device_id)
                try:
                    dev.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                continue
            self._register(dev)
            self.safety.audit("device_discovered", device=device_id,
                              uri=dev.uri, reason=info.get("reason", ""))
            result["added"].append(self._describe_device(dev))
        return result

    def survey_transports(self, measure: bool = True) -> dict:
        """Report every way into every radio, without registering anything.

        A transport this deployment already holds is reported as in use rather
        than as unreachable. The USB backend is exclusive, so probing it while
        we own it fails with "Device or resource busy" — calling that
        "unreachable" would blame the hardware for our own handle.
        """
        from ..devices import discovery
        from ..devices.pluto import driver_status
        status = driver_status()
        if not status["available"]:
            return {"driver": status, "boards": [], "probes": []}

        mine = {getattr(d, "uri", None): did
                for did, d in self.devices.items() if getattr(d, "uri", None)}
        probes = []
        for uri in discovery.candidate_uris(book=tuple(self.radios.uris())):
            if uri in mine:
                held = discovery.TransportProbe(
                    uri=uri, reachable=True,
                    error="", identity={})
                held.in_use_by = mine[uri]
                probes.append(held)
                continue
            probes.append(discovery.probe(uri, measure=measure))
        out = {"driver": status,
               "boards": discovery.group_boards(probes),
               "probes": []}
        for p in probes:
            d = p.to_dict()
            if getattr(p, "in_use_by", ""):
                d["in_use_by"] = p.in_use_by
                d["note"] = ("already open in this deployment; not re-probed "
                             "because the USB backend is exclusive")
            out["probes"].append(d)
        return out

    def forget_device(self, device_id: str) -> dict:
        """Drop a radio from this session.

        Discovery will find it again on the next scan — this removes the entry,
        not the hardware. Saved addresses are managed separately so that
        forgetting a device does not quietly unlearn where it lives.
        """
        dev = self.device(device_id)
        if getattr(dev, "kind", "") == "simulated_pluto_plus":
            raise ValueError("the simulated radio is always available and "
                             "cannot be removed")
        try:
            dev.disconnect()
        except Exception:  # noqa: BLE001 - forgetting must not be blockable
            pass
        self.devices.pop(device_id, None)
        self.device_locks.pop(device_id, None)
        self.calibration.pop(device_id, None)
        self.safety.audit("device_forgotten", device=device_id)
        return {"forgotten": device_id}

    def switch_transport(self, device_id: str, uri: str) -> dict:
        """Reach the same radio a different way.

        Calibration and any in-flight scan belong to the device entry, so this
        replaces the entry rather than mutating it, and refuses while a scan is
        running rather than silently reattaching underneath one.
        """
        from ..devices.pluto import PlutoDevice
        dev = self.device(device_id)
        if getattr(dev, "kind", "") == "simulated_pluto_plus":
            raise ValueError("the simulated radio has no transports")
        live = [sid for sid, sc in self.scans.items()
                if sc.get("device_id") == device_id and not sc.get("finalized")]
        if live:
            raise ValueError(f"{device_id} has an unfinished scan ({live[0]}); "
                             "finalize or abandon it before switching transport")
        target = PlutoDevice(uri)
        target.connect()                    # fail before tearing anything down
        old_discovery = getattr(dev, "discovery", {}) or {}
        self.forget_device(device_id)
        target.discovery = {**old_discovery, "uri": uri,
                            "reason": f"switched by operator to {uri}"}
        self._register(target)
        self.safety.audit("device_transport_switched",
                          device=target.device_id, uri=uri, was=device_id)
        return self._describe_device(target)

    # -- saved radio addresses (FR-DEV-002) ---------------------------------
    def list_radio_addresses(self) -> list[dict]:
        import os as _os
        pinned = _os.environ.get("FORGE_VISION_PLUTO_URIS", "").strip()
        out = self.radios.list()
        for e in out:
            e["in_use"] = any(getattr(d, "uri", "") == e["uri"]
                              for d in self.devices.values())
        if pinned:
            for e in out:
                e["overridden_by_env"] = True
        return out

    def add_radio_address(self, address: str, label: str = "") -> dict:
        entry = self.radios.add(address, label=label)
        self.safety.audit("radio_address_added", uri=entry["uri"],
                          label=entry["label"])
        return entry

    def update_radio_address(self, radio_id: str, fields: dict) -> dict:
        return self.radios.update(radio_id, fields)

    def remove_radio_address(self, radio_id: str) -> dict:
        return self.radios.remove(radio_id)

    def device(self, device_id: str):
        if device_id not in self.devices:
            raise KeyError(f"unknown device: {device_id}")
        return self.devices[device_id]

    def connect(self, device_id: str) -> dict:
        dev = self.device(device_id)
        dev.connect()
        self.safety.audit("device_connected", device=device_id)
        return self._describe_device(dev)

    def disconnect(self, device_id: str) -> dict:
        dev = self.device(device_id)
        dev.disconnect()   # fault-safe TX off inside adapter
        self.safety.notify_tx_stopped(device_id, reason="disconnect")
        self.safety.audit("device_disconnected", device=device_id)
        return self._describe_device(dev)

    def configure(self, device_id: str, cfg: dict) -> dict:
        dev = self.device(device_id)
        merged = {**dev.config.to_dict(), **cfg}
        dev.configure(DeviceConfig(**merged))
        # A live transmitter must not inherit permission granted for a
        # different configuration. Previously this path bypassed the interlock
        # entirely: tx gain could go from -30 dB to 0 dB, or the radio could be
        # walked outside the active frequency profile, without revalidation.
        self.enforce_tx_authorization(f"device {device_id} reconfigured")
        return self._describe_device(dev)

    # -- safety ------------------------------------------------------------
    def emergency_stop(self) -> dict:
        """Stop transmitting and stop acquiring (FR-SAF-003).

        Disabling TX is the urgent part, so it happens first and unconditionally.
        But a stop that leaves survey jobs sweeping and live streams pulling
        buffers has not stopped the instrument — it has only made it quieter,
        and the next thing an operator does may re-key a radio that is still
        mid-acquisition.
        """
        results = self.safety.emergency_stop(self.devices.values())
        self._tx_waveforms.clear()
        for device_id in list(self.devices):
            self.safety.revoke_authorization(device_id)
        self.stop_acquisition.set()
        cancelled = self._cancel_active_jobs("emergency stop")
        interrupted = self._mark_scans_interrupted("emergency stop")
        self.safety.audit("emergency_stop", jobs_cancelled=cancelled,
                          scans_interrupted=interrupted)
        return {"stopped": True, "results": results,
                "jobs_cancelled": cancelled, "scans_interrupted": interrupted}

    def resume_acquisition(self) -> dict:
        """Clear the emergency-stop latch.

        An emergency stop latches: acquisition stays refused until an operator
        deliberately says the instrument is fit to use again. Auto-clearing on
        the next request would make the stop a momentary interruption rather
        than a state someone has to look at and dismiss.
        """
        was_set = self.stop_acquisition.is_set()
        self.stop_acquisition.clear()
        if was_set:
            self.safety.audit("acquisition_resumed")
        return {"acquisition_stopped": False, "was_stopped": was_set}

    def _cancel_active_jobs(self, reason: str) -> list[str]:
        cancelled = []
        for j in self.jobs.list(active_only=True):
            try:
                self.jobs.cancel(j["job_id"])
                cancelled.append(j["job_id"])
            except Exception:  # noqa: BLE001 - stopping must not be blockable
                pass
        return cancelled

    def _mark_scans_interrupted(self, reason: str) -> list[str]:
        """Record unfinished scans as interrupted rather than leaving them open.

        A scan abandoned mid-line is partial data, not absent data. Saying so
        keeps it distinguishable from a scan that simply has not started
        (FR-ACQ-003).
        """
        marked = []
        for exp_id, session in list(self.scans.items()):
            if session.get("finalized"):
                continue
            try:
                self.store.annotate(exp_id, {
                    "type": "interrupted", "reason": reason,
                    "at": time.time(),
                    "text": f"acquisition interrupted: {reason}"})
                session["interrupted"] = reason
                marked.append(exp_id)
            except Exception:  # noqa: BLE001
                pass
        return marked

    def shutdown(self, reason: str = "application shutdown") -> dict:
        """Bring the instrument to a safe, recorded stop.

        Called from the application lifespan handler so that stopping the
        service is not merely the process disappearing while a radio is keyed.
        Software cannot guarantee this after a host failure — a hardware TX
        watchdog is the real answer for field work — but it covers the ordinary
        case of the service being stopped or restarted.
        """
        out = self.emergency_stop()
        self.safety.disarm()
        for device_id, dev in list(self.devices.items()):
            try:
                dev.disconnect()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        self.safety.audit("runtime_shutdown", reason=reason)
        out["shutdown"] = reason
        return out

    def arm(self, operator: str, acknowledgement: str) -> dict:
        """Arm the interlock. Arming is a deliberate act, so it also lifts an
        emergency-stop latch rather than leaving the operator to find it."""
        self.safety.arm(operator, acknowledgement)
        self.stop_acquisition.clear()
        return self.safety.status()

    def _enable_tx(self, dev, waveform, tx_gain_db: float) -> None:
        # only a physical receiver can be damaged, so the RX-protection
        # interlock is enforced for real radios and recorded for simulated ones
        self.safety.validate_tx(
            dev.config.center_frequency_hz, waveform, tx_gain_db,
            rx_gain_db=dev.config.rx_gain_db,
            enforce_rx_protection=not dev.kind.startswith("simulated"))
        dev.load_waveform(waveform)
        dev.enable_tx()
        # Remember the exact configuration this permission was granted against,
        # and which waveform is loaded, so a later change can be detected.
        self._tx_waveforms[dev.device_id] = waveform
        self.safety.authorize_tx(dev.device_id, self._tx_fingerprint(dev, waveform,
                                                                    tx_gain_db))
        self.safety.notify_tx_started(
            dev.device_id, waveform=waveform.name,
            frequency_hz=dev.config.center_frequency_hz, tx_gain_db=tx_gain_db)

    def _disable_tx(self, dev, reason: str = "normal") -> None:
        dev.force_tx_off()
        self._tx_waveforms.pop(dev.device_id, None)
        self.safety.notify_tx_stopped(dev.device_id, reason=reason)

    def _tx_fingerprint(self, dev, waveform, tx_gain_db: float) -> str:
        return self.safety.tx_fingerprint(
            dev.config.center_frequency_hz, waveform, tx_gain_db,
            rx_gain_db=dev.config.rx_gain_db,
            rf_bandwidth_hz=float(dev.config.rx_bandwidth_hz),
            device_sample_rate_hz=float(dev.config.sample_rate_hz))

    def set_frequency_profile(self, name: str) -> dict:
        """Change the active profile, withdrawing any TX it no longer covers."""
        if name not in self.safety.limits.frequency_profiles:
            raise KeyError(f"unknown frequency profile: {name}")
        self.safety.limits.active_profile = name
        self.safety.audit("frequency_profile_changed", profile=name)
        revoked = self.enforce_tx_authorization(f"frequency profile -> {name}")
        status = self.safety.status()
        if revoked:
            status["tx_revoked"] = revoked
        return status

    def declare_path_attenuation(self, db: float) -> dict:
        """Record TX->RX isolation, withdrawing TX approved under the old value."""
        out = self.safety.declare_path_attenuation(db)
        revoked = self.enforce_tx_authorization(f"path attenuation -> {db} dB")
        if revoked:
            out["tx_revoked"] = revoked
        return out

    def enforce_tx_authorization(self, reason: str) -> list[str]:
        """Force TX off wherever the approved configuration no longer holds.

        Transmit is authorized against a configuration, not a device. Changing
        the centre frequency, gain, bandwidth, sample rate, safety profile or
        declared path after TX is live means the approval no longer describes
        what the radio is doing — so the approval is withdrawn rather than
        quietly inherited (FR-SAF-004).
        """
        revoked = []
        for device_id, dev in list(self.devices.items()):
            if not getattr(dev, "tx_enabled", False):
                continue
            waveform = self._tx_waveforms.get(device_id)
            approved = self.safety.authorization_for(device_id)
            current = (self._tx_fingerprint(dev, waveform, dev.config.tx_gain_db)
                       if waveform is not None else "")
            if approved and current == approved:
                continue
            self.safety.audit("tx_authorization_revoked", device=device_id,
                              reason=reason, approved=approved, now=current)
            self._disable_tx(dev, reason=f"authorization revoked: {reason}")
            self.safety.revoke_authorization(device_id)
            revoked.append(device_id)
        return revoked

    def set_tx(self, device_id: str, enable: bool, waveform_name: str = "") -> dict:
        dev = self.device(device_id)
        if enable:
            self._check_waveform(dev, waveform_name)
            self._enable_tx(dev, CATALOG[waveform_name], dev.config.tx_gain_db)
        else:
            self._disable_tx(dev)
        return self._describe_device(dev)

    def _check_waveform(self, dev, waveform_name: str):
        """Reject an incompatible waveform with an actionable message."""
        if waveform_name not in CATALOG:
            raise KeyError(f"unknown waveform: {waveform_name}")
        wf = CATALOG[waveform_name]
        problems = wf.validate(dev.capabilities)
        if problems:
            usable = dev.compatible_waveforms(CATALOG)
            raise ValueError(
                f"waveform '{waveform_name}' is not supported by "
                f"{dev.device_id} ({'; '.join(problems)}). "
                f"Compatible waveforms: {', '.join(usable) or 'none'}")
        return wf

    # -- calibration -------------------------------------------------------
    def set_cable_delay(self, device_id: str, delay_s: float) -> dict:
        self.calibration[device_id]["cable_delay_s"] = float(delay_s)
        self.safety.audit("calibration_updated", device=device_id,
                          cable_delay_s=delay_s)
        return self.calibration_status(device_id)

    def calibration_status(self, device_id: str, waveform_name: str = "",
                           config: dict | None = None) -> dict:
        """Validity checks for the current run (FR-CAL-007, UX-RNG-002)."""
        cal = self.calibration[device_id]
        bg = cal.get("background")
        warnings = []
        if cal["cable_delay_s"] == 0.0:
            warnings.append("cable delay not calibrated; ranges include cable path")
        if bg is None:
            warnings.append("no background captured; clutter is not subtracted")
        else:
            if waveform_name and bg["waveform"] != waveform_name:
                warnings.append(
                    f"background was captured with waveform '{bg['waveform']}', "
                    f"not '{waveform_name}'")
            if config and bg["config"] != config:
                warnings.append("background captured under a different device config")
            if time.time() - bg["captured_at"] > 3600:
                warnings.append("background is older than one hour; consider recapture")
        return {
            "cable_delay_s": cal["cable_delay_s"],
            "background": None if bg is None else {
                "captured_at": bg["captured_at"], "waveform": bg["waveform"],
                "experiment_id": bg["experiment_id"]},
            "warnings": warnings,
            "valid": not warnings,
        }

    # -- shared capture/processing helpers ----------------------------------
    def _medium_from(self, medium: str | dict | None) -> Medium:
        if medium is None:
            return Medium()
        if isinstance(medium, str):
            return MEDIA_PRESETS.get(medium, Medium())
        return Medium(
            name=medium.get("name", "custom"),
            epsilon_r=float(medium.get("epsilon_r", 1.0)),
            epsilon_r_uncertainty=float(medium.get("epsilon_r_uncertainty", 0.0)),
            attenuation_db_per_m=float(medium.get("attenuation_db_per_m", 0.0)))

    def _ranging_capture(self, dev, waveform, chirps: int,
                         position: dict | None = None):
        """TX-on capture bracketed so TX can never stay on after a failure."""
        num_samples = waveform.num_samples * chirps
        with self.device_locks[dev.device_id]:
            self._enable_tx(dev, waveform, dev.config.tx_gain_db)
            try:
                seg = dev.receive(num_samples, position=position)
            finally:
                self._disable_tx(dev)
        return seg

    def _process_range(self, seg, medium: Medium, cable_delay_s: float,
                       background, pipeline_overrides: dict | None = None):
        stages_list = []
        overrides = pipeline_overrides or {}
        for name, params in RANGE_PIPELINE:
            stages_list.append((name, {**params, **overrides.get(name, {})}))
        pipe = Pipeline(stages_list)
        ctx = PipelineContext(
            sample_rate_hz=seg.sample_rate_hz,
            center_frequency_hz=seg.center_frequency_hz,
            waveform=seg.waveform,
            medium=medium.to_dict(),
            cable_delay_s=cable_delay_s,
            background=background)
        return pipe.run(seg.iq, ctx)

    # -- Range Lab (release 0.2) -------------------------------------------
    def capture_background(self, device_id: str, waveform_name: str = "fmcw_bench_56M",
                           chirps: int = 8, operator: str = "") -> dict:
        """Capture the static-scene baseline (FR-CAL-004)."""
        dev = self.device(device_id)
        wf = self._check_waveform(dev, waveform_name)
        seg = self._ranging_capture(dev, wf, chirps)
        result = self._process_range(seg, Medium(),
                                     self.calibration[device_id]["cable_delay_s"], None)
        manifest = self.store.create(
            name="background capture", kind="calibration", operator=operator,
            objective="static scene baseline for background subtraction",
            hardware={"device_id": device_id, "kind": dev.kind,
                      "rf_chain": self.current_chain()},
            rf_config=dev.config.to_dict(),
            calibration={"cable_delay_s": self.calibration[device_id]["cable_delay_s"]})
        exp_id = manifest["identity"]["experiment_id"]
        self.store.add_segment(exp_id, seg)
        self.store.add_derived(exp_id, "range_profile",
                               {k: v for k, v in result.products.items()
                                if not k.startswith("_")},
                               result.record, ["segment_0000"])
        self.store.finalize(exp_id)
        self.calibration[device_id]["background"] = {
            "spectrum": result.products["_range_complex"],
            "captured_at": time.time(),
            "waveform": waveform_name,
            "config": dev.config.to_dict(),
            "experiment_id": exp_id,
        }
        return {"experiment_id": exp_id,
                "calibration": self.calibration_status(device_id, waveform_name)}

    def range_run(self, device_id: str, waveform_name: str = "fmcw_bench_56M",
                  chirps: int = 8, medium: str | dict | None = None,
                  use_background: bool = True, name: str = "range run",
                  operator: str = "", tags: list[str] | None = None,
                  pipeline_overrides: dict | None = None,
                  parent_id: str | None = None) -> dict:
        """One complete Range Lab run: capture -> package -> process -> report."""
        dev = self.device(device_id)
        wf = self._check_waveform(dev, waveform_name)
        med = self._medium_from(medium)
        cal = self.calibration[device_id]
        background = None
        if use_background and cal.get("background") is not None:
            background = cal["background"]["spectrum"]

        seg = self._ranging_capture(dev, wf, chirps)
        cal_status = self.calibration_status(device_id, waveform_name,
                                             dev.config.to_dict())
        result = self._process_range(seg, med, cal["cable_delay_s"], background,
                                     pipeline_overrides)

        manifest = self.store.create(
            name=name, kind="range", operator=operator, tags=tags or [],
            objective=f"range profile via {waveform_name}",
            hardware={"device_id": device_id, "kind": dev.kind,
                      "rf_chain": self.current_chain()},
            rf_config=dev.config.to_dict(),
            calibration={"cable_delay_s": cal["cable_delay_s"],
                         "background_experiment": (cal.get("background") or {}).get(
                             "experiment_id"),
                         "propagation_model": med.to_dict(),
                         "status_at_run": cal_status},
            parent_id=parent_id)
        exp_id = manifest["identity"]["experiment_id"]
        self.store.add_segment(exp_id, seg)
        products = {k: v for k, v in result.products.items() if not k.startswith("_")}
        self.store.add_derived(exp_id, "range_profile", products, result.record,
                               ["segment_0000"])
        self.store.finalize(exp_id)

        return {
            "experiment_id": exp_id,
            "range_profile": products.get("range_profile"),
            "peaks": products.get("peaks", []),
            "quality": products.get("quality", {}),
            "calibration": cal_status,
            "warnings": result.warnings,
            "segment": {"clipped": seg.clipped, "loss_events": seg.loss_events},
            "processing": result.record,
        }

    def replay(self, exp_id: str, medium: str | dict | None = None,
               use_background: bool = False,
               pipeline_overrides: dict | None = None) -> dict:
        """Reprocess recorded raw data without hardware (AC-006, FR-ACQ-008)."""
        manifest = self.store.load(exp_id)
        replay_dev = ReplayDevice(manifest, self.store)
        med = self._medium_from(
            medium or manifest.get("calibration", {}).get("propagation_model"))
        cable = manifest.get("calibration", {}).get("cable_delay_s", 0.0)
        outputs = []
        while replay_dev.remaining:
            seg = replay_dev.receive()
            result = self._process_range(seg, med, cable, None, pipeline_overrides)
            outputs.append(result)
        if not outputs:
            raise ValueError("experiment has no recorded segments")
        result = outputs[-1]
        products = {k: v for k, v in result.products.items() if not k.startswith("_")}
        fingerprint = result.record["fingerprint"]
        derived_name = f"replay_{fingerprint}"
        self.store.add_derived(exp_id, derived_name, products, result.record,
                               [s["segment_id"] for s in manifest["segments"]])
        return {
            "experiment_id": exp_id,
            "derived_name": derived_name,
            "range_profile": products.get("range_profile"),
            "peaks": products.get("peaks", []),
            "quality": products.get("quality", {}),
            "warnings": result.warnings,
            "processing": result.record,
            "segments_processed": len(outputs),
        }

    # -- Scan Studio (release 0.3) -----------------------------------------
    def scan_start(self, device_id: str, plan: dict, operator: str = "") -> dict:
        """Create a scan experiment from a plan (UX-SCN-001)."""
        dev = self.device(device_id)
        plan = {
            "start_m": float(plan.get("start_m", 0.0)),
            "end_m": float(plan.get("end_m", 3.0)),
            "step_m": float(plan.get("step_m", 0.1)),
            "waveform": plan.get("waveform", "fmcw_bench_56M"),
            "chirps": int(plan.get("chirps", 4)),
            "medium": plan.get("medium", "soil_dry"),
            "antenna_height_m": float(plan.get("antenna_height_m", 0.0)),
            "orientation": plan.get("orientation", "broadside"),
            "position_uncertainty_m": float(plan.get("position_uncertainty_m", 0.01)),
            # free-space-equivalent range window for the scan display
            "max_range_m": float(plan.get("max_range_m", 16.0)),
            "notes": plan.get("notes", ""),
        }
        med = self._medium_from(plan["medium"])
        manifest = self.store.create(
            name=plan.get("notes") or "linear scan", kind="scan", operator=operator,
            objective=f"B-scan {plan['start_m']}–{plan['end_m']} m "
                      f"step {plan['step_m']} m",
            hardware={"device_id": device_id, "kind": dev.kind,
                      "rf_chain": self.current_chain()},
            geometry={"scan_plan": plan, "coordinate_system":
                      "local scan axis, meters from start position"},
            rf_config=dev.config.to_dict(),
            calibration={"cable_delay_s": self.calibration[device_id]["cable_delay_s"],
                         "propagation_model": med.to_dict()})
        exp_id = manifest["identity"]["experiment_id"]
        builder = BScanBuilder(plan)
        self.scans[exp_id] = {"builder": builder, "device_id": device_id,
                              "plan": plan}
        self._persist_scan(exp_id)
        return {"scan_id": exp_id, "plan": plan,
                "positions_m": [float(p) for p in builder.positions]}

    def _persist_scan(self, exp_id: str) -> None:
        session = self.scans[exp_id]
        self.store.add_derived(exp_id, "scan_state",
                               session["builder"].to_dict(),
                               {"stages": [], "fingerprint": "scan_state"}, [])

    def scan_resume(self, exp_id: str) -> dict:
        """Rebuild an interrupted scan from its package (UX-SCN-008)."""
        if exp_id not in self.scans:
            manifest = self.store.load(exp_id)
            if manifest["identity"]["kind"] != "scan":
                raise ValueError("not a scan experiment")
            state = self.store.load_derived(exp_id, "scan_state")["product"]
            builder = BScanBuilder.from_dict(state)
            self.scans[exp_id] = {
                "builder": builder,
                "device_id": manifest["hardware"]["device_id"],
                "plan": builder.plan,
            }
        builder = self.scans[exp_id]["builder"]
        return {"scan_id": exp_id, "status": builder.status()}

    def scan_point(self, exp_id: str, x_m: float | None = None,
                   operator_override: bool = False) -> dict:
        """Capture one scan point with quality gating (UX-SCN-003).

        With no explicit position the active source is asked where the
        antenna is — that is the survey-wheel path. The reading is snapped to
        the nearest planned position, and how far it had to move to get there
        is recorded rather than hidden.
        """
        if exp_id not in self.scans:
            self.scan_resume(exp_id)
        session = self.scans[exp_id]
        builder: BScanBuilder = session["builder"]
        plan = session["plan"]
        dev = self.device(session["device_id"])

        sample = None
        snap_error_m = 0.0
        if x_m is None:
            sample = self.position_source.read()
            if sample is None:
                raise ValueError(
                    f"the {self.position_source.name} position source has no "
                    "reading; enter a position explicitly or check the link")
            nearest = min(builder.positions,
                          key=lambda p: abs(p - sample.x_m))
            snap_error_m = abs(float(nearest) - sample.x_m)
            x_m = float(nearest)
        if builder.index_of(x_m) is None:
            raise ValueError(f"position {x_m} m is outside the scan plan")

        wf = self._check_waveform(dev, plan["waveform"])
        med = self._medium_from(plan["medium"])
        cal = self.calibration[dev.device_id]
        if sample is not None:
            position = pose_from_sample(sample, plan)
            position["x_m"] = x_m               # the planned grid position
            position["reported_x_m"] = sample.x_m
            position["snap_error_m"] = round(snap_error_m, 4)
            # snapping further than half a step means the rig is not where the
            # plan thinks it is; that is a measurement problem, not a rounding
            # detail, so it becomes a gate failure below
            position["uncertainty_m"] = max(sample.uncertainty_m, snap_error_m)
        else:
            position = {"x_m": x_m,
                        "uncertainty_m": plan["position_uncertainty_m"],
                        "height_m": plan["antenna_height_m"],
                        "source": "operator"}
        seg = self._ranging_capture(dev, wf, plan["chirps"], position=position)
        result = self._process_range(
            seg, med, cal["cable_delay_s"], None,
            pipeline_overrides={"range_profile_fmcw":
                                {"max_range_m": plan["max_range_m"]}})
        quality = result.products.get("quality", {})

        gate_failures = []
        if seg.clipped:
            gate_failures.append("receiver clipping")
        if seg.loss_events:
            gate_failures.append(f"{len(seg.loss_events)} sample-loss event(s)")
        if quality.get("profile_peak_snr_db", 0) < 6:
            gate_failures.append(
                f"peak SNR {quality.get('profile_peak_snr_db')} dB below 6 dB gate")
        if snap_error_m > plan["step_m"] / 2:
            gate_failures.append(
                f"reported position {position.get('reported_x_m'):.3f} m is "
                f"{snap_error_m:.3f} m from the nearest planned point "
                f"({x_m:.3f} m) — more than half a step")
        for w in position.get("warnings", []):
            gate_failures.append(w)
        if gate_failures and not operator_override:
            return {"accepted": False, "gate_failures": gate_failures,
                    "quality": quality,
                    "hint": "fix the issue or re-submit with operator_override=true"}

        entry = self.store.add_segment(exp_id, seg)
        profile = result.products["range_profile"]
        progress = builder.add_column(x_m, profile, quality,
                                      plan["position_uncertainty_m"])
        self._persist_scan(exp_id)
        return {"accepted": True, "segment": entry["segment_id"],
                "override_used": bool(gate_failures), "gate_failures": gate_failures,
                "progress": progress, "quality": quality,
                "status": builder.status()}

    def scan_render(self, exp_id: str, interpolate: bool = False,
                    remove_mean_trace: bool = False) -> dict:
        if exp_id not in self.scans:
            self.scan_resume(exp_id)
        return self.scans[exp_id]["builder"].render(
            interpolate_missing=interpolate, remove_mean_trace=remove_mean_trace)

    def scan_finalize(self, exp_id: str) -> dict:
        if exp_id not in self.scans:
            self.scan_resume(exp_id)
        builder = self.scans[exp_id]["builder"]
        image = builder.render()
        self.store.add_derived(exp_id, "bscan", image,
                               {"stages": [{"stage": "bscan_assembly",
                                            "version": "1.0",
                                            "params": {"interpolate": False}}],
                                "fingerprint": "bscan-1.0"},
                               [c["segment_id"] for c in
                                self.store.load(exp_id)["segments"]])
        status = "finalized" if not image["status"]["pending"] else "partial"
        manifest = self.store.finalize(exp_id, status=status)
        return {"scan_id": exp_id, "status": manifest["identity"]["status"],
                "image": image}

    # -- Live RF (release 0.1) ---------------------------------------------
    def live_frame(self, device_id: str, num_samples: int = 16384) -> dict:
        """One decimated live frame; independent of any recording (FR-API-002)."""
        dev = self.device(device_id)
        with self.device_locks[device_id]:
            seg = dev.receive(num_samples)
        pipe = Pipeline(LIVE_PIPELINE)
        ctx = PipelineContext(sample_rate_hz=seg.sample_rate_hz,
                              center_frequency_hz=seg.center_frequency_hz,
                              waveform=seg.waveform)
        result = pipe.run(seg.iq, ctx)
        step = max(1, len(seg.iq) // 512)
        iq_dec = seg.iq[::step]
        return {
            "t": seg.timestamp,
            "device_id": device_id,
            "tx_active": seg.tx_active,
            "spectrum": result.products["spectrum"],
            "quality": result.products["quality"],
            "clipped": seg.clipped,
            "loss_events": seg.loss_events,
            "telemetry": seg.telemetry,
            "iq_preview": {
                "i": np.round(iq_dec.real, 4).tolist(),
                "q": np.round(iq_dec.imag, 4).tolist(),
            },
            "config": seg.config,
        }

    def record_capture(self, device_id: str, num_samples: int = 262144,
                       segments: int = 1, name: str = "raw capture",
                       operator: str = "", waveform_name: str = "",
                       tags: list[str] | None = None) -> dict:
        """Record raw I/Q into a new experiment package (UX-LIVE-006)."""
        dev = self.device(device_id)
        bytes_needed = num_samples * segments * 8
        stats = self.store.storage_stats()
        if bytes_needed > stats["disk_free_bytes"] - (1 << 30):
            raise ValueError("insufficient free space for requested capture")
        tx_started_here = False
        if waveform_name:
            wf = CATALOG[waveform_name]
            self._enable_tx(dev, wf, dev.config.tx_gain_db)
            tx_started_here = True
        try:
            manifest = self.store.create(
                name=name, kind="capture", operator=operator, tags=tags or [],
                objective="raw I/Q recording",
                hardware={"device_id": device_id, "kind": dev.kind,
                          "rf_chain": self.current_chain()},
                rf_config=dev.config.to_dict())
            exp_id = manifest["identity"]["experiment_id"]
            entries = []
            with self.device_locks[device_id]:
                for _ in range(segments):
                    seg = dev.receive(num_samples)
                    entries.append(self.store.add_segment(exp_id, seg))
        finally:
            if tx_started_here:
                self._disable_tx(dev)
        self.store.finalize(exp_id)
        return {"experiment_id": exp_id, "segments": entries,
                "bytes_estimate": bytes_needed}

    # -- Scene Builder / World View (release 0.4) ----------------------------
    def _scan_result(self, placement: dict, migration_params: dict | None = None,
                     detect_params: dict | None = None) -> dict:
        """Load a registered scan, migrate it, and extract focused targets."""
        exp_id = placement["experiment_id"]
        manifest = self.store.load(exp_id)
        try:
            bscan = self.store.load_derived(exp_id, "bscan")["product"]
        except FileNotFoundError as exc:
            raise ValueError(
                f"{exp_id} has no finalized B-scan; finalize the scan in Scan "
                "Studio before registering it to a site") from exc
        medium = manifest.get("calibration", {}).get("propagation_model", {})
        migrated = migrate_bscan(
            bscan["positions_m"], bscan["ranges_m"], bscan["magnitude_db"],
            **(migration_params or {}))
        targets = focused_targets(migrated, **(detect_params or {}))
        return {
            "experiment_id": exp_id,
            "placement": placement,
            "medium": medium,
            "migrated": migrated,
            "targets": targets,
            "measured_columns": migrated["measured_columns"],
            "path": scan_path(placement, bscan["positions_m"]),
            "name": manifest["identity"]["name"],
        }

    def site_scene(self, site_id: str, tolerance_m: float = 0.6,
                   slice_depth_m: float | None = None,
                   migration_params: dict | None = None,
                   detect_params: dict | None = None, ctx=None) -> dict:
        """Build the fused world view for a site (Milestone D)."""
        site = self.sites.load(site_id)
        results, errors = [], []
        total = max(1, len(site["scans"]))
        for i, placement in enumerate(site["scans"]):
            if ctx is not None:
                ctx.check()
                ctx.progress(i / total,
                             f"focusing {placement.get('label', '')} "
                             f"({i + 1}/{total})")
            try:
                results.append(self._scan_result(placement, migration_params,
                                                 detect_params))
            except Exception as exc:  # noqa: BLE001 - report per-scan failures
                errors.append({"experiment_id": placement["experiment_id"],
                               "error": str(exc)})
        findings = fuse_targets(results, tolerance_m=tolerance_m)
        slice_out = None
        if slice_depth_m is not None:
            slice_out = depth_slice(results, float(slice_depth_m))
        return {
            "site": site,
            "scans": [{k: v for k, v in r.items() if k != "migrated"}
                      for r in results],
            "migrated": {r["experiment_id"]: r["migrated"] for r in results},
            "findings": findings,
            "depth_slice": slice_out,
            "errors": errors,
        }

    def site_report(self, site_id: str, tolerance_m: float = 0.6) -> dict:
        scene = self.site_scene(site_id, tolerance_m=tolerance_m)
        results = [{**s, "migrated": scene["migrated"].get(s["experiment_id"])}
                   for s in scene["scans"]]
        text = site_report(scene["site"], results, scene["findings"],
                           __import__("forge_vision").__version__)
        return {"site_id": site_id, "markdown": text,
                "findings": scene["findings"], "errors": scene["errors"]}

    # -- stepped-frequency synthesis (FR-WAV-002, §17) -----------------------
    def stepped_run(self, device_id: str, start_hz: float, stop_hz: float,
                    waveform_name: str = "fmcw_pluto_40M",
                    overlap: float = 0.5, chirps: int = 4,
                    medium: str | dict | None = None,
                    correction: str = "overlap",
                    max_range_m: float = 20.0, name: str = "stepped-frequency run",
                    operator: str = "", ctx=None) -> dict:
        """Sweep the LO across a band and synthesise the wide bandwidth.

        Chunks overlap so the arbitrary phase each PLL retune lands on can be
        solved for and removed; without that the chunks add incoherently and
        the result is worse than a single sweep.
        """
        dev = self.device(device_id)
        wf = self._check_waveform(dev, waveform_name)
        caps = dev.capabilities
        med = self._medium_from(medium)

        chunk_bw = wf.bandwidth_hz
        if chunk_bw <= 0:
            raise ValueError(f"{waveform_name} has no sweep bandwidth")
        if not 0.0 <= overlap < 1.0:
            raise ValueError("overlap must be in [0, 1)")
        step = chunk_bw * (1.0 - overlap)

        # keep every chunk wholly inside what the radio can actually tune
        lo = max(float(start_hz), caps.min_frequency + chunk_bw / 2)
        hi = min(float(stop_hz), caps.max_frequency - chunk_bw / 2)
        if hi < lo:
            raise ValueError(
                f"requested {start_hz/1e6:.0f}-{stop_hz/1e6:.0f} MHz leaves no "
                f"room for a {chunk_bw/1e6:.0f} MHz chunk inside the device "
                f"range {caps.min_frequency/1e6:.0f}-{caps.max_frequency/1e6:.0f} MHz")
        centers = list(np.arange(lo, hi + step * 0.5, step))
        if len(centers) > 256:
            raise ValueError(f"{len(centers)} chunks requested; widen the step "
                             "or narrow the band (limit 256)")

        original = DeviceConfig(**dev.config.to_dict())
        bands = []
        try:
            with self.device_locks[device_id]:
                for i, fc in enumerate(centers):
                    if ctx is not None:
                        ctx.check()
                        ctx.progress(i / len(centers),
                                     f"{fc/1e6:.0f} MHz ({i+1}/{len(centers)})")
                    cfg = DeviceConfig(**{**dev.config.to_dict(),
                                          "center_frequency_hz": float(fc),
                                          "sample_rate_hz": wf.sample_rate,
                                          "rx_bandwidth_hz": min(
                                              wf.sample_rate, caps.max_bandwidth)})
                    dev.configure(cfg)
                    self._enable_tx(dev, wf, dev.config.tx_gain_db)
                    try:
                        seg = dev.receive(wf.num_samples * chirps)
                    finally:
                        self._disable_tx(dev)
                    bands.append(subband_response(seg.iq, wf.preview(), float(fc),
                                                  seg.sample_rate_hz))
        finally:
            dev.configure(original)

        stitched = stitch_subbands(bands, correction=correction)
        profile = stepped_range_profile(stitched, medium=med.to_dict(),
                                        max_range_m=max_range_m)

        manifest = self.store.create(
            name=name, kind="stepped", operator=operator,
            objective=f"stepped-frequency synthesis {lo/1e6:.0f}-{hi/1e6:.0f} MHz "
                      f"in {len(centers)} x {chunk_bw/1e6:.0f} MHz chunks",
            hardware={"device_id": device_id, "kind": dev.kind,
                      "rf_chain": self.current_chain()},
            rf_config={**original.to_dict(), "chunk_waveform": waveform_name,
                       "centers_hz": [float(c) for c in centers],
                       "overlap": overlap, "chirps": chirps},
            calibration={"propagation_model": med.to_dict(),
                         "cable_delay_s": self.calibration[device_id]["cable_delay_s"]})
        exp_id = manifest["identity"]["experiment_id"]
        self.store.add_derived(exp_id, "stepped_profile", profile,
                               {"stages": [{"stage": "subband_response",
                                            "version": "1.0", "params": {}},
                                           {"stage": "stitch_subbands",
                                            "version": "1.0",
                                            "params": {"correction": correction}},
                                           {"stage": "stepped_range_profile",
                                            "version": "1.0",
                                            "params": {"max_range_m": max_range_m}}],
                                "fingerprint": "stepped-1.0"}, [])
        self.store.finalize(exp_id)
        self.safety.audit("stepped_run", device=device_id, chunks=len(centers),
                          synthetic_bandwidth_hz=stitched["synthetic_bandwidth_hz"],
                          experiment=exp_id)
        return {"experiment_id": exp_id, **profile}

    # -- SAGE assistance (release 0.5, §8) -----------------------------------
    def _narrate(self, answer: dict, narrate: bool = True) -> dict:
        """Optionally add LLM prose over the facts. Never changes the facts."""
        if not narrate:
            return answer
        from ..sage.narrate import narrate as add_narration
        return add_narration(answer, self.llm.active())

    def sage_ask(self, question: str, site_id: str = "",
                 experiment_id: str = "", narrate: bool = False) -> dict:
        """Answer a grounded question. Read-only by construction (FR-AI-008):
        this path has no way to enable transmission or change any setting.

        Narration is off by default and fetched separately: a local model can
        take 30 s to 2 minutes to rephrase a handful of facts, and the
        instrument's own answer must never wait on it.
        """
        from ..sage.query import ask
        scene = self.site_scene(site_id) if site_id else None
        out = ask(question, store=self.store, scene=scene,
                  experiment_id=experiment_id)
        out["narration_available"] = self.llm.active() is not None
        return self._narrate(out, narrate)

    def sage_narrate(self, answer: dict) -> dict:
        """Narrate an answer produced earlier. Returns only the narration
        block, so a slow model never blocks the findings."""
        active = self.llm.active()
        if active is None:
            return {"available": False,
                    "error": "no language model endpoint is enabled",
                    "note": "The findings are the instrument's own output and "
                            "do not depend on a model."}
        return self._narrate(answer, True).get("narration", {"available": False})

    # -- LLM endpoints (optional narration layer) ----------------------------
    def llm_list(self) -> dict:
        from ..sage.narrate import health
        eps = self.llm.load()
        active = self.llm.active()
        return {"endpoints": [e.to_dict() for e in eps.values()],
                "active": active.name if active else "",
                "health": {active.name: health(active)} if active else {}}

    def llm_put(self, spec: dict) -> dict:
        from ..sage.narrate import LLMEndpoint
        self.llm.load()
        allowed = {"name", "base_url", "model", "api_key", "timeout_s",
                   "max_tokens", "enabled"}
        ep = LLMEndpoint(**{k: v for k, v in spec.items() if k in allowed})
        self.llm.put(ep)
        self.safety.audit("llm_endpoint_configured", name=ep.name,
                          base_url=ep.base_url, model=ep.model,
                          enabled=ep.enabled)
        return self.llm_list()

    def llm_remove(self, name: str) -> dict:
        self.llm.load()
        self.llm.remove(name)
        return self.llm_list()

    def llm_health(self, name: str) -> dict:
        from ..sage.narrate import health
        eps = self.llm.load()
        if name not in eps:
            raise KeyError(f"unknown endpoint: {name}")
        return health(eps[name])

    def _with_narration_flag(self, out: dict) -> dict:
        out["narration_available"] = self.llm.active() is not None
        return out

    def sage_experiment(self, experiment_id: str) -> dict:
        from ..sage.analysis import assess_experiment, summarize_experiment
        from ..sage.facts import answer
        return self._with_narration_flag(answer(
            summarize_experiment(self.store, experiment_id)
            + assess_experiment(self.store, experiment_id),
            f"summary and quality assessment of {experiment_id}"))

    def sage_explain(self, site_id: str, index: int) -> dict:
        from ..sage.analysis import explain_finding
        from ..sage.facts import answer
        scene = self.site_scene(site_id)
        findings = scene["findings"]
        if not 0 <= index < len(findings):
            raise KeyError(f"site has {len(findings)} finding(s); "
                           f"no #{index + 1}")
        return self._with_narration_flag(answer(
            explain_finding(scene["site"], findings[index], index),
            f"why is finding #{index + 1} highlighted?"))

    def sage_recommend(self, site_id: str) -> dict:
        from ..sage.analysis import recommend_next
        from ..sage.facts import answer
        return self._with_narration_flag(answer(
            recommend_next(self.site_scene(site_id)),
            "what should I measure next?"))

    def sage_compare(self, a_id: str, b_id: str) -> dict:
        from ..sage.analysis import compare_experiments
        from ..sage.facts import answer
        return self._with_narration_flag(answer(
            compare_experiments(self.store, a_id, b_id),
            f"compare {a_id} with {b_id}"))

    # -- band survey (receive only) ------------------------------------------
    def band_survey(self, device_id: str, start_hz: float, stop_hz: float,
                    step_hz: float = 2e6, sample_rate_hz: float = 2.5e6,
                    rx_gain_db: float = 40.0, samples: int = 65536,
                    name: str = "band survey", operator: str = "",
                    ctx=None) -> dict:
        """Sweep a frequency range with the receiver only and report occupancy.

        Transmits nothing, so it is safe before any TX bring-up, and answers
        the practical question "where is it quiet enough to transmit?".
        Results are stored as a normal experiment package.
        """
        dev = self.device(device_id)
        if not dev.connected:
            raise ValueError(f"{device_id} is not connected")
        if self.stop_acquisition.is_set():
            raise ValueError("acquisition is stopped after an emergency stop; "
                             "resume before starting a new sweep")
        caps = dev.capabilities
        start_hz = max(float(start_hz), caps.min_frequency)
        stop_hz = min(float(stop_hz), caps.max_frequency)
        if stop_hz <= start_hz:
            raise ValueError("stop frequency must be above start frequency, "
                             f"and both inside {caps.min_frequency:.4g}-"
                             f"{caps.max_frequency:.4g} Hz")
        step_hz = max(float(step_hz), 0.1e6)
        steps = int((stop_hz - start_hz) / step_hz) + 1
        if steps > 400:
            raise ValueError(f"{steps} steps requested; widen the step size "
                             "(limit 400 per survey)")

        original = DeviceConfig(**dev.config.to_dict())
        manifest = self.store.create(
            name=name, kind="survey", operator=operator,
            objective=f"receive-only occupancy survey {start_hz / 1e6:.1f}-"
                      f"{stop_hz / 1e6:.1f} MHz",
            hardware={"device_id": device_id, "kind": dev.kind,
                      "rf_chain": self.current_chain()},
            rf_config={"sample_rate_hz": sample_rate_hz,
                       "rx_gain_db": rx_gain_db, "step_hz": step_hz})
        exp_id = manifest["identity"]["experiment_id"]

        points = []
        try:
            with self.device_locks[device_id]:
                for i in range(steps):
                    freq = start_hz + i * step_hz
                    cfg = DeviceConfig(**{**dev.config.to_dict(),
                                          "center_frequency_hz": freq,
                                          "sample_rate_hz": sample_rate_hz,
                                          "rx_bandwidth_hz": min(
                                              sample_rate_hz, caps.max_bandwidth),
                                          "rx_gain_db": rx_gain_db})
                    if ctx is not None:
                        ctx.check()
                        ctx.progress(i / steps,
                                     f"{freq / 1e6:.1f} MHz ({i + 1}/{steps})")
                    dev.configure(cfg)
                    seg = dev.receive(samples)
                    points.append(_survey_point(freq, seg))
        finally:
            dev.configure(original)     # always restore the operator's config

        floors = [p["noise_floor_dbfs"] for p in points]
        quietest = min(points, key=lambda p: p["peak_dbfs"])
        busiest = max(points, key=lambda p: p["peak_dbfs"])
        product = {
            "start_hz": start_hz, "stop_hz": stop_hz, "step_hz": step_hz,
            "sample_rate_hz": sample_rate_hz, "rx_gain_db": rx_gain_db,
            "points": points,
            "median_noise_floor_dbfs": round(float(np.median(floors)), 1),
            "quietest": quietest, "busiest": busiest,
        }
        self.store.add_derived(
            exp_id, "band_survey", product,
            {"stages": [{"stage": "band_survey", "version": "1.0",
                         "params": {"step_hz": step_hz, "samples": samples,
                                    "rx_gain_db": rx_gain_db}}],
             "fingerprint": "band_survey-1.0"}, [])
        self.store.finalize(exp_id)
        # A survey run through a saved configuration is a measurement OF that
        # configuration — that is what makes it re-measurable later.
        self._record_chain_measurement(exp_id, "survey", {
            "start_hz": start_hz, "stop_hz": stop_hz,
            "median_noise_floor_dbfs": product["median_noise_floor_dbfs"],
            "busiest_hz": busiest["center_hz"],
            "busiest_peak_above_floor_db": busiest["peak_above_floor_db"],
        })
        self.safety.audit("band_survey", device=device_id, start_hz=start_hz,
                          stop_hz=stop_hz, steps=steps, experiment=exp_id)
        return {"experiment_id": exp_id, **product}

    def _record_chain_measurement(self, exp_id: str, kind: str,
                                  summary: dict) -> None:
        """Attach a measurement to the active configuration, if one is active
        and the patching still matches it. A reading taken through an edited
        chain is not a measurement of the saved configuration (FR-RFC-007)."""
        w = self.chains.working()
        if not w["config_id"] or w["modified"]:
            return
        try:
            self.chains.record_measurement(w["config_id"], exp_id, kind, summary)
        except FileNotFoundError:
            pass

    # -- simulator control ---------------------------------------------------
    def set_sim_scene(self, device_id: str, preset: str = "",
                      targets: list[dict] | None = None,
                      medium: str | dict | None = None,
                      noise_floor_dbfs: float | None = None,
                      leakage_amplitude: float | None = None) -> dict:
        dev = self.device(device_id)
        if not isinstance(dev, SimulatedPluto):
            raise ValueError("scene control only applies to simulated devices")
        if preset == "bench":
            scene = default_bench_scene()
        elif preset == "scan":
            scene = default_scan_scene()
        else:
            scene = dev.scene
        if targets is not None:
            scene = SimScene(targets=[SceneTarget(**t) for t in targets],
                             medium=scene.medium,
                             leakage_amplitude=scene.leakage_amplitude,
                             leakage_delay_s=scene.leakage_delay_s,
                             noise_floor_dbfs=scene.noise_floor_dbfs,
                             seed=scene.seed)
        if medium is not None:
            scene.medium = self._medium_from(medium)
        if noise_floor_dbfs is not None:
            scene.noise_floor_dbfs = float(noise_floor_dbfs)
        if leakage_amplitude is not None:
            scene.leakage_amplitude = float(leakage_amplitude)
        dev.set_scene(scene)
        return scene.to_dict()

    # -- dashboard -----------------------------------------------------------
    # -- long-running jobs (FR-API-003) --------------------------------------
    def submit_job(self, kind: str, params: dict) -> dict:
        """Run one of the slow operations in the background."""
        builders = {
            "survey": self._job_survey,
            "site_scene": self._job_site_scene,
            "replay": self._job_replay,
        }
        if kind not in builders:
            raise KeyError(f"unknown job kind: {kind}; "
                           f"expected one of {sorted(builders)}")
        fn, description = builders[kind](params)
        job = self.jobs.submit(kind, description, fn, params)
        self.safety.audit("job_submitted", job_id=job.job_id, kind=kind)
        return job.to_dict()

    def _job_survey(self, p: dict):
        def run(ctx):
            return self.band_survey(
                device_id=p.get("device_id", "sim-pluto-0"),
                start_hz=float(p.get("start_hz", 902e6)),
                stop_hz=float(p.get("stop_hz", 928e6)),
                step_hz=float(p.get("step_hz", 2e6)),
                sample_rate_hz=float(p.get("sample_rate_hz", 2.5e6)),
                rx_gain_db=float(p.get("rx_gain_db", 40.0)),
                samples=int(p.get("samples", 65536)),
                name=p.get("name", "band survey"),
                operator=p.get("operator", ""), ctx=ctx)
        lo, hi = p.get("start_hz", 902e6) / 1e6, p.get("stop_hz", 928e6) / 1e6
        return run, f"band survey {lo:.0f}-{hi:.0f} MHz"

    def _job_site_scene(self, p: dict):
        site_id = p["site_id"]
        def run(ctx):
            return self.site_scene(site_id,
                                   tolerance_m=float(p.get("tolerance_m", 0.6)),
                                   slice_depth_m=p.get("slice_depth_m"), ctx=ctx)
        return run, f"build scene for site {site_id}"

    def _job_replay(self, p: dict):
        exp_id = p["experiment_id"]
        def run(ctx):
            ctx.progress(0.1, "reprocessing stored raw data")
            return self.replay(exp_id, medium=p.get("medium"),
                               pipeline_overrides=p.get("pipeline_overrides"))
        return run, f"replay {exp_id}"

    def job_status(self, job_id: str, include_result: bool = False) -> dict:
        return self.jobs.get(job_id).to_dict(include_result=include_result)

    # -- position sources (FR-POS-001/002, UX-SCN-002) ----------------------
    def set_position_source(self, kind: str, **opts) -> dict:
        """Switch between manual entry, a serial rig, or recorded positions."""
        old = self.position_source
        if kind == "manual":
            self.position_source = ManualSource(
                uncertainty_m=float(opts.get("uncertainty_m", 0.01)))
        elif kind == "serial":
            self.position_source = SerialSource(
                port=opts["port"], baud=int(opts.get("baud", 115200)),
                wheel_circumference_m=float(
                    opts.get("wheel_circumference_m", 0.0)),
                counts_per_revolution=int(
                    opts.get("counts_per_revolution", 0)),
                uncertainty_m=float(opts.get("uncertainty_m", 0.01)))
        elif kind == "replay":
            self.position_source = ReplaySource(opts.get("samples", []))
        else:
            raise KeyError(f"unknown position source: {kind}; "
                           "expected manual, serial, or replay")
        if old is not self.position_source:
            old.close()
        self.safety.audit("position_source_changed", kind=kind,
                          **{k: v for k, v in opts.items() if k != "samples"})
        return self.position_status()

    def position_status(self) -> dict:
        src = self.position_source
        status = src.status()
        sample = src.latest()          # observing must not consume a sample
        status["latest"] = sample.to_dict() if sample else None
        status["kind"] = src.name
        return status

    @property
    def rf_chain(self) -> dict:
        """The working chain. Persisted, so it survives a restart."""
        w = self.chains.working()
        return {k: w[k] for k in ("tx_ids", "rx_ids", "antenna_tx", "antenna_rx")}

    def set_rf_chain(self, tx_ids=None, rx_ids=None, antenna_tx: str = "",
                     antenna_rx: str = "") -> dict:
        """Declare the cable/adapter chain currently patched up (FR-RFC-006)."""
        self.chains.set_working(tx_ids, rx_ids, antenna_tx, antenna_rx)
        self.safety.audit("rf_chain_declared", **self.rf_chain)
        return self.current_chain()

    def current_chain(self) -> dict:
        """Resolve the working chain for the experiment record.

        Carries the saved configuration it came from, and whether it still
        matches it. A capture taken after the operator edited the patching
        must not read as though it came from the pristine named configuration
        (FR-RFC-007).
        """
        w = self.chains.working()
        chain = self.components.describe_chain(
            w["tx_ids"], w["rx_ids"], w["antenna_tx"], w["antenna_rx"])
        chain["config_id"] = w["config_id"]
        chain["config_name"] = w["config_name"]
        if w["modified"]:
            chain["config_modified"] = True
            chain["note"] = (chain.get("note", "") + " " if chain.get("note") else "") + (
                f"Patched chain differs from saved configuration "
                f"{w['config_name']!r}; it is not that configuration.")
        return chain

    # -- saved chain configurations (FR-RFC-006) ------------------------------
    def list_chain_configs(self) -> list[dict]:
        return self.chains.list()

    def save_chain_config(self, name: str, notes: str = "") -> dict:
        cfg = self.chains.save_working_as(name, notes=notes)
        self.safety.audit("chain_config_saved", config_id=cfg["config_id"],
                          name=cfg["name"])
        return cfg

    def activate_chain_config(self, config_id: str) -> dict:
        cfg = self.chains.activate(config_id)
        self.safety.audit("chain_config_activated", config_id=config_id,
                          name=cfg["name"])
        return self.current_chain()

    def detach_chain_config(self) -> dict:
        """Stop claiming the working chain came from a saved configuration.

        Without this, clearing the patching leaves a chain that still reports
        itself as a modified version of whatever was last active — technically
        true, but it reads as an unresolved problem rather than a fresh start.
        """
        w = self.chains.working()
        self.chains.set_working(w["tx_ids"], w["rx_ids"], w["antenna_tx"],
                                w["antenna_rx"], config_id="")
        return self.current_chain()

    def delete_chain_config(self, config_id: str) -> dict:
        self.chains.delete(config_id)
        return {"deleted": config_id}

    def chain_config_measurements(self, config_id: str) -> dict:
        """A configuration plus the measurements taken with it."""
        cfg = self.chains.load(config_id)
        out = []
        for m in cfg.get("measurements", []):
            try:
                man = self.store.load(m["experiment_id"])
            except FileNotFoundError:
                # The capture was deleted; say so rather than dropping it.
                out.append({**m, "missing": True})
                continue
            out.append({**m, "name": man["identity"].get("name", ""),
                        "created_at": man["identity"].get("created_at")})
        return {**cfg, "measurements": out}

    def _describe_device(self, dev) -> dict:
        from ..devices import discovery
        d = dev.describe()
        # the UI must never offer a waveform this radio cannot transmit
        d["compatible_waveforms"] = dev.compatible_waveforms(CATALOG)
        # How the radio is attached, in terms an operator reads rather than a
        # libiio URI: the same board over Ethernet and over USB behaves very
        # differently, and the dashboard should not make you infer which from
        # the device id.
        uri = getattr(dev, "uri", "")
        info = getattr(dev, "discovery", {}) or {}
        alts = info.get("alternatives", [])
        d["link"] = {
            "uri": uri,
            "kind": discovery.uri_kind(uri) if uri else "simulated",
            "address": discovery.uri_address(uri),
            "throughput_mb_s": next(
                (t.get("throughput_mb_s") for t in alts if t.get("uri") == uri),
                None),
            "chosen_because": info.get("reason", ""),
            "alternatives": [
                {k: t.get(k) for k in ("uri", "kind", "throughput_mb_s", "error")}
                for t in alts if t.get("uri") != uri],
        }
        return d

    def set_caps_profile(self, device_id: str, profile: str) -> dict:
        dev = self.device(device_id)
        if not hasattr(dev, "set_caps_profile"):
            raise ValueError("capability profiles only apply to simulated devices")
        notes = dev.set_caps_profile(profile)
        self.safety.audit("sim_caps_profile_changed", device=device_id,
                          profile=profile, clamped=notes)
        return {**self._describe_device(dev), "clamp_notes": notes}

    def status(self) -> dict:
        return {
            "version": __import__("forge_vision").__version__,
            "devices": [self._describe_device(d) for d in self.devices.values()],
            "safety": self.safety.status(),
            "acquisition_stopped": self.stop_acquisition.is_set(),
            "storage": self.store.storage_stats(),
            "recent_experiments": self.store.list()[:8],
            "active_scans": {k: v["builder"].status() for k, v in self.scans.items()},
            "media_presets": {k: m.to_dict() for k, m in MEDIA_PRESETS.items()},
            "waveforms": {k: w.preview() for k, w in CATALOG.items()},
        }
