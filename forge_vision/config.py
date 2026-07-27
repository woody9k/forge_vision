"""Physical constants, units, and platform configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

C_VACUUM = 299_792_458.0  # m/s

DEFAULT_DATA_DIR = os.environ.get(
    "FORGE_VISION_DATA", os.path.join(os.path.expanduser("~"), ".forge-vision")
)


@dataclass
class Medium:
    """Propagation model for range/depth conversion (FR-CAL-008).

    Depth uncertainty from an unknown permittivity is first-class: every
    range estimate carries the assumed epsilon_r and its uncertainty so the
    UI can display an interval instead of false precision (REF-07).
    """

    name: str = "air"
    epsilon_r: float = 1.0
    epsilon_r_uncertainty: float = 0.0  # +/- absolute on epsilon_r
    attenuation_db_per_m: float = 0.0

    @property
    def velocity(self) -> float:
        return C_VACUUM / (self.epsilon_r ** 0.5)

    def velocity_bounds(self) -> tuple[float, float]:
        lo_eps = max(1.0, self.epsilon_r - self.epsilon_r_uncertainty)
        hi_eps = self.epsilon_r + self.epsilon_r_uncertainty
        # higher permittivity -> slower wave -> shallower true depth
        return C_VACUUM / (hi_eps ** 0.5), C_VACUUM / (lo_eps ** 0.5)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "epsilon_r": self.epsilon_r,
            "epsilon_r_uncertainty": self.epsilon_r_uncertainty,
            "attenuation_db_per_m": self.attenuation_db_per_m,
            "velocity_m_per_s": self.velocity,
        }


MEDIA_PRESETS: dict[str, Medium] = {
    "air": Medium("air", 1.0, 0.0),
    "drywall": Medium("drywall", 2.5, 0.5, 1.0),
    "concrete_dry": Medium("concrete_dry", 5.5, 1.5, 5.0),
    "soil_dry": Medium("soil_dry", 4.0, 2.0, 3.0),
    "soil_moist": Medium("soil_moist", 12.0, 6.0, 10.0),
    "water_fresh": Medium("water_fresh", 81.0, 4.0, 40.0),
}


@dataclass
class SafetyLimits:
    """Configurable transmit limits enforced by the safety controller (FR-SAF-004)."""

    max_amplitude: float = 0.25          # digital full-scale fraction
    max_duty_cycle: float = 1.0   # FMCW sweeps are continuous while capturing
    max_tx_gain_db: float = -10.0
    min_frequency_hz: float = 70e6
    max_frequency_hz: float = 6e9
    # frequency profiles: name -> list of allowed [lo, hi] bands (FR-SAF-007)
    frequency_profiles: dict = field(default_factory=lambda: {
        "bench_cabled": [[70e6, 6e9]],           # closed-circuit, cabled/attenuated
        "ism_conservative": [[433.05e6, 434.79e6],
                             [902e6, 928e6],
                             [2.4e9, 2.4835e9],
                             [5.725e9, 5.875e9]],
    })
    active_profile: str = "bench_cabled"

    def allowed_bands(self) -> list[list[float]]:
        return self.frequency_profiles.get(self.active_profile, [])

    def to_dict(self) -> dict:
        return {
            "max_amplitude": self.max_amplitude,
            "max_duty_cycle": self.max_duty_cycle,
            "max_tx_gain_db": self.max_tx_gain_db,
            "min_frequency_hz": self.min_frequency_hz,
            "max_frequency_hz": self.max_frequency_hz,
            "frequency_profiles": self.frequency_profiles,
            "active_profile": self.active_profile,
        }
