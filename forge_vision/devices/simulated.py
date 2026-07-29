"""Simulated Pluto+ device (§15.1 "Hardware simulation").

Physically motivated monostatic model: the received signal is the sum of the
transmit waveform delayed by each scene element's two-way propagation time,
plus direct TX->RX leakage, plus thermal noise. Delays are applied with a
frequency-domain phase ramp so sub-sample (fractional) delays are exact, and
each echo carries the carrier phase term exp(-j*2*pi*fc*tau) so phase-coherent
processing behaves as it would on hardware (FR-DSP-007).

Scene elements support position-dependent range so a linear scan over a
buried point target produces the classic B-scan hyperbola.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..config import C_VACUUM, Medium
from .base import CaptureSegment, DeviceAdapter, DeviceCapabilities


@dataclass
class SceneTarget:
    """A reflector in the simulated scene.

    kind:
      "plate"  — constant range regardless of antenna position
      "point"  — buried/point target; range depends on antenna x position
      "layer"  — planar boundary at constant depth below the scan surface
    """

    kind: str = "plate"
    range_m: float = 5.0          # plate: one-way range in air
    x_m: float = 0.0              # point: lateral position along scan axis
    depth_m: float = 1.0          # point/layer: depth below surface
    amplitude: float = 0.05       # linear reflection amplitude at reference range
    velocity_m_s: float = 0.0     # radial motion (plate only), for motion tests
    label: str = ""

    def one_way_path(self, antenna_x: float, elapsed_s: float) -> float:
        if self.kind == "plate":
            return max(0.1, self.range_m + self.velocity_m_s * elapsed_s)
        if self.kind == "point":
            dx = antenna_x - self.x_m
            return max(0.1, math.hypot(self.depth_m, dx))
        if self.kind == "layer":
            return max(0.1, self.depth_m)
        raise ValueError(f"unknown target kind {self.kind}")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "range_m": self.range_m, "x_m": self.x_m,
            "depth_m": self.depth_m, "amplitude": self.amplitude,
            "velocity_m_s": self.velocity_m_s, "label": self.label,
        }


@dataclass
class SimScene:
    targets: list[SceneTarget] = field(default_factory=list)
    medium: Medium = field(default_factory=Medium)
    # direct TX->RX coupling for a reasonably separated antenna pair; raise
    # this to demonstrate the leakage-dominance failure mode from the risk table
    leakage_amplitude: float = 0.003
    leakage_delay_s: float = 8e-9        # short internal/cable path
    noise_floor_dbfs: float = -75.0
    seed: int | None = 12345             # deterministic by default

    def to_dict(self) -> dict:
        return {
            "targets": [t.to_dict() for t in self.targets],
            "medium": self.medium.to_dict(),
            "leakage_amplitude": self.leakage_amplitude,
            "leakage_delay_s": self.leakage_delay_s,
            "noise_floor_dbfs": self.noise_floor_dbfs,
            "seed": self.seed,
        }


def default_bench_scene() -> SimScene:
    # nearest plate sits just beyond the leakage mainlobe for a 56 MHz sweep
    # (2.7 m resolution) — closer targets are only recoverable via coherent
    # background subtraction, which is exactly what the Range Lab teaches
    return SimScene(targets=[
        SceneTarget(kind="plate", range_m=8.0, amplitude=0.08, label="metal plate 8 m"),
        SceneTarget(kind="plate", range_m=14.0, amplitude=0.08, label="far wall 14 m"),
    ])


def default_scan_scene() -> SimScene:
    """Ground-scan style scene: shallow layer + two buried point targets."""
    return SimScene(
        targets=[
            SceneTarget(kind="layer", depth_m=0.9, amplitude=0.06, label="soil boundary"),
            SceneTarget(kind="point", x_m=0.8, depth_m=2.2, amplitude=0.35,
                        label="buried target A"),
            SceneTarget(kind="point", x_m=2.4, depth_m=3.6, amplitude=0.30,
                        label="buried target B"),
        ],
        medium=Medium("soil_dry", 4.0, 2.0, 3.0),
        leakage_amplitude=0.0008,   # ground-coupled, shielded antenna pair
    )


PLUTO_PLUS_CAPS = DeviceCapabilities(
    min_frequency=70e6, max_frequency=6e9,
    min_sample_rate=0.5e6, max_sample_rate=61.44e6,
    max_bandwidth=56e6, rx_channels=2, tx_channels=2,
    max_rx_gain_db=71.0, min_tx_gain_db=-89.75, max_tx_gain_db=0.0,
    transports=("usb", "ethernet"),
)

# stock ADI PlutoSDR Rev.B (AD9363A), measured from firmware v0.39:
# tuning 325 MHz-3.8 GHz, 56 MHz RX / 40 MHz TX bandwidth, 2.083-61.44 MSPS
PLUTO_REV_B_CAPS = DeviceCapabilities(
    min_frequency=325e6, max_frequency=3.8e9,
    min_sample_rate=2.083333e6, max_sample_rate=61.44e6,
    max_bandwidth=56e6, max_tx_bandwidth=40e6,
    rx_channels=1, tx_channels=1,
    max_rx_gain_db=71.0, min_tx_gain_db=-89.75, max_tx_gain_db=0.0,
    transports=("usb",),
)

CAPS_PROFILES = {
    "pluto_plus": PLUTO_PLUS_CAPS,
    "pluto_rev_b": PLUTO_REV_B_CAPS,
}


class SimulatedPluto(DeviceAdapter):
    """Virtual Pluto+ with a configurable physical scene."""

    def __init__(self, device_id: str = "sim-pluto-0", scene: SimScene | None = None,
                 caps_profile: str = "pluto_plus"):
        super().__init__(device_id)
        # lets you rehearse against the hardware you actually own: set
        # "pluto_rev_b" to get stock AD9363 limits (325 MHz-3.8 GHz, 20 MHz)
        self.caps_profile = caps_profile
        self.scene = scene or default_bench_scene()
        self.antenna_x_m = 0.0            # scan-axis position of the antenna pair
        self._t0 = time.time()
        self._temperature_c = 41.0
        self._rng_stream = np.random.default_rng(self.scene.seed)
        self.inject_sample_loss = False   # fault-injection hook for tests

    @property
    def capabilities(self) -> DeviceCapabilities:
        return CAPS_PROFILES.get(self.caps_profile, PLUTO_PLUS_CAPS)

    @property
    def kind(self) -> str:
        return f"simulated_{self.caps_profile}"

    def set_caps_profile(self, profile: str) -> list[str]:
        """Switch the emulated hardware class; returns config clamp notes."""
        if profile not in CAPS_PROFILES:
            raise ValueError(f"unknown profile {profile}; "
                             f"expected one of {sorted(CAPS_PROFILES)}")
        self.caps_profile = profile
        self.config, notes = self.clamp_config(self.config)
        return notes

    def set_scene(self, scene: SimScene) -> None:
        # the noise stream deliberately continues across scene changes so
        # repeated captures see fresh (realistic) noise; determinism still
        # holds for a fresh device with the same seed and call sequence
        self.scene = scene

    # -- physics -----------------------------------------------------------
    def _delayed(self, tx: np.ndarray, tau: float, fs: float, fc: float) -> np.ndarray:
        """Delay tx by tau seconds (fractional-accurate) incl. carrier phase."""
        n = len(tx)
        freqs = np.fft.fftfreq(n, d=1.0 / fs)
        spectrum = np.fft.fft(tx) * np.exp(-2j * np.pi * freqs * tau)
        baseband = np.fft.ifft(spectrum)
        return baseband * np.exp(-2j * np.pi * fc * tau)

    def _synthesize_rx(self, tx: np.ndarray, position: dict | None) -> np.ndarray:
        cfg = self.config
        fs = cfg.sample_rate_hz
        fc = cfg.center_frequency_hz
        n = len(tx)
        elapsed = time.time() - self._t0
        antenna_x = self.antenna_x_m
        if position and "x_m" in position:
            antenna_x = float(position["x_m"])

        rx = np.zeros(n, dtype=np.complex128)
        if self.tx_enabled:
            # direct leakage path
            rx += self.scene.leakage_amplitude * self._delayed(
                tx, self.scene.leakage_delay_s, fs, fc)
            v = self.scene.medium.velocity
            att = self.scene.medium.attenuation_db_per_m
            for tgt in self.scene.targets:
                one_way = tgt.one_way_path(antenna_x, elapsed)
                tau = 2.0 * one_way / v
                # spreading loss relative to 1 m plus medium attenuation
                amp = tgt.amplitude / max(1.0, one_way) ** 2
                amp *= 10 ** (-(att * 2 * one_way) / 20)
                rx += amp * self._delayed(tx, tau, fs, fc)

        # receiver noise, shaped by rx gain relative to a 40 dB reference
        noise_rms = 10 ** (self.scene.noise_floor_dbfs / 20)
        gain_lin = 10 ** ((cfg.rx_gain_db - 40.0) / 20)
        noise = self._rng_stream.normal(size=n) + 1j * self._rng_stream.normal(size=n)
        rx = rx * gain_lin + noise * (noise_rms / np.sqrt(2)) * gain_lin
        return rx

    # -- acquisition -------------------------------------------------------
    def receive(self, num_samples: int, position: dict | None = None) -> CaptureSegment:
        if not self.connected:
            raise RuntimeError("device not connected")
        wf = self._tx_waveform
        if wf is not None and wf.kind != "rx_only" and self.tx_enabled:
            tx = wf.generate().astype(np.complex128)
            reps = int(np.ceil(num_samples / len(tx)))
            tx = np.tile(tx, reps)[:num_samples]
        else:
            tx = np.zeros(num_samples, dtype=np.complex128)

        rx = self._synthesize_rx(tx, position)

        # clipping model: hardware saturates at digital full scale (UX-LIVE-005)
        clipped = bool(np.max(np.abs(rx.real)) > 1.0 or np.max(np.abs(rx.imag)) > 1.0)
        rx = np.clip(rx.real, -1, 1) + 1j * np.clip(rx.imag, -1, 1)

        loss_events = []
        if self.inject_sample_loss:
            # fault injection: drop a mid-buffer block and report it honestly
            start = num_samples // 3
            rx[start:start + 256] = 0
            loss_events.append({"type": "overrun", "sample_index": start, "count": 256})

        self._temperature_c += 0.01
        return CaptureSegment(
            iq=rx.astype(np.complex64),
            timestamp=time.time(),
            config=self.config.to_dict(),
            waveform=wf.preview() if wf else None,
            device_id=self.device_id,
            sample_rate_hz=self.config.sample_rate_hz,
            center_frequency_hz=self.config.center_frequency_hz,
            loss_events=loss_events,
            clipped=clipped,
            position=position,
            telemetry=self.health(),
            tx_active=self.tx_enabled,
        )

    def health(self) -> dict:
        return {
            "time": time.time(),
            "connected": self.connected,
            "temperature_c": round(self._temperature_c, 2),
            "clock": "internal",
            "transport": "virtual",
            "buffer_events": 0,
        }
