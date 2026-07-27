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
from ..experiments.store import ExperimentStore
from ..imaging.bscan import BScanBuilder
from ..safety import SafetyController
from ..waveforms import CATALOG


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
        self.safety = SafetyController(
            SafetyLimits(), os.path.join(self.data_dir, "logs", "safety_audit.jsonl"))
        self.devices: dict[str, object] = {}
        self.device_locks: dict[str, threading.Lock] = {}
        self.calibration: dict[str, dict] = {}       # device_id -> assets
        self.scans: dict[str, dict] = {}             # scan experiment_id -> session
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
            for dev in PlutoDevice.discover():
                self._register(dev)
        except Exception:  # noqa: BLE001 - hardware discovery is best-effort
            pass

    def device(self, device_id: str):
        if device_id not in self.devices:
            raise KeyError(f"unknown device: {device_id}")
        return self.devices[device_id]

    def connect(self, device_id: str) -> dict:
        dev = self.device(device_id)
        dev.connect()
        self.safety.audit("device_connected", device=device_id)
        return dev.describe()

    def disconnect(self, device_id: str) -> dict:
        dev = self.device(device_id)
        dev.disconnect()   # fault-safe TX off inside adapter
        self.safety.notify_tx_stopped(device_id, reason="disconnect")
        self.safety.audit("device_disconnected", device=device_id)
        return dev.describe()

    def configure(self, device_id: str, cfg: dict) -> dict:
        dev = self.device(device_id)
        merged = {**dev.config.to_dict(), **cfg}
        dev.configure(DeviceConfig(**merged))
        return dev.describe()

    # -- safety ------------------------------------------------------------
    def emergency_stop(self) -> dict:
        results = self.safety.emergency_stop(self.devices.values())
        return {"stopped": True, "results": results}

    def _enable_tx(self, dev, waveform, tx_gain_db: float) -> None:
        self.safety.validate_tx(dev.config.center_frequency_hz, waveform, tx_gain_db)
        dev.load_waveform(waveform)
        dev.enable_tx()
        self.safety.notify_tx_started(
            dev.device_id, waveform=waveform.name,
            frequency_hz=dev.config.center_frequency_hz, tx_gain_db=tx_gain_db)

    def _disable_tx(self, dev, reason: str = "normal") -> None:
        dev.force_tx_off()
        self.safety.notify_tx_stopped(dev.device_id, reason=reason)

    def set_tx(self, device_id: str, enable: bool, waveform_name: str = "") -> dict:
        dev = self.device(device_id)
        if enable:
            wf = CATALOG[waveform_name]
            problems = wf.validate(dev.capabilities)
            if problems:
                raise ValueError("; ".join(problems))
            self._enable_tx(dev, wf, dev.config.tx_gain_db)
        else:
            self._disable_tx(dev)
        return dev.describe()

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
        wf = CATALOG[waveform_name]
        seg = self._ranging_capture(dev, wf, chirps)
        result = self._process_range(seg, Medium(),
                                     self.calibration[device_id]["cable_delay_s"], None)
        manifest = self.store.create(
            name="background capture", kind="calibration", operator=operator,
            objective="static scene baseline for background subtraction",
            hardware={"device_id": device_id, "kind": dev.kind},
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
        wf = CATALOG[waveform_name]
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
            hardware={"device_id": device_id, "kind": dev.kind},
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
            hardware={"device_id": device_id, "kind": dev.kind},
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

    def scan_point(self, exp_id: str, x_m: float, operator_override: bool = False) -> dict:
        """Capture one scan point with quality gating (UX-SCN-003)."""
        if exp_id not in self.scans:
            self.scan_resume(exp_id)
        session = self.scans[exp_id]
        builder: BScanBuilder = session["builder"]
        plan = session["plan"]
        dev = self.device(session["device_id"])
        if builder.index_of(x_m) is None:
            raise ValueError(f"position {x_m} m is outside the scan plan")

        wf = CATALOG[plan["waveform"]]
        med = self._medium_from(plan["medium"])
        cal = self.calibration[dev.device_id]
        position = {"x_m": x_m,
                    "uncertainty_m": plan["position_uncertainty_m"],
                    "height_m": plan["antenna_height_m"]}
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
                hardware={"device_id": device_id, "kind": dev.kind},
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
    def status(self) -> dict:
        return {
            "version": __import__("forge_vision").__version__,
            "devices": [d.describe() for d in self.devices.values()],
            "safety": self.safety.status(),
            "storage": self.store.storage_stats(),
            "recent_experiments": self.store.list()[:8],
            "active_scans": {k: v["builder"].status() for k, v in self.scans.items()},
            "media_presets": {k: m.to_dict() for k, m in MEDIA_PRESETS.items()},
            "waveforms": {k: w.preview() for k, w in CATALOG.items()},
        }
