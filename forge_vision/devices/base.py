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
    max_bandwidth: float
    rx_channels: int = 1
    tx_channels: int = 1
    max_rx_gain_db: float = 70.0
    min_tx_gain_db: float = -89.0
    max_tx_gain_db: float = 0.0
    transports: tuple = ("usb",)

    def to_dict(self) -> dict:
        return asdict(self)


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
