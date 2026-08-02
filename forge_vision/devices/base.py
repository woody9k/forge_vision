"""Device adapter contract (FR-DEV-*, FR-API-004).

Adapters isolate hardware access from everything above them. They must:
  * start with transmit disabled (FR-DEV-004),
  * expose a capability model used for configuration validation (FR-DEV-002/003),
  * report loss/discontinuity rather than concealing it (FR-ACQ-003),
  * support force_tx_off() that never depends on higher layers (FR-SAF-008).
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field, asdict

import numpy as np


@dataclass(frozen=True)
class DeviceCapabilities:
    min_frequency: float
    max_frequency: float
    min_sample_rate: float
    max_sample_rate: float
    max_bandwidth: float                    # receive analog bandwidth
    rx_channels: int = 1
    tx_channels: int = 1
    max_rx_gain_db: float = 70.0
    min_tx_gain_db: float = -89.0
    max_tx_gain_db: float = 0.0
    transports: tuple = ("usb",)
    # transmit bandwidth is often narrower than receive (an AD9363 Pluto
    # reports 56 MHz RX but only 40 MHz TX); None means "same as RX"
    max_tx_bandwidth: float | None = None

    @property
    def tx_bandwidth(self) -> float:
        return (self.max_tx_bandwidth if self.max_tx_bandwidth is not None
                else self.max_bandwidth)

    def to_dict(self) -> dict:
        return {**asdict(self), "tx_bandwidth": self.tx_bandwidth}


@dataclass
class DeviceConfig:
    center_frequency_hz: float = 915e6
    sample_rate_hz: float = 61.44e6
    rx_bandwidth_hz: float = 56e6
    rx_gain_db: float = 40.0
    tx_gain_db: float = -30.0
    rx_channel: int = 0
    tx_channel: int = 0
    buffer_size: int = 65536

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigurationError(Exception):
    pass


# How far a read-back value may sit from the requested one before it counts as
# drift rather than quantization: (absolute, relative). The absolute figures
# follow the ones the bench characterization suite settled on for this board
# (tools/chancal/common.py) — the LO is fractional-N, gain moves in 0.25 dB
# steps, the sample rate lands on an integer. RF bandwidth is relative because
# it snaps to whichever analog filter is nearest, which is coarse and scales
# with the setting; 1% is empirical and deliberately loose, since the actual
# value is always reported and only the in_sync boolean depends on this.
SYNC_TOLERANCES = {
    "center_frequency_hz": (100.0, 0.0),
    "sample_rate_hz": (1.0, 0.0),
    "rx_bandwidth_hz": (0.0, 0.01),
    "rx_gain_db": (0.5, 0.0),
    "tx_gain_db": (0.5, 0.0),
}


@dataclass
class CaptureSegment:
    """One timed acquisition unit with complete metadata (FR-ACQ-001/002)."""

    iq: np.ndarray
    timestamp: float
    config: dict
    waveform: dict | None
    device_id: str
    sample_rate_hz: float
    center_frequency_hz: float
    loss_events: list = field(default_factory=list)   # FR-ACQ-003
    clipped: bool = False
    position: dict | None = None                      # FR-POS-007
    telemetry: dict = field(default_factory=dict)     # FR-DEV-007
    tx_active: bool = False

    def metadata(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "num_samples": int(len(self.iq)),
            "config": self.config,
            "waveform": self.waveform,
            "device_id": self.device_id,
            "sample_rate_hz": self.sample_rate_hz,
            "center_frequency_hz": self.center_frequency_hz,
            "loss_events": self.loss_events,
            "clipped": bool(self.clipped),
            "position": self.position,
            "telemetry": self.telemetry,
            "tx_active": self.tx_active,
        }


class DeviceAdapter(abc.ABC):
    """Contract every radio (real, simulated, or replay) implements."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.connected = False
        self.tx_enabled = False        # always starts disabled (FR-DEV-004)
        self.config = DeviceConfig()
        self._tx_waveform = None

    # -- identity / capability --------------------------------------------
    @property
    @abc.abstractmethod
    def capabilities(self) -> DeviceCapabilities: ...

    @property
    @abc.abstractmethod
    def kind(self) -> str: ...

    def describe(self) -> dict:
        return {
            "device_id": self.device_id,
            "kind": self.kind,
            "connected": self.connected,
            "tx_enabled": self.tx_enabled,
            "config": self.config.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "health": self.health(),
        }

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        # fault-safe: dropping the device always kills TX (FR-DEV-005)
        self.force_tx_off()
        self.connected = False

    # -- configuration -----------------------------------------------------
    def validate_config(self, cfg: DeviceConfig) -> list[str]:
        """Return violations; empty list means acceptable (FR-DEV-003)."""
        caps = self.capabilities
        problems = []
        if not caps.min_frequency <= cfg.center_frequency_hz <= caps.max_frequency:
            problems.append(
                f"center frequency {cfg.center_frequency_hz:.4g} outside "
                f"[{caps.min_frequency:.4g}, {caps.max_frequency:.4g}]")
        if not caps.min_sample_rate <= cfg.sample_rate_hz <= caps.max_sample_rate:
            problems.append(
                f"sample rate {cfg.sample_rate_hz:.4g} outside "
                f"[{caps.min_sample_rate:.4g}, {caps.max_sample_rate:.4g}]")
        if cfg.rx_bandwidth_hz > caps.max_bandwidth:
            problems.append(f"rx bandwidth {cfg.rx_bandwidth_hz:.4g} exceeds "
                            f"{caps.max_bandwidth:.4g}")
        if cfg.rx_bandwidth_hz > cfg.sample_rate_hz:
            problems.append("rx bandwidth exceeds sample rate")
        if not 0 <= cfg.rx_gain_db <= caps.max_rx_gain_db:
            problems.append(f"rx gain {cfg.rx_gain_db} outside [0, {caps.max_rx_gain_db}]")
        if not caps.min_tx_gain_db <= cfg.tx_gain_db <= caps.max_tx_gain_db:
            problems.append(
                f"tx gain {cfg.tx_gain_db} outside "
                f"[{caps.min_tx_gain_db}, {caps.max_tx_gain_db}]")
        if not caps.rx_channels > cfg.rx_channel >= 0:
            problems.append(f"rx channel {cfg.rx_channel} unavailable")
        return problems

    def configure(self, cfg: DeviceConfig) -> None:
        problems = self.validate_config(cfg)
        if problems:
            raise ConfigurationError("; ".join(problems))
        self.config = cfg

    def clamp_config(self, cfg: DeviceConfig) -> tuple[DeviceConfig, list[str]]:
        """Fit a config inside this device's capabilities, reporting changes.

        Defaults are written for a wideband Pluto+; a narrower device (a stock
        AD9363 Pluto tops out at 20 MHz RF bandwidth) must not have out-of-range
        values pushed at its driver on connect.
        """
        caps = self.capabilities
        notes: list[str] = []

        def fit(value, lo, hi, label, unit="Hz", scale=1e6, suffix="MHz"):
            new = min(max(value, lo), hi)
            if new != value:
                notes.append(f"{label} {value / scale:.4g} {suffix} -> "
                             f"{new / scale:.4g} {suffix} (device limit)")
            return new

        cfg.center_frequency_hz = fit(cfg.center_frequency_hz, caps.min_frequency,
                                      caps.max_frequency, "center frequency")
        cfg.sample_rate_hz = fit(cfg.sample_rate_hz, caps.min_sample_rate,
                                 caps.max_sample_rate, "sample rate",
                                 suffix="MSPS")
        cfg.rx_bandwidth_hz = fit(cfg.rx_bandwidth_hz, 0,
                                  min(caps.max_bandwidth, cfg.sample_rate_hz),
                                  "rx bandwidth")
        # Gains were clamped silently while every other field reported what it
        # had to move. That is the wrong way round: a transmit gain clamps
        # *upward* toward more power when a saved or requested value is below
        # the part's floor, and moving it without saying so is precisely the
        # kind of quiet change rule 3 exists to prevent.
        def fit_db(value, lo, hi, label):
            new = min(max(value, lo), hi)
            if new != value:
                notes.append(f"{label} {value:.4g} dB -> {new:.4g} dB "
                             "(device limit)")
            return new

        cfg.rx_gain_db = fit_db(cfg.rx_gain_db, 0.0, caps.max_rx_gain_db,
                                "rx gain")
        cfg.tx_gain_db = fit_db(cfg.tx_gain_db, caps.min_tx_gain_db,
                                caps.max_tx_gain_db, "tx gain")
        return cfg, notes

    # -- state reconciliation (FR-DEV-002/007) -----------------------------
    #
    # `self.config` is what was *asked for*. On real hardware that is not the
    # same claim as what the radio *has*: the AD9361 driver silently clamps
    # and quantizes almost every setting, so an unverified write is a guess,
    # and anything else touching the board — a bench script, another handle,
    # a reboot — moves it underneath us with no notification. Reporting the
    # requested value as though it were the actual one is rule 1 (inferred
    # presented as measured) and rule 3 (a problem hidden).

    def read_hardware_config(self) -> dict | None:
        """What the device actually holds, or None if it cannot be read.

        The default is correct for devices with no hardware behind them — a
        simulator's cached config *is* its state, so it cannot drift.
        """
        return self.config.to_dict()

    def sync_status(self) -> dict:
        """Compare the requested configuration against the device's own.

        Tolerances exist because quantization is not drift: the LO is
        fractional-N, gain moves in 0.25 dB steps, and the RF bandwidth lands
        on whatever analog filter is nearest. A value inside its tolerance is
        the setting we asked for, as the hardware is able to express it.
        Outside it, something other than quantization changed the radio.

        The actual value is reported either way. The tolerance decides a
        boolean; it never decides what gets shown.
        """
        out = {
            "checked_at": time.time(),
            "readable": False,
            "in_sync": None,
            "drift": [],
            "hardware": None,
            "error": None,
        }
        try:
            actual = self.read_hardware_config()
        except Exception as exc:  # noqa: BLE001 - a driver can fail any way
            out["error"] = f"could not read device state: {exc}"
            return out
        if actual is None:
            out["error"] = "this device does not expose its hardware state"
            return out

        out["readable"] = True
        out["hardware"] = actual
        requested = self.config.to_dict()
        for field, (abs_tol, rel_tol) in SYNC_TOLERANCES.items():
            if field not in actual or field not in requested:
                continue
            want, got = requested[field], actual[field]
            if want is None or got is None:
                continue
            tol = max(abs_tol, abs(want) * rel_tol)
            if abs(got - want) > tol:
                out["drift"].append({
                    "field": field,
                    "requested": want,
                    "actual": got,
                    "delta": got - want,
                    "tolerance": tol,
                })
        # Settings outside DeviceConfig that still change what a capture means.
        # Numeric ones get the same quantization allowance as a frequency;
        # a mode string is either right or it is not.
        for field, want in (actual.get("_expected") or {}).items():
            got = actual.get(field)
            if got is None:
                continue
            if isinstance(want, (int, float)) and isinstance(got, (int, float)):
                tol = SYNC_TOLERANCES["center_frequency_hz"][0]
                if abs(got - want) <= tol:
                    continue
                delta = got - want
            elif got == want:
                continue
            else:
                tol, delta = None, None
            out["drift"].append({
                "field": field, "requested": want, "actual": got,
                "delta": delta, "tolerance": tol,
            })
        out["in_sync"] = not out["drift"]
        return out

    def adopt_hardware_state(self) -> dict:
        """Take the device's own settings as the truth and report what moved.

        Used when the radio has been changed underneath us. Re-applying the
        cached values instead would fight whatever made the change and hide
        the conflict; the radio is the authority on its own state.

        Adopting cannot fix everything, so the status returned is **re-read
        afterwards** rather than the one that justified the adoption. Settings
        with no `DeviceConfig` field — a TX LO that has stopped tracking RX,
        an AGC mode that reverted to automatic — survive it, and the caller
        needs to see that rather than a stale `in_sync` from before the write.
        """
        before = self.sync_status()
        if not before["readable"]:
            raise ConfigurationError(
                before["error"] or "device state could not be read")
        actual = before["hardware"]
        adopted = []
        for field in SYNC_TOLERANCES:
            if field in actual and hasattr(self.config, field):
                if getattr(self.config, field) != actual[field]:
                    adopted.append(field)
                setattr(self.config, field, actual[field])

        after = self.sync_status()
        after["adopted"] = adopted
        after["was"] = {d["field"]: d["requested"] for d in before["drift"]}
        if after["drift"]:
            after["note"] = (
                "Adopting the radio's settings did not resolve everything. "
                + "; ".join(
                    f"{d['field']} is {d['actual']!r} and should be "
                    f"{d['requested']!r}" for d in after["drift"])
                + ". These are not configuration fields, so they need the "
                  "radio reconfigured or whatever changed it stopped.")
        if actual.get("gain_control_mode") not in (None, "manual"):
            after["rx_gain_unstable"] = True
            after["note"] = (after.get("note", "") + " Receive gain was read "
                             "while automatic gain control was active, so the "
                             "adopted figure is a sample of a moving value, "
                             "not a setting.").strip()
        return after

    def compatible_waveforms(self, catalog: dict) -> list[str]:
        """Names of catalog waveforms this device can actually transmit."""
        return [name for name, wf in catalog.items()
                if not wf.validate(self.capabilities)]

    # -- transmit ----------------------------------------------------------
    def load_waveform(self, waveform) -> None:
        self._tx_waveform = waveform

    def enable_tx(self) -> None:
        if not self.connected:
            raise ConfigurationError("device not connected")
        if self._tx_waveform is None or self._tx_waveform.kind == "rx_only":
            raise ConfigurationError("no transmit waveform loaded")
        self.tx_enabled = True

    def disable_tx(self) -> None:
        self.tx_enabled = False

    def force_tx_off(self) -> None:
        """Best-effort, exception-free TX kill used by e-stop and fault paths."""
        try:
            self.disable_tx()
        except Exception:  # noqa: BLE001
            self.tx_enabled = False

    # -- acquisition -------------------------------------------------------
    @abc.abstractmethod
    def receive(self, num_samples: int, position: dict | None = None) -> CaptureSegment:
        """Capture complex I/Q. Never lossy-transforms raw data (FR-ACQ-001)."""

    # -- health ------------------------------------------------------------
    def health(self) -> dict:
        return {"time": time.time(), "connected": self.connected}
