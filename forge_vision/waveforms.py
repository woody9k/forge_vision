"""Waveform catalog: named, versioned waveform definitions (FR-WAV-001..005).

Every waveform is a reproducible *definition*; the generated samples are a
deterministic function of the definition, so storing the definition with an
experiment satisfies FR-WAV-005 (reference preservation).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np


@dataclass(frozen=True)
class Waveform:
    name: str
    version: str
    kind: str                      # "cw" | "fmcw" | "stepped" | "rx_only"
    sample_rate: float             # Hz
    duration_s: float
    amplitude: float = 0.2         # digital full-scale fraction
    bandwidth_hz: float = 0.0      # sweep bandwidth for fmcw/stepped
    steps: int = 0                 # for stepped-frequency
    duty_cycle: float = 1.0
    intended_use: str = ""
    processing: str = ""           # expected processing method (UX preview)

    @property
    def num_samples(self) -> int:
        return int(round(self.sample_rate * self.duration_s))

    @property
    def chirp_rate(self) -> float:
        """Hz per second of sweep, for dechirp range conversion."""
        if self.kind == "fmcw" and self.duration_s > 0:
            return self.bandwidth_hz / self.duration_s
        return 0.0

    def generate(self) -> np.ndarray:
        """Deterministic complex baseband samples."""
        n = self.num_samples
        t = np.arange(n) / self.sample_rate
        if self.kind == "rx_only":
            return np.zeros(n, dtype=np.complex64)
        if self.kind == "cw":
            return (self.amplitude * np.ones(n)).astype(np.complex64)
        if self.kind == "fmcw":
            k = self.chirp_rate
            phase = 2 * np.pi * (-0.5 * self.bandwidth_hz * t + 0.5 * k * t * t)
            return (self.amplitude * np.exp(1j * phase)).astype(np.complex64)
        if self.kind == "stepped":
            steps = max(2, self.steps)
            samples_per_step = n // steps
            freqs = np.linspace(-self.bandwidth_hz / 2, self.bandwidth_hz / 2, steps)
            out = np.zeros(n, dtype=np.complex64)
            for i, f in enumerate(freqs):
                s = slice(i * samples_per_step, (i + 1) * samples_per_step)
                tt = t[s]
                out[s] = self.amplitude * np.exp(2j * np.pi * f * tt)
            return out
        raise ValueError(f"unknown waveform kind: {self.kind}")

    def preview(self) -> dict:
        """Metadata shown before transmission (FR-WAV-003)."""
        return {
            **asdict(self),
            "num_samples": self.num_samples,
            "chirp_rate_hz_per_s": self.chirp_rate,
        }

    def occupied_range(self, center_frequency_hz: float) -> tuple | None:
        """Lowest and highest frequency this waveform actually puts on air.

        Checking only the centre frequency is not a band check: a 56 MHz sweep
        centred inside a 26 MHz ISM allocation is legal at its centre and
        illegal across most of its span. Returns None for a waveform that never
        transmits, which has no occupied range to constrain (FR-SAF-004).
        """
        if self.kind == "rx_only":
            return None
        half = max(self.bandwidth_hz, 0.0) / 2.0
        return (center_frequency_hz - half, center_frequency_hz + half)

    def validate(self, capabilities) -> list[str]:
        """Return a list of violations against device capabilities (FR-WAV-004)."""
        problems = []
        if self.kind == "rx_only":
            # a receive-only waveform never transmits, so amplitude/bandwidth
            # limits do not apply to it
            if self.sample_rate > capabilities.max_sample_rate:
                problems.append(
                    f"sample rate {self.sample_rate:.3g} exceeds device max "
                    f"{capabilities.max_sample_rate:.3g}")
            return problems
        if self.sample_rate > capabilities.max_sample_rate:
            problems.append(
                f"sample rate {self.sample_rate:.3g} exceeds device max "
                f"{capabilities.max_sample_rate:.3g}")
        if self.bandwidth_hz > capabilities.tx_bandwidth:
            problems.append(
                f"bandwidth {self.bandwidth_hz:.3g} exceeds device transmit max "
                f"{capabilities.tx_bandwidth:.3g}")
        if not 0 < self.amplitude <= 1.0:
            problems.append(f"amplitude {self.amplitude} outside (0, 1]")
        if not 0 < self.duty_cycle <= 1.0:
            problems.append(f"duty cycle {self.duty_cycle} outside (0, 1]")
        if self.num_samples < 16:
            problems.append("waveform shorter than 16 samples")
        return problems


CATALOG: dict[str, Waveform] = {}


def _register(w: Waveform) -> None:
    CATALOG[w.name] = w


_register(Waveform(
    name="fmcw_bench_56M", version="1.0", kind="fmcw",
    sample_rate=61.44e6, duration_s=1e-3, amplitude=0.2, bandwidth_hz=56e6,
    intended_use="Bench ranging; ~2.7 m free-space resolution",
    processing="dechirp + FFT range profile"))

_register(Waveform(
    name="fmcw_pluto_40M", version="1.0", kind="fmcw",
    sample_rate=61.44e6, duration_s=1e-3, amplitude=0.2, bandwidth_hz=40e6,
    intended_use="Widest sweep a stock AD9363 Pluto will transmit; "
                 "~3.7 m free-space resolution. Exceeds the AD9363's 20 MHz "
                 "datasheet spec — verify flatness before trusting accuracy",
    processing="dechirp + FFT range profile"))

_register(Waveform(
    name="fmcw_narrow_20M", version="1.0", kind="fmcw",
    sample_rate=30.72e6, duration_s=1e-3, amplitude=0.2, bandwidth_hz=20e6,
    intended_use="Lower-rate links; ~7.5 m resolution",
    processing="dechirp + FFT range profile"))

_register(Waveform(
    name="cw_probe", version="1.0", kind="cw",
    sample_rate=2.0e6, duration_s=5e-3, amplitude=0.1,
    intended_use="Leakage/level probing and Doppler experiments",
    processing="spectral analysis"))

_register(Waveform(
    name="stepped_64", version="1.0", kind="stepped",
    sample_rate=61.44e6, duration_s=4e-3, amplitude=0.2,
    bandwidth_hz=56e6, steps=64,
    intended_use="Stepped-frequency ranging experiments",
    processing="per-step phase -> IFFT synthetic range profile"))

_register(Waveform(
    name="rx_only", version="1.0", kind="rx_only",
    sample_rate=2.0e6, duration_s=10e-3, amplitude=0.0,
    intended_use="Passive observation; no transmission",
    processing="spectrum/waterfall"))
