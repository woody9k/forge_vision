"""Peak detection and characterization with explicit uncertainty (FR-DSP-005).

Each peak reports:
  * measured delay (the actual observation),
  * derived range under the active propagation model,
  * a range interval reflecting resolution and medium uncertainty (UX-RNG-004:
    measured vs inferred are separate fields, never conflated).
"""

from __future__ import annotations

import numpy as np

from ..config import C_VACUUM


def detect_peaks_from_profile(profile: dict, ctx, threshold_db: float = 10.0,
                              min_separation_m: float = 0.0,
                              max_peaks: int = 10) -> list[dict]:
    ranges = np.asarray(profile["ranges_m"])
    delays = np.asarray(profile["delays_s"])
    mag = np.asarray(profile["magnitude_db"])
    if len(mag) < 3:
        return []

    # 25th percentile, not the median: a narrowband sweep has few range bins
    # (a 20 MHz sweep gives ~46 bins over 40 m) and a strong wide return such
    # as TX leakage can occupy enough of them to drag the median up above the
    # returns themselves, suppressing every detection.
    noise_floor = float(np.percentile(mag, 25))
    threshold = noise_floor + threshold_db
    resolution = profile.get("resolution_m", 0.0)
    min_sep = max(min_separation_m, resolution * 0.75)

    # local maxima above threshold
    is_peak = (mag[1:-1] > mag[:-2]) & (mag[1:-1] >= mag[2:]) & (mag[1:-1] > threshold)
    idx = np.where(is_peak)[0] + 1
    idx = idx[np.argsort(mag[idx])[::-1]]      # strongest first

    chosen: list[int] = []
    for i in idx:
        if all(abs(ranges[i] - ranges[j]) >= min_sep for j in chosen):
            chosen.append(i)
        if len(chosen) >= max_peaks:
            break

    medium = ctx.medium or {}
    eps = medium.get("epsilon_r", 1.0)
    eps_u = medium.get("epsilon_r_uncertainty", 0.0)
    v_lo = C_VACUUM / np.sqrt(eps + eps_u) if eps + eps_u >= 1 else C_VACUUM
    v_hi = C_VACUUM / np.sqrt(max(1.0, eps - eps_u))

    peaks = []
    for i in sorted(chosen, key=lambda j: ranges[j]):
        # -3 dB width around the peak
        level = mag[i] - 3.0
        lo = i
        while lo > 0 and mag[lo] > level:
            lo -= 1
        hi = i
        while hi < len(mag) - 1 and mag[hi] > level:
            hi += 1
        width_m = float(ranges[hi] - ranges[lo])
        tau = float(delays[i])
        r_lo = v_lo * tau / 2 - resolution / 2
        r_hi = v_hi * tau / 2 + resolution / 2
        snr = float(mag[i] - noise_floor)
        confidence = _confidence(snr, eps_u)
        # a return this close to zero delay is usually direct TX->RX coupling,
        # not a real reflector — say so instead of presenting it as a target
        suspected_leakage = tau < 15e-9
        peaks.append({
            "suspected_leakage": suspected_leakage,
            "measured_delay_s": tau,                       # observation
            "range_m": round(float(ranges[i]), 3),         # derived
            "range_interval_m": [round(max(0.0, r_lo), 3), round(r_hi, 3)],
            "power_db": round(float(mag[i]), 2),
            "snr_db": round(snr, 1),
            "width_m": round(width_m, 3),
            "confidence": confidence,
            "epistemic": {
                "observation": "beat-frequency peak at measured delay",
                "derived": "range from delay via propagation model "
                           f"(epsilon_r={eps}±{eps_u})",
            },
        })
    return peaks


def _confidence(snr_db: float, eps_uncertainty: float) -> dict:
    """Confidence combines signal quality and model certainty (FR-INT-003)."""
    if snr_db >= 20:
        signal = "high"
    elif snr_db >= 10:
        signal = "medium"
    else:
        signal = "low"
    model = "high" if eps_uncertainty == 0 else ("medium" if eps_uncertainty <= 1 else "low")
    overall = min(signal, model, key=["low", "medium", "high"].index)
    return {"signal": signal, "propagation_model": model, "overall": overall}
