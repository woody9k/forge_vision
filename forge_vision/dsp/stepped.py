"""Stepped-frequency synthesis (FR-WAV-002, §17 bandwidth mitigation).

Range resolution is `v / (2B)`, and the AD9364 caps `B` at 40 MHz — about
1.9 m in dry soil, too coarse to be useful. But `B` need not be *instantaneous*.
Sweeping the local oscillator across the band, capturing a chunk at each step,
and combining the chunks coherently makes the radar behave as though it had
the full synthetic bandwidth. 70-500 MHz in 40 MHz chunks is 430 MHz
synthetic, around 17 cm in the same soil.

The measurement each chunk yields is the channel frequency response over its
sub-band. Dechirping an FMCW sweep against its own reference gives exactly
that: at time `t` into the sweep the instantaneous RF frequency is
`f_c - B/2 + k·t`, so the dechirped samples *are* `H(f)` sampled across the
chunk.

The hard part is that a fractional-N PLL lands at an unpredictable phase every
time it retunes. Concatenating chunks that each carry an unknown phase offset
produces noise, not resolution. Two corrections are provided:

* `overlap`  — adjacent chunks share spectrum, so the complex gain that best
  aligns one to the previous is solved in the overlap and applied. Needs no
  reference path and corrects amplitude as well as phase. Default.
* `none`     — no correction, for measuring how bad the problem actually is
  on a given radio.

Nothing here invents bandwidth: the reported resolution follows from the
frequencies actually measured, and gaps between chunks are recorded rather
than interpolated over silently.
"""

from __future__ import annotations

import numpy as np

from ..config import C_VACUUM


def subband_response(iq, waveform: dict, center_hz: float,
                     sample_rate_hz: float) -> dict:
    """Channel frequency response over one chunk, from a dechirped FMCW sweep.

    Returns the RF frequency axis and the complex response on it.
    """
    if waveform.get("kind") != "fmcw":
        raise ValueError("stepped-frequency synthesis needs an fmcw chunk "
                         f"waveform, got {waveform.get('kind')!r}")
    bandwidth = float(waveform["bandwidth_hz"])
    duration = float(waveform["duration_s"])
    period = int(round(sample_rate_hz * duration))
    if period < 16:
        raise ValueError("chunk shorter than 16 samples")
    nseg = len(iq) // period
    if nseg == 0:
        raise ValueError("capture shorter than one chirp period")

    t = np.arange(period) / sample_rate_hz
    k = bandwidth / duration
    ref = np.exp(1j * 2 * np.pi * (-0.5 * bandwidth * t + 0.5 * k * t * t))

    # average the chirps coherently; a static scene repeats exactly, noise does not
    beat = np.zeros(period, dtype=np.complex128)
    for i in range(nseg):
        beat += np.asarray(iq[i * period:(i + 1) * period],
                           dtype=np.complex128) * np.conj(ref)
    beat /= nseg

    freqs = center_hz - bandwidth / 2 + k * t
    return {"freqs_hz": freqs, "response": beat, "center_hz": center_hz,
            "bandwidth_hz": bandwidth, "chirps_averaged": nseg}


def _align_to(previous: dict, current: dict) -> complex:
    """Complex gain that best maps `current` onto `previous` where they overlap.

    Least squares on the shared frequencies: a single complex number absorbs
    both the PLL's arbitrary phase and any gain step between chunks.
    """
    lo = max(previous["freqs_hz"].min(), current["freqs_hz"].min())
    hi = min(previous["freqs_hz"].max(), current["freqs_hz"].max())
    if hi <= lo:
        return None                     # no overlap; cannot align
    grid = np.linspace(lo, hi, 256)
    a = _interp_complex(previous, grid)
    b = _interp_complex(current, grid)
    denom = np.vdot(b, b)
    if abs(denom) < 1e-30:
        return None
    return complex(np.vdot(b, a) / denom)


def _interp_complex(band: dict, grid) -> np.ndarray:
    f = band["freqs_hz"]
    r = band["response"]
    order = np.argsort(f)
    f, r = f[order], r[order]
    return (np.interp(grid, f, r.real).astype(np.complex128)
            + 1j * np.interp(grid, f, r.imag))


def stitch_subbands(bands: list, correction: str = "overlap") -> dict:
    """Combine chunks into one wideband response on a uniform grid."""
    if not bands:
        raise ValueError("no sub-bands to stitch")
    bands = sorted(bands, key=lambda b: b["center_hz"])

    gains = [1.0 + 0j]
    uncorrected = []
    if correction == "overlap":
        for prev, cur in zip(bands, bands[1:]):
            g = _align_to({"freqs_hz": prev["freqs_hz"],
                           "response": prev["response"] * gains[-1]}, cur)
            if g is None:
                uncorrected.append(cur["center_hz"])
                g = 1.0 + 0j           # carry on uncorrected, and say so
            gains.append(g)
    else:
        gains = [1.0 + 0j] * len(bands)

    f_lo = min(b["freqs_hz"].min() for b in bands)
    f_hi = max(b["freqs_hz"].max() for b in bands)
    # grid fine enough to preserve the narrowest chunk's detail
    n_grid = int(2 ** np.ceil(np.log2(max(1024, 4 * sum(
        len(b["freqs_hz"]) for b in bands) // len(bands) * len(bands)))))
    n_grid = min(n_grid, 1 << 16)
    grid = np.linspace(f_lo, f_hi, n_grid)

    accum = np.zeros(n_grid, dtype=np.complex128)
    weight = np.zeros(n_grid)
    for band, g in zip(bands, gains):
        lo, hi = band["freqs_hz"].min(), band["freqs_hz"].max()
        mask = (grid >= lo) & (grid <= hi)
        if not mask.any():
            continue
        accum[mask] += _interp_complex(band, grid[mask]) * g
        weight[mask] += 1.0

    covered = weight > 0
    response = np.zeros(n_grid, dtype=np.complex128)
    response[covered] = accum[covered] / weight[covered]

    gap_hz = float((~covered).sum()) * (grid[1] - grid[0]) if n_grid > 1 else 0.0
    return {
        "freqs_hz": grid, "response": response, "covered": covered,
        "synthetic_bandwidth_hz": float(f_hi - f_lo),
        "f_lo_hz": float(f_lo), "f_hi_hz": float(f_hi),
        "chunks": len(bands),
        "correction": correction,
        "uncorrected_chunks": uncorrected,
        "uncovered_hz": gap_hz,
        "gains_db": [round(float(20 * np.log10(max(abs(g), 1e-12))), 2)
                     for g in gains],
        "phase_steps_deg": [round(float(np.degrees(np.angle(g))), 1)
                            for g in gains],
    }


def stepped_range_profile(stitched: dict, medium: dict | None = None,
                          max_range_m: float = 20.0,
                          window: bool = True) -> dict:
    """Inverse-transform the synthetic response into a range profile."""
    response = np.asarray(stitched["response"], dtype=np.complex128)
    covered = np.asarray(stitched["covered"], dtype=bool)
    n = response.size
    if n < 8:
        raise ValueError("stitched response too short to transform")

    data = response.copy()
    if window:
        # taper only the measured span, so gaps stay zero rather than ringing
        w = np.hanning(n)
        data = data * w
    data[~covered] = 0.0

    velocity = (medium or {}).get("velocity_m_per_s", C_VACUUM)
    df = float(stitched["freqs_hz"][1] - stitched["freqs_hz"][0])
    pad = int(2 ** np.ceil(np.log2(n * 4)))
    profile = np.fft.ifft(data, pad)

    # bin spacing in delay is 1/(pad*df); range is velocity*tau/2
    tau = np.arange(pad) / (pad * df)
    rng = velocity * tau / 2.0
    keep = rng <= max_range_m
    rng, profile = rng[keep], profile[keep]

    mag = np.abs(profile)
    peak = mag.max() if mag.size else 1.0
    mag_db = 20 * np.log10(np.maximum(mag, 1e-15) / max(peak, 1e-15))

    resolution = velocity / (2 * max(stitched["synthetic_bandwidth_hz"], 1.0))
    unambiguous = velocity / (2 * df) if df > 0 else float("inf")
    return {
        "ranges_m": np.round(rng, 4).tolist(),
        "magnitude_db": np.round(mag_db, 2).tolist(),
        "resolution_m": resolution,
        "unambiguous_range_m": unambiguous,
        "synthetic_bandwidth_hz": stitched["synthetic_bandwidth_hz"],
        "velocity_m_per_s": velocity,
        "chunks": stitched["chunks"],
        "correction": stitched["correction"],
        "phase_steps_deg": stitched["phase_steps_deg"],
        "uncorrected_chunks": stitched["uncorrected_chunks"],
        "coverage_note": (
            f"synthetic bandwidth {stitched['synthetic_bandwidth_hz'] / 1e6:.0f} MHz "
            f"from {stitched['chunks']} chunk(s) spanning "
            f"{stitched['f_lo_hz'] / 1e6:.0f}-{stitched['f_hi_hz'] / 1e6:.0f} MHz. "
            "Resolution follows the total span; depth reach is set by the low "
            "end, so fine detail is available near the surface and degrades "
            "with depth as the high-frequency content attenuates."),
    }
