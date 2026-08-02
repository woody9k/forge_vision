"""Forge Vision runtime: orchestrates devices, safety, DSP, and storage.

This layer is UI-agnostic (§4.1: hardware access isolated from the UI) and is
exercised directly by the test suite. The FastAPI app in `app.py` is a thin
routing shell over this class.
"""

from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

from ..config import DEFAULT_DATA_DIR, MEDIA_PRESETS, Medium, SafetyLimits
from ..devices.base import ConfigurationError, DeviceConfig
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


def bin_by_bearing(points: list, bin_deg: float = 5.0) -> dict:
    """Group power measurements into bearing bins (FR-POS-003, FR-ACQ-001).

    A bearing the antenna never pointed at is **unmeasured**, and that is not
    the same claim as "nothing was there". Empty bins are returned with
    `samples: 0` and a null level rather than a floor value, so a polar plot
    can leave that sector blank instead of drawing a hole in the pattern that
    looks like a null the antenna does not have.

    Each bin also carries how many captures landed in it. A sector crossed
    once while swinging the antenna is a weaker claim than one held steady,
    and the display should be able to say so.
    """
    if bin_deg <= 0:
        raise ValueError("bearing bin size must be positive")
    count = int(round(360.0 / bin_deg))
    bins = [{"bearing_deg": round(i * 360.0 / count, 3), "samples": 0,
             "peak_dbfs": None, "mean_dbfs": None, "spread_db": None,
             "clipped": False} for i in range(count)]
    buckets: dict = {}
    for p in points:
        heading = p.get("heading_deg")
        if heading is None or p.get("peak_dbfs") is None:
            continue                       # no bearing: cannot place it
        idx = int(round((heading % 360.0) / 360.0 * count)) % count
        buckets.setdefault(idx, []).append(p)

    for idx, group in buckets.items():
        levels = [g["peak_dbfs"] for g in group]
        b = bins[idx]
        b["samples"] = len(group)
        b["peak_dbfs"] = round(max(levels), 1)
        b["mean_dbfs"] = round(sum(levels) / len(levels), 1)
        b["spread_db"] = round(max(levels) - min(levels), 1)
        b["clipped"] = any(g.get("clipped") for g in group)

    measured = [b for b in bins if b["samples"]]
    covered = len(measured)
    out = {
        "bin_deg": bin_deg,
        "bins": bins,
        "bins_measured": covered,
        "bins_total": count,
        "coverage": round(covered / count, 3),
        "unmeasured_bearings": [b["bearing_deg"] for b in bins
                                if not b["samples"]],
    }
    if measured:
        best = max(measured, key=lambda b: b["peak_dbfs"])
        out["strongest"] = {"bearing_deg": best["bearing_deg"],
                            "peak_dbfs": best["peak_dbfs"],
                            "samples": best["samples"]}
        # Front-to-back needs the bin 180 degrees round to have been visited;
        # without it the ratio is not available rather than zero.
        opposite = bins[(bins.index(best) + count // 2) % count]
        if opposite["samples"]:
            out["front_to_back_db"] = round(
                best["peak_dbfs"] - opposite["peak_dbfs"], 1)
        else:
            out["front_to_back_note"] = (
                f"The bearing opposite the strongest ({opposite['bearing_deg']:.0f}"
                "°) was never measured, so front-to-back ratio is unavailable.")
    return out


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
        self._safety_state_path = os.path.join(self.data_dir, "safety_state.json")
        self.profile_restore = self._restore_frequency_profile()
        self._device_config_path = os.path.join(self.data_dir,
                                                "device_configs.json")
        self._saved_device_configs = self._load_device_configs()
        self.device_config_restore: dict[str, dict] = {}
        self.devices: dict[str, object] = {}
        self.device_locks: dict[str, threading.Lock] = {}
        self.calibration: dict[str, dict] = {}       # device_id -> assets
        self.scans: dict[str, dict] = {}             # scan experiment_id -> session
        self.jobs = JobManager()                     # FR-API-003
        self._tx_waveforms: dict[str, object] = {}   # device -> live waveform
        # set by emergency stop and shutdown; long loops check it
        self.stop_acquisition = threading.Event()
        # device_id -> last sync_status(); the watchdog fills these in
        self._sync_records: dict[str, dict] = {}
        self._sync_thread: threading.Thread | None = None
        self._sync_stop = threading.Event()
        self.sync_interval_s = float(
            os.environ.get("FORGE_VISION_SYNC_INTERVAL", "15"))
        self.position_source = ManualSource()        # FR-POS-001 default
        self._register(SimulatedPluto("sim-pluto-0"))
        self._discover_hardware()

    # -- devices -----------------------------------------------------------
    def _register(self, dev, carry_config=None) -> None:
        self.devices[dev.device_id] = dev
        self.device_locks[dev.device_id] = threading.Lock()
        self.calibration.setdefault(dev.device_id, {
            "cable_delay_s": 0.0,
            "background": None,
            "leakage_baseline": None,
        })
        # Note the ordering, because it is the opposite of what it looks like:
        # a real radio arrives here **already connected**, since
        # `PlutoDevice.discover()` calls `connect()` before the runtime ever
        # sees the device and `connect()` has already pushed defaults at the
        # hardware. `_restore_device_config` therefore has to apply to the
        # hardware itself, not merely assign the cache. Only a device that is
        # still disconnected can rely on a later `connect()` to do it.
        self._restore_device_config(dev, carry_config=carry_config)
        # Here rather than inside the restore, which has several early returns
        # — including the common "no saved config" one — so the re-check would
        # not have held for most registrations. A no-op today (every caller
        # hands over an adapter with tx_enabled False), kept so the property
        # holds by construction rather than by an invariant nothing asserts.
        self.enforce_tx_authorization(f"{dev.device_id} registered")

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
        # Per-session records about a device that no longer exists. Left
        # behind these accumulate across transport switches and would be
        # served for a re-registered id that had not actually been checked.
        self.device_config_restore.pop(device_id, None)
        self._sync_records.pop(device_id, None)
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
        # Saved configurations are keyed by device id, which is per-URI, while
        # `usb:`, `ip:192.168.99.222` and `ip:192.168.2.1` are all one board.
        # Letting the new entry restore *its* key would push a stale saved
        # configuration onto the radio and report it as `restored` at the
        # moment it discarded the operator's current one. Carry the settings
        # across instead: this is the same board, reached another way.
        carried = dev.config
        self.forget_device(device_id)
        target.discovery = {**old_discovery, "uri": uri,
                            "reason": f"switched by operator to {uri}"}
        self._register(target, carry_config=carried)
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

    # -- state reconciliation (FR-DEV-002/007) -----------------------------
    #
    # `dev.config` records what was asked for. The radio is the authority on
    # what it actually has, and the two come apart more easily than they look:
    # the AD9361 driver clamps and quantizes silently, AGC overrides a gain
    # you just wrote, and anything else on the bench — a characterization
    # script, a second handle, a reboot — moves the board with no notification.
    # Showing the requested value as though it were the actual one is rule 1;
    # noticing and not saying is rule 3.

    def check_device_sync(self, device_id: str, blocking: bool = True) -> dict:
        """Compare a device against its hardware and record the result.

        Only *transitions* reach the audit log. A radio that has been adrift
        for an hour should not write a line every poll — that buries the
        moment it happened, which is the part worth finding later.
        """
        dev = self.device(device_id)
        if not getattr(dev, "connected", False):
            return {"device_id": device_id, "checked_at": time.time(),
                    "readable": False, "in_sync": None,
                    "error": "device is not connected", "drift": []}

        lock = self.device_locks[device_id]
        if not lock.acquire(blocking=blocking):
            # A capture holds this. Its configuration is in use and will be
            # checked on the next pass; never make the watchdog a source of
            # latency in the acquisition path.
            prev = self._sync_records.get(device_id, {})
            return {**prev, "device_id": device_id, "skipped": "device busy"}
        try:
            status = dev.sync_status()
        finally:
            lock.release()

        status["device_id"] = device_id
        previous = self._sync_records.get(device_id)
        # `forget_device` takes no lock, so it can drop this device during the
        # ~4 ms read above. Writing unconditionally then resurrected a record
        # for a device that no longer exists — and re-registering that id (a
        # transport switch back does exactly this) served `in_sync: true` for
        # a device nothing had checked, which inverts the rule that a missing
        # record must read as "not checked".
        # Identity, not membership: forget-and-re-register under the same id
        # (a transport switch away and back) would otherwise write the old
        # adapter's result for the new one.
        if self.devices.get(device_id) is not dev:
            return {**status, "stale": "device was replaced during the check"}
        self._sync_records[device_id] = status

        was = previous.get("in_sync") if previous else None
        now = status["in_sync"]
        if previous is not None and was != now:
            if now is False:
                self.safety.audit(
                    "device_drift_detected", device=device_id,
                    drift=status["drift"])
            elif now is True:
                self.safety.audit("device_drift_cleared", device=device_id)
        elif previous is None and now is False:
            self.safety.audit("device_drift_detected", device=device_id,
                              drift=status["drift"], first_check=True)
        return status

    def resync_device(self, device_id: str) -> dict:
        """Adopt the radio's own settings as the truth for a drifted device."""
        dev = self.device(device_id)
        with self.device_locks[device_id]:
            status = dev.adopt_hardware_state()
        status["device_id"] = device_id
        # Same identity check as the watchdog: narrower here (operator-driven,
        # and it held the lock) but the same shape of mistake if it happened.
        if self.devices.get(device_id) is dev:
            self._sync_records[device_id] = status
        self.safety.audit("device_resynced", device=device_id,
                          adopted=status.get("adopted"),
                          unresolved=[d["field"] for d in status["drift"]])
        # Permission was granted against a configuration that has now changed
        # underneath it, so it no longer describes the radio (rule 5).
        revoked = self.enforce_tx_authorization(f"resync of {device_id}")
        if revoked:
            status["tx_revoked"] = revoked
        return status

    def sync_record(self, device_id: str) -> dict | None:
        """The last recorded sync result, without touching the hardware."""
        return self._sync_records.get(device_id)

    def start_sync_watchdog(self, interval_s: float | None = None) -> dict:
        """Poll connected devices so drift is found rather than stumbled into.

        A full read-back measures ~4 ms over Ethernet, so this is close to
        free. It skips any device whose lock is held, which keeps it out of
        the way of captures entirely.
        """
        if interval_s is not None:
            self.sync_interval_s = float(interval_s)
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return {"running": True, "interval_s": self.sync_interval_s}

        self._sync_stop.clear()

        def loop():
            # Check before the first wait, not after it. Sleeping first leaves
            # every device reporting "not yet checked" for a full interval
            # after startup — the moment an operator is most likely to be
            # looking, having just restarted the service.
            while True:
                for device_id in list(self.devices):
                    if self._sync_stop.is_set():
                        break
                    try:
                        self.check_device_sync(device_id, blocking=False)
                    except Exception:  # noqa: BLE001 - a watchdog never dies
                        continue
                if self._sync_stop.wait(self.sync_interval_s):
                    break

        self._sync_thread = threading.Thread(
            target=loop, name="sync-watchdog", daemon=True)
        self._sync_thread.start()
        self.safety.audit("sync_watchdog_started",
                          interval_s=self.sync_interval_s)
        return {"running": True, "interval_s": self.sync_interval_s}

    def stop_sync_watchdog(self) -> dict:
        self._sync_stop.set()
        thread, self._sync_thread = self._sync_thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        return {"running": False, "interval_s": self.sync_interval_s}

    def sync_watchdog_status(self) -> dict:
        alive = self._sync_thread is not None and self._sync_thread.is_alive()
        return {
            "running": alive,
            "interval_s": self.sync_interval_s,
            "devices": {k: {"in_sync": v.get("in_sync"),
                            "checked_at": v.get("checked_at"),
                            "drift": v.get("drift", [])}
                        for k, v in self._sync_records.items()},
        }

    def configure(self, device_id: str, cfg: dict) -> dict:
        dev = self.device(device_id)
        merged = {**dev.config.to_dict(), **cfg}
        dev.configure(DeviceConfig(**merged))
        # A live transmitter must not inherit permission granted for a
        # different configuration. Previously this path bypassed the interlock
        # entirely: tx gain could go from -30 dB to 0 dB, or the radio could be
        # walked outside the active frequency profile, without revalidation.
        self.enforce_tx_authorization(f"device {device_id} reconfigured")
        out = self._describe_device(dev)
        try:
            self._save_device_config(dev)
        except Exception as exc:  # noqa: BLE001 - the change still applies
            # Not necessarily "defaults": if an earlier save succeeded, a
            # restart returns the radio to *that*, which is a different and
            # more confusing surprise than reverting to a known baseline.
            previous = self._saved_device_configs.get(device_id)
            reverts_to = ("the last configuration that saved successfully"
                          if previous else "its built-in defaults")
            out["config_not_saved"] = (
                f"Applied now, but could not be saved ({exc}), so a restart "
                f"will return this radio to {reverts_to}.")
        return out

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

    # -- persisted safety policy (FR-SAF-007) ------------------------------
    #
    # The frequency profile is a *policy choice* — which bands this bench is
    # permitted to occupy — and it survives a restart, because a profile
    # narrowed for antenna work quietly widening back to 70 MHz-6 GHz is a
    # safety gate reverting with nobody told. It used to do exactly that:
    # SafetyLimits() was constructed with its defaults on every start.
    #
    # Path attenuation is deliberately **not** persisted. It asserts "there is
    # N dB in the cable path right now", which is a claim about physical
    # cabling that only a person at the bench can make truthfully. Restoring
    # it would re-assert yesterday's wiring on today's bench, and a restart is
    # a good moment to force that claim to be made again. Policy persists;
    # physical assertions do not.

    # -- persisted device configuration ------------------------------------
    #
    # `DeviceConfig()` was constructed fresh for every registration, so centre
    # frequency, sample rate and both gains returned to their defaults on
    # every start — and `connect()` pushes them at the radio, so a restart
    # silently retuned the bench. An operator who sets a gain, restarts the
    # service and finds it back at 40 dB is not imagining it.
    #
    # Restoring what the operator last chose is the least surprising
    # behaviour, and safe: transmit still requires arming, which deliberately
    # does not persist, and any restored gain is re-checked by the TX
    # fingerprint before it can key anything.

    def _load_device_configs(self) -> dict:
        self._device_config_load_error = None
        if not os.path.exists(self._device_config_path):
            return {}
        try:
            with open(self._device_config_path, encoding="utf-8") as f:
                saved = json.load(f)
            if not isinstance(saved, dict):
                raise ValueError("saved device configuration is not an object")
            return saved
        except Exception as exc:  # noqa: BLE001 - a bad file must not stop boot
            # Recorded, not just audited. Every device would otherwise report
            # `{"source": "default"}` — byte-identical to a bench that never
            # had a saved configuration — so a lost file and a fresh start
            # looked the same. `_restore_frequency_profile` gets this right
            # and is the precedent this follows.
            self._device_config_load_error = str(exc)
            self.safety.audit("device_config_restore_failed", error=str(exc))
            return {}

    def _restore_device_config(self, dev, carry_config=None) -> None:
        """Apply the saved configuration to a freshly registered device.

        Clamped to whatever this device actually is: the saved values may
        have come from a different radio at the same address, or from before
        a firmware change moved the limits. `clamp_config` reports what it
        had to move, and those notes are kept rather than discarded.
        """
        origin = "carried"
        saved = None
        if carry_config is not None:
            saved = carry_config.to_dict()
        else:
            origin = "restored"
            saved = self._saved_device_configs.get(dev.device_id)
        if not saved:
            return
        try:
            fields = {k: v for k, v in saved.items()
                      if k in DeviceConfig().to_dict()}
            cfg, notes = dev.clamp_config(DeviceConfig(**fields))
            # `clamp_config` fits the continuous knobs; it does not look at
            # channel indices or buffer size. Without this, a disconnected
            # device accepted and reported a saved `rx_channel` no adapter
            # would honour — the connected path caught it only because
            # `configure()` happens to validate.
            problems = dev.validate_config(cfg)
            if problems:
                raise ConfigurationError("; ".join(problems))
        except Exception as exc:  # noqa: BLE001
            self.safety.audit("device_config_restore_failed",
                              device=dev.device_id, error=str(exc))
            self.device_config_restore[dev.device_id] = {
                "source": "default",
                "note": f"{origin.capitalize()} configuration could not be "
                        f"applied ({exc}); "
                        "the device is on its built-in defaults."}
            return

        # `dev.config = cfg` is not enough. A real radio arrives here *already
        # connected* — `PlutoDevice.discover()` calls `connect()` before the
        # runtime ever sees the device, and `connect()` has already pushed
        # `DeviceConfig()` defaults at the hardware through `_apply()`. Only
        # assigning the cache would leave the radio on 915 MHz / 40 dB while
        # `/api/status` reported the restored values as though it held them:
        # not merely an ineffective restore, but the platform asserting a
        # configuration the radio never had (rule 1), and the sync watchdog
        # would then report it as externally-caused drift.
        applied_to_hardware = False
        if getattr(dev, "connected", False):
            # `PlutoDevice.configure` assigns `self.config` and *then* writes
            # seven libiio attributes, any of which can fail — a board that
            # re-enumerated after a reflash is documented. Without this
            # snapshot a partial write left `config` holding what we asked for
            # while the radio held something else, which is the same false
            # claim this whole path exists to remove, surviving in the error
            # branch.
            before = dev.config
            try:
                dev.configure(cfg)          # validates, and applies on real hardware
                applied_to_hardware = True
            except Exception as exc:  # noqa: BLE001
                # `_apply()` writes seven libiio attributes in sequence, so a
                # failure part-way leaves the radio holding a *mixture* that
                # neither `cfg` nor `before` describes. Rolling back to
                # `before` only swaps one false claim for another. Ask the
                # radio instead — this is what `resync_device` already does,
                # and a read-back measures ~4 ms.
                recovered, adopt_note = False, ""
                dev.config = before
                try:
                    # `adopt_hardware_state`, not a hand-rolled copy of the
                    # DeviceConfig fields. `read_hardware_config` also carries
                    # `tx_lo_hz` and `gain_control_mode`, and those are what
                    # say whether the adopted numbers mean anything: `_apply`
                    # writes the AGC mode fifth of seven, immediately before
                    # the gains, so failing there leaves AGC live and the
                    # "gain" a moving value rather than a setting. Adopting it
                    # silently would report a transient as a configuration.
                    status = dev.adopt_hardware_state()
                    recovered = True
                except Exception as read_exc:  # noqa: BLE001
                    # Bind it. The outer handler reports its exception and
                    # this one discarded its own, losing the difference
                    # between "another process holds the USB handle" and "the
                    # board re-enumerated" — the two documented causes.
                    status, read_error = None, str(read_exc)
                if recovered:
                    # Record what the re-read found, including anything
                    # adopting could not resolve. `None` here would mean "not
                    # checked", which is untrue the instant after checking.
                    self._sync_records[dev.device_id] = {
                        **status, "device_id": dev.device_id}
                    adopt_note = (" The write failed part-way; the values "
                                  "shown were read back from the radio "
                                  "afterwards rather than assumed.")
                    if status.get("drift"):
                        adopt_note += (
                            " The radio still disagrees on "
                            + ", ".join(d["field"] for d in status["drift"])
                            + ".")
                    if status.get("rx_gain_unstable"):
                        adopt_note += (" Automatic gain control was left "
                                       "running, so the receive gain shown is "
                                       "a sample of a moving value, not a "
                                       "setting.")
                else:
                    # Neither the radio nor our cache can be trusted, so do
                    # not assert either. `in_sync: None` is "not checked",
                    # which is a different claim from agreement.
                    self._sync_records[dev.device_id] = {
                        "device_id": dev.device_id, "checked_at": time.time(),
                        "readable": False, "in_sync": None, "drift": [],
                        "error": "a configuration write failed part-way and "
                                 f"the device could not be read back: "
                                 f"{read_error}",
                    }
                    adopt_note = (" The write failed part-way, so the radio "
                                  "may hold a mixture of the old and new "
                                  "settings, and reading it back also failed "
                                  f"({read_error}). Its configuration is "
                                  "unverified.")
                self.safety.audit("device_config_restore_failed",
                                  device=dev.device_id, error=str(exc),
                                  stage="apply", read_back=recovered)
                self.device_config_restore[dev.device_id] = {
                    "source": "default",
                    "applied_to_hardware": False,
                    "note": (f"Saved configuration was refused by the device "
                             f"({exc})." + adopt_note +
                             " Check it before capturing.")}
                return
        else:
            # Not connected yet, so `connect()` will apply this for us.
            dev.config = cfg

        detail = []
        if notes:
            detail.append("Adjusted to this device's limits: " + "; ".join(notes))
        self.device_config_restore[dev.device_id] = {
            "source": origin,
            "applied_to_hardware": applied_to_hardware,
            "note": " ".join(detail) or None,
        }
        self.safety.audit("device_config_restored", device=dev.device_id,
                          config=cfg.to_dict(), clamped=notes or None,
                          applied_to_hardware=applied_to_hardware,
                          origin=origin)
        if origin == "carried":
            # The same board under a new device id: persist under every alias
            # so the next restart restores what is actually on the radio.
            try:
                self._save_device_config(dev)
            except Exception as exc:  # noqa: BLE001
                # Auditing alone is not enough — swallowing this reintroduced
                # exactly the defect fixed for `configure()`: the settings
                # carry correctly, the save fails, and the next restart
                # quietly reverts while still reporting `restored`.
                self.device_config_restore[dev.device_id]["note"] = (
                    (self.device_config_restore[dev.device_id]["note"] or "")
                    + f" Carried onto this transport, but could not be saved "
                      f"({exc}), so a restart will not keep it.").strip()

    def _config_keys_for(self, dev) -> list:
        """Every device id that names this same board.

        Device ids are per-URI (`pluto-usb:`, `pluto-ip:192.168.99.222`) while
        those are one radio — CLAUDE.md is explicit that registering it twice
        gives entries whose cached configs silently diverge. Saving under one
        key alone reproduced that on disk: switch transport, retune, restart,
        and discovery picks whichever transport measures fastest, restores the
        *other* key's stale entry, and reports `source: "restored"`. Observed
        putting a radio on 2437 MHz / RX 40 dB when the operator's last choice
        was 2450 MHz / RX 18 dB. Writing every alias keeps them from diverging.
        """
        keys = [dev.device_id]
        info = getattr(dev, "discovery", {}) or {}
        # Only when the grouping rests on a serial number. `group_boards`
        # returns `identified_by` precisely because "these transports are one
        # radio" is sometimes an *inference*: with no serial it falls back to
        # matching attributes, and its own note says two identical Plutos on
        # identical firmware are indistinguishable that way — as are two
        # boards whose identity probe failed, since both then fingerprint on
        # an empty identity. Writing aliases on that inference would save one
        # radio's configuration under another's id and then apply it to the
        # wrong hardware, which is worse than the staleness it fixes. Consume
        # the inference only at the confidence it was offered with.
        if info.get("identified_by") == "serial":
            for alt in (info.get("alternatives") or []):
                uri = alt.get("uri") if isinstance(alt, dict) else None
                if uri:
                    keys.append(f"pluto-{uri}")
        return list(dict.fromkeys(keys))

    def _per_transport_note(self, dev) -> str:
        """Say when settings are saved per transport rather than per board.

        Gating the aliases on a serial number is right, but silently losing
        the cross-transport restore is not: `discovery.py` calls an empty
        `hw_serial` "the common case", so on a typical bench the gate is
        *closed* and switching transport then restarting quietly presents a
        stale entry as the operator's choice. `config_source` exists to keep
        "the radio is at 915 MHz" apart from "nobody chose 915 MHz"; a
        limitation nobody is told about belongs on the same side of that line.
        """
        info = getattr(dev, "discovery", {}) or {}
        alts = info.get("alternatives") or []
        if info.get("identified_by") == "serial" or len(alts) < 2:
            return ""
        # Precise about *when*, because the obvious phrasing is wrong twice
        # over. Settings do carry across a switch on their own — the carry
        # path saves under the new id — so "switching and restarting will not
        # carry them" is false for the ordinary switch-and-restart. And the
        # trigger is not always an operator action: discovery picks a
        # transport on every boot, so a LAN that is down at start time moves
        # the radio without anyone touching the button. Naming only the button
        # would let an operator who never touches it read this as not applying
        # to them, which is the more dangerous of the two errors.
        return ("This radio reports no serial number, so its settings are "
                "saved per transport rather than per board. Anything changed "
                "after switching transport is saved only under that "
                "transport; if the platform later comes up on a different one — "
                "because you switched back, or because discovery picked "
                "differently — it restores that transport's own last-saved "
                "settings, or its defaults if it has none. Pin an explicit "
                "URI to avoid this.")

    def _save_device_config(self, dev) -> None:
        """Record the operator's configuration so a restart does not undo it."""
        # Build the new state without committing it to memory first. Mutating
        # `self._saved_device_configs` up front left memory and disk diverged
        # after a failed write, so a later successful save on any *other*
        # device would silently persist the entry the operator had just been
        # told was not saved.
        candidate = dict(self._saved_device_configs)
        cfg = dev.config.to_dict()
        for key in self._config_keys_for(dev):
            candidate[key] = cfg
        tmp = self._device_config_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._device_config_path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(candidate, f, indent=1)
            os.replace(tmp, self._device_config_path)   # atomic
        except Exception as exc:  # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self.safety.audit("device_config_save_failed",
                              device=dev.device_id, error=str(exc))
            raise
        self._saved_device_configs = candidate

    def _restore_frequency_profile(self) -> dict:
        """Load the saved profile at startup, reporting what happened.

        Never raises: a bench that cannot read its saved policy must still
        start, but it must not pretend it restored one. The result is carried
        into `/api/status` so "this is the default because the file was
        unreadable" is visible rather than indistinguishable from a
        deliberate choice.
        """
        default = self.safety.limits.active_profile
        out = {"source": "default", "profile": default, "note": None}
        if not os.path.exists(self._safety_state_path):
            return out
        try:
            with open(self._safety_state_path, encoding="utf-8") as f:
                saved = json.load(f)
            name = saved.get("active_profile")
        except Exception as exc:  # noqa: BLE001
            out["note"] = (
                f"Saved safety policy could not be read ({exc}), so the "
                f"frequency profile is the built-in default '{default}'. "
                "Check it before transmitting.")
            self.safety.audit("frequency_profile_restore_failed",
                              error=str(exc), profile=default)
            return out
        if name == default:
            out["source"] = "restored"
            return out
        if name not in self.safety.limits.frequency_profiles:
            out["note"] = (
                f"Saved frequency profile '{name}' no longer exists, so the "
                f"built-in default '{default}' is active. Check it before "
                "transmitting.")
            self.safety.audit("frequency_profile_restore_failed",
                              missing_profile=name, profile=default)
            return out
        self.safety.limits.active_profile = name
        out.update(source="restored", profile=name)
        self.safety.audit("frequency_profile_restored", profile=name)
        return out

    def _save_frequency_profile(self) -> None:
        """Persist the policy. A failure here must be loud, not silent."""
        tmp = self._safety_state_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._safety_state_path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"active_profile": self.safety.limits.active_profile,
                           "saved_at": time.time()}, f, indent=1)
            os.replace(tmp, self._safety_state_path)   # atomic
        except Exception as exc:  # noqa: BLE001
            # Leave no half-written file behind: the next start reads this
            # directory, and a stray .tmp is clutter at best and confusing at
            # worst when someone is working out why a profile did not stick.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self.safety.audit("frequency_profile_save_failed", error=str(exc))
            raise

    def set_frequency_profile(self, name: str) -> dict:
        """Change the active profile, withdrawing any TX it no longer covers."""
        if name not in self.safety.limits.frequency_profiles:
            raise KeyError(f"unknown frequency profile: {name}")
        self.safety.limits.active_profile = name
        self.safety.audit("frequency_profile_changed", profile=name)
        saved, save_error = True, None
        try:
            self._save_frequency_profile()
        except Exception as exc:  # noqa: BLE001 - the change still applies
            saved, save_error = False, str(exc)
        self.profile_restore = {"source": "set", "profile": name, "note": None}
        revoked = self.enforce_tx_authorization(f"frequency profile -> {name}")
        status = self.safety.status()
        if revoked:
            status["tx_revoked"] = revoked
        if not saved:
            # Applied in memory but it will not survive a restart, which is
            # the whole failure this persistence exists to end.
            status["profile_not_saved"] = (
                f"'{name}' is active now but could not be saved ({save_error}), "
                "so a restart will return to the built-in default.")
        return status

    # -- vector network analyser (FR-RFC-003/004) --------------------------
    #
    # The VNA is an instrument, not a transmitter under rule 5. Its source is
    # fixed-level (~ -9 dBm), is not operator-controllable, and free-runs
    # whenever the instrument is powered, so the TX fingerprint — frequency,
    # occupied span, waveform, gains, rate, profile, declared path — has
    # nothing to bind to and arming the bench to measure a cable would be
    # theatre. What it does do is sweep across bands while attached to an
    # antenna, so every sweep is recorded to the safety audit log and its span
    # is checked against the active frequency profile. The check warns and is
    # recorded; it does not block, because a broadband sweep into a 50 ohm
    # load radiates essentially nothing and blocking it would make the
    # instrument useless for the job it is here to do. Making that judgement
    # visible is the point — see `band_check` in every sweep result.

    def _vna_band_check(self, start_hz: float, stop_hz: float) -> dict:
        """Compare a swept span against the active profile (FR-SAF-007)."""
        bands = self.safety.limits.allowed_bands()
        profile = self.safety.limits.active_profile
        inside = (not bands) or any(lo <= start_hz and stop_hz <= hi
                                    for lo, hi in bands)
        out = {
            "profile": profile,
            "allowed_bands": [[float(lo), float(hi)] for lo, hi in bands],
            "swept_hz": [float(start_hz), float(stop_hz)],
            "inside_profile": bool(inside),
            "blocking": False,
            "warning": None,
        }
        if not inside:
            out["warning"] = (
                f"This sweep covers {start_hz / 1e6:.3f}-{stop_hz / 1e6:.1f} MHz, "
                f"which is not inside the active profile '{profile}'. Into a "
                "load or a cable that is a closed circuit and radiates "
                "essentially nothing; into an antenna it is emission outside "
                "the profile. The sweep is recorded either way — check what is "
                "on the port.")
        return out

    def vna_discover(self) -> list[dict]:
        """Serial ports that answer as a VNA (probed, not assumed)."""
        from ..rfcomponents import nanovna
        return nanovna.discover()

    def vna_status(self, port: str = "/dev/nanovna") -> dict:
        """Identity, battery, sweep settings and calibration state."""
        from ..rfcomponents import nanovna
        with nanovna.NanoVNA(port) as vna:
            return {
                "port": port,
                **vna.identify(),
                "battery_mv": vna.battery_mv(),
                "sweep": vna.sweep_settings(),
                "calibration": vna.cal_status(),
                "max_points": nanovna.MAX_SWEEP_POINTS,
            }

    def vna_sweep(self, start_hz: float, stop_hz: float, points: int = 101,
                  ports: int = 2, port: str = "/dev/nanovna",
                  comp_id: str = "", ctx=None) -> dict:
        """Sweep the instrument, optionally attaching the result to a component.

        `ports` is the operator's declaration of what is actually connected.
        It matters: the instrument always returns an S21 column, so a
        one-port antenna measurement with nothing on port 2 still produces a
        column of noise. Storing that as "insertion loss" would be inventing a
        measurement nobody made, so a reflection-only sweep discards it here
        rather than carrying it forward (rule 1).
        """
        from ..rfcomponents import nanovna
        if ports not in (1, 2):
            raise ValueError("ports must be 1 (reflection) or 2 (thru)")

        band = self._vna_band_check(start_hz, stop_hz)
        if ctx is not None:
            ctx.progress(0.1, "sweeping")
        with nanovna.NanoVNA(port) as vna:
            ident = vna.identify()
            cal = vna.cal_status()
            data = vna.scan(start_hz, stop_hz, points)
        if ctx is not None:
            ctx.progress(0.8, "analysing")

        self.safety.audit(
            "vna_sweep", port=port, instrument=ident.get("model", ""),
            serial_number=ident.get("serial_number", ""),
            start_hz=float(start_hz), stop_hz=float(stop_hz), points=int(points),
            declared_ports=ports, component=comp_id or None,
            profile=band["profile"], inside_profile=band["inside_profile"])

        if ports == 1:
            data.pop("s21", None)
            data["ports"] = 1

        calibration = {
            "known": False,
            "standards": cal["standards"],
            "applied": cal["applied"],
            "instrument_reported": cal["raw"],
            "note": ("The instrument reports which standards are captured but "
                     "not the span they were captured over, and it interpolates "
                     "a calibration onto whatever span is swept. Run a "
                     "verification sweep against a known standard to establish "
                     "whether this calibration covers this span."),
        }

        result = {
            "instrument": ident,
            "port": port,
            "band_check": band,
            "calibration": calibration,
            "points": len(data["freqs_hz"]),
            "start_hz": data["freqs_hz"][0],
            "stop_hz": data["freqs_hz"][-1],
            "declared_ports": ports,
        }

        if comp_id:
            comp = self.components.attach_measurement(
                comp_id, data,
                source={"kind": "instrument",
                        "instrument": ident.get("model", ""),
                        "serial_number": ident.get("serial_number", ""),
                        "port": port,
                        "declared_ports": ports},
                calibration=calibration)
            result["component"] = comp
        else:
            # Not stored — hand back the derived curves so the operator can
            # look before committing a measurement to a component.
            from ..rfcomponents.touchstone import analyze_s11, analyze_s21
            result["s11"] = analyze_s11(data["freqs_hz"], data["s11"])
            if "s21" in data:
                result["s21"] = analyze_s21(data["freqs_hz"], data["s21"])
            result["freqs_hz"] = data["freqs_hz"]
        if ctx is not None:
            ctx.progress(1.0, "done")
        return result

    def vna_sweep_job(self, **kwargs):
        """Run `vna_sweep` as a cancellable background job (FR-API-003)."""
        span = f"{kwargs.get('start_hz', 0) / 1e6:.3f}-{kwargs.get('stop_hz', 0) / 1e6:.1f} MHz"
        return self.jobs.submit(
            "vna_sweep", f"VNA sweep {span}",
            lambda ctx: self.vna_sweep(ctx=ctx, **kwargs),
            params=dict(kwargs))

    def vna_measure_delay(self, start_hz: float, stop_hz: float,
                          port: str = "/dev/nanovna", points_a: int = 101,
                          points_b: int = 301, comp_id: str = "",
                          reference_plane_ns: float = 0.0) -> dict:
        """Measure electrical delay, cross-checked against phase aliasing.

        Sweeps the same span twice at different point counts. Aliasing folds a
        long delay down by a whole number of cycles per frequency step, and
        the fold depends on the step — so two steps agree only if neither
        wrapped. A single sweep cannot establish this about itself: a 40 ns
        cable sampled every 23 MHz reports 3.5 ns with a perfectly clean
        linear fit, indistinguishable from a genuinely short one.

        `nominal_delay_ns` is written only when the two sweeps agree. A
        disagreement means at least one aliased and the true delay is longer
        than both, which is a reason to report and re-sweep, not to store the
        smaller number.
        """
        from ..rfcomponents import nanovna
        from ..rfcomponents.touchstone import analyze_delay, delays_agree

        if points_a == points_b:
            raise ValueError(
                "the two sweeps must use different point counts, or they "
                "share a frequency step and would alias identically")

        band = self._vna_band_check(start_hz, stop_hz)
        with nanovna.NanoVNA(port) as vna:
            ident = vna.identify()
            a = vna.scan(start_hz, stop_hz, points_a)
            b = vna.scan(start_hz, stop_hz, points_b)

        da = analyze_delay(a["freqs_hz"], a["s21"])
        db = analyze_delay(b["freqs_hz"], b["s21"])
        check = delays_agree(da, db)

        self.safety.audit(
            "vna_delay_measurement", port=port,
            instrument=ident.get("model", ""),
            start_hz=float(start_hz), stop_hz=float(stop_hz),
            points=[points_a, points_b], component=comp_id or None,
            agree=check["agree"], profile=band["profile"],
            inside_profile=band["inside_profile"])

        out = {"instrument": ident, "band_check": band, "sweeps": [da, db],
               "cross_check": check, "reference_plane_ns": reference_plane_ns,
               "adopted": False}

        if check["agree"]:
            total = round(check["delay_ns"] + reference_plane_ns, 3)
            out["delay_ns"] = check["delay_ns"]
            out["total_delay_ns"] = total
            if comp_id:
                note = (f"electrical delay {total} ns from S21 phase slope over "
                        f"{start_hz / 1e6:.1f}-{stop_hz / 1e6:.1f} MHz, "
                        f"cross-checked at {points_a} and {points_b} points "
                        f"(agree to {check['difference_ns']} ns, so the phase "
                        f"did not alias)")
                if reference_plane_ns:
                    note += (f"; includes {reference_plane_ns} ns declared for "
                             "the calibration reference plane, which is an "
                             "operator assumption rather than a measurement")
                out["component"] = self.components.set_delay(comp_id, total, note)
                out["adopted"] = True
        return out

    def vna_verify_calibration(self, start_hz: float, stop_hz: float,
                               points: int = 101, port: str = "/dev/nanovna") -> dict:
        """Measure a known thru and judge whether the calibration covers this span.

        The firmware will not say what span a calibration was taken over, so
        this measures the residual instead: a calibration that covers the span
        drives S21 to 0 dB across all of it, and one that was interpolated
        outward leaves its error concentrated at the band edges.

        The operator must have a known thru connected. This cannot verify
        that, which is why the result records what was asked of them rather
        than asserting the standard was in place.
        """
        from ..rfcomponents import nanovna
        band = self._vna_band_check(start_hz, stop_hz)
        with nanovna.NanoVNA(port) as vna:
            ident = vna.identify()
            cal = vna.cal_status()
            data = vna.scan(start_hz, stop_hz, points)
        residual = nanovna.analyze_thru_residual(data["freqs_hz"], data["s21"])
        self.safety.audit(
            "vna_calibration_check", port=port,
            start_hz=float(start_hz), stop_hz=float(stop_hz), points=int(points),
            covers_span=residual["covers_span"],
            max_deviation_db=residual["max_deviation_db"],
            profile=band["profile"], inside_profile=band["inside_profile"])
        return {
            "instrument": ident,
            "band_check": band,
            "instrument_reported": cal["raw"],
            "standards": cal["standards"],
            "assumed_connected": "thru",
            "residual": residual,
            "checked_at": time.time(),
        }

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
    def bearing_sweep(self, device_id: str, center_hz: float | None = None,
                      duration_s: float = 60.0, bin_deg: float = 5.0,
                      sample_rate_hz: float = 2.5e6, rx_gain_db: float = 40.0,
                      samples: int = 65536, name: str = "bearing sweep",
                      operator: str = "", ctx=None) -> dict:
        """Record received power against where the antenna was pointed.

        Receive-only: it transmits nothing, so it needs no arming and is safe
        before any TX bring-up. The operator sweeps the antenna by hand while
        this captures continuously, and each capture is stamped with the
        heading reported by the position source.

        This is the air-looking counterpart to a band survey — power against
        *bearing* rather than against frequency — and it is what the platform
        can show today with the antennas on the bench. It is not a PPI: there
        is no range axis, because range needs the radio to transmit and time
        its own echo, which this bench has never done.

        It refuses to run without a heading. A sweep with no bearings is a
        list of power readings with nothing to plot them against, and filling
        the axis with assumed angles would be inventing the measurement.
        """
        dev = self.device(device_id)
        if not dev.connected:
            raise ValueError(f"{device_id} is not connected")
        if self.stop_acquisition.is_set():
            raise ValueError("acquisition is stopped after an emergency stop; "
                             "resume before starting a sweep")

        probe = self.position_source.read()
        if probe is None or probe.heading_deg is None:
            raise ValueError(
                "this sweep plots power against bearing, and the active "
                f"position source ({self.position_source.name}) is not "
                "reporting a heading. Connect an orientation sensor that "
                "sends heading_deg, or use a band survey if you want power "
                "against frequency instead.")

        original = DeviceConfig(**dev.config.to_dict())
        centre = float(center_hz or original.center_frequency_hz)
        manifest = self.store.create(
            name=name, kind="bearing_sweep", operator=operator,
            objective=f"receive-only bearing sweep at {centre / 1e6:.3f} MHz",
            hardware={"device_id": device_id, "kind": dev.kind,
                      "rf_chain": self.current_chain()},
            rf_config={"center_frequency_hz": centre,
                       "sample_rate_hz": sample_rate_hz,
                       "rx_gain_db": rx_gain_db, "bin_deg": bin_deg})
        exp_id = manifest["identity"]["experiment_id"]

        points, no_heading = [], 0
        deadline = time.time() + max(1.0, float(duration_s))
        try:
            with self.device_locks[device_id]:
                dev.configure(DeviceConfig(**{
                    **original.to_dict(),
                    "center_frequency_hz": centre,
                    "sample_rate_hz": sample_rate_hz,
                    "rx_bandwidth_hz": min(sample_rate_hz,
                                           dev.capabilities.max_bandwidth),
                    "rx_gain_db": rx_gain_db}))
                while time.time() < deadline:
                    if ctx is not None:
                        ctx.check()
                        left = max(0.0, deadline - time.time())
                        ctx.progress(1.0 - left / max(1.0, float(duration_s)),
                                     f"{len(points)} captures, {left:.0f}s left")
                    seg = dev.receive(samples)
                    pos = self.position_source.read()
                    point = _survey_point(centre, seg)
                    if pos is None or pos.heading_deg is None:
                        # Recorded, but it cannot be placed on the display.
                        no_heading += 1
                        point["heading_deg"] = None
                    else:
                        point["heading_deg"] = round(pos.heading_deg % 360.0, 2)
                        point["heading_stale_s"] = round(pos.stale_s, 3)
                    points.append(point)
        finally:
            dev.configure(original)     # always restore the operator's config

        binned = bin_by_bearing(points, bin_deg=bin_deg)
        binned["center_hz"] = centre
        binned["captures"] = len(points)
        binned["captures_without_heading"] = no_heading
        if no_heading:
            binned["note"] = (
                f"{no_heading} of {len(points)} captures arrived with no "
                "heading and are stored but not placed on the plot.")
        self.store.add_derived(
            exp_id, "bearing_sweep", binned,
            {"stages": [{"stage": "bearing_sweep", "version": "1.0",
                         "params": {"bin_deg": bin_deg, "center_hz": centre,
                                    "duration_s": duration_s,
                                    "samples": samples,
                                    "rx_gain_db": rx_gain_db}}],
             "fingerprint": "bearing_sweep-1.0"}, [])
        self.store.finalize(exp_id)
        self.safety.audit("bearing_sweep", device=device_id, experiment=exp_id,
                          center_hz=centre, captures=len(points),
                          coverage=binned["coverage"])
        return {"experiment_id": exp_id, **binned}

    def bearing_sweep_job(self, **kwargs):
        """Run a bearing sweep as a cancellable background job (FR-API-003)."""
        return self.jobs.submit(
            "bearing_sweep", kwargs.get("name", "bearing sweep"),
            lambda ctx: self.bearing_sweep(ctx=ctx, **kwargs),
            params=dict(kwargs))

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
        # The last reconciliation against the hardware. Read from the record
        # rather than the radio so building a status page cannot block on a
        # device, but never omitted: a UI showing `config` with no indication
        # of whether it still matches is the problem this exists to fix. A
        # `null` here means "not checked yet", which is a different claim from
        # "in sync" and must not be rendered as one.
        d["sync"] = self._sync_records.get(dev.device_id)
        # Whether this device came up on saved settings or on built-in
        # defaults, so "the radio is at 915 MHz" and "nobody chose 915 MHz"
        # are distinguishable.
        src = dict(self.device_config_restore.get(
            dev.device_id,
            {"source": "default",
             "note": (f"Saved device configuration could not be read "
                      f"({self._device_config_load_error}), so this radio is "
                      "on its built-in defaults. Check it before capturing."
                      if self._device_config_load_error else None)}))
        per_transport = self._per_transport_note(dev)
        if per_transport:
            src["note"] = " ".join(x for x in (src.get("note"),
                                               per_transport) if x)
        # Every shape carries every key. One `/api/status` used to return four
        # keys for a discovered radio and two for the simulator, so a consumer
        # indexing `applied_to_hardware` worked on one device and raised on
        # the other. Absent-versus-False is also the ambiguity this codebase
        # refuses elsewhere — `in_sync: None` means "not checked", not "fine".
        src.setdefault("applied_to_hardware", False)
        # Tri-state. `False` is a true statement for a device with one known
        # way in, but asserting it for a radio registered by explicit URI —
        # which carries no `discovery` at all, so its other transports were
        # never looked for — claims something nothing established, while
        # per-URI saving is exactly what is in force. "One transport" and "we
        # did not look" are different facts, and this codebase already keeps
        # them apart: `in_sync: None` is "not checked", not "fine".
        info = getattr(dev, "discovery", {}) or {}
        src["saved_per_transport"] = (
            bool(per_transport) if "alternatives" in info else None)
        d["config_source"] = src
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
            # Where the active frequency profile came from. A profile that is
            # the built-in default because a saved one could not be read is a
            # different claim from one the operator chose, and the UI has to
            # be able to tell them apart.
            "safety": {**self.safety.status(),
                       "profile_source": self.profile_restore},
            "acquisition_stopped": self.stop_acquisition.is_set(),
            "storage": self.store.storage_stats(),
            "recent_experiments": self.store.list()[:8],
            "active_scans": {k: v["builder"].status() for k, v in self.scans.items()},
            "media_presets": {k: m.to_dict() for k, m in MEDIA_PRESETS.items()},
            "waveforms": {k: w.preview() for k, w in CATALOG.items()},
        }
