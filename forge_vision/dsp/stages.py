"""Core DSP stages (FR-DSP-002..008).

All stages are pure: (data, ctx, products, warnings, **params) -> data.
They never mutate their input arrays and record derived products under
well-known names in `products`.
"""

from __future__ import annotations

import numpy as np

from ..config import C_VACUUM
from .peaks import detect_peaks_from_profile
from .pipeline import stage


@stage("dc_remove", "1.0", "Remove complex DC offset")
def dc_remove(data, ctx, products, warnings):
    return data - np.mean(data)


@stage("spectrum", "1.0", "Averaged power spectral density for live views")
def spectrum(data, ctx, products, warnings, fft_size: int = 2048):
    n = (len(data) // fft_size) * fft_size
    if n == 0:
        fft_size = max(64, 2 ** int(np.log2(max(64, len(data)))))
        n = fft_size
        segs = np.resize(data, fft_size)[None, :]
    else:
        segs = data[:n].reshape(-1, fft_size)
    win = np.hanning(fft_size)
    scale = np.sum(win) ** 2
    spec = np.fft.fftshift(
        np.mean(np.abs(np.fft.fft(segs * win, axis=1)) ** 2, axis=0)) / scale
    freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, 1.0 / ctx.sample_rate_hz))
    psd_db = 10 * np.log10(spec + 1e-20)
    products["spectrum"] = {
        "freqs_hz": freqs.tolist(),
        "psd_db": np.round(psd_db, 2).tolist(),
        "center_frequency_hz": ctx.center_frequency_hz,
    }
    return data


@stage("range_profile_fmcw", "1.1",
       "Per-chirp dechirp + window + zero-padded FFT, power-averaged")
def range_profile_fmcw(data, ctx, products, warnings,
                       zero_pad_factor: int = 8, max_range_m: float = 40.0):
    wf = ctx.waveform or {}
    if wf.get("kind") != "fmcw":
        warnings.append("range_profile_fmcw requires an fmcw waveform")
        return data
    fs = ctx.sample_rate_hz
    period = int(round(fs * wf["duration_s"]))
    bandwidth = wf["bandwidth_hz"]
    k = bandwidth / wf["duration_s"]          # chirp rate Hz/s
    nseg = len(data) // period
    if nseg == 0:
        warnings.append("capture shorter than one chirp period")
        return data

    # reference chirp regenerated from the stored definition (FR-WAV-005)
    t = np.arange(period) / fs
    ref = np.exp(1j * 2 * np.pi * (-0.5 * bandwidth * t + 0.5 * k * t * t))

    win = np.hanning(period)
    nfft = int(2 ** np.ceil(np.log2(period * zero_pad_factor)))
    acc = np.zeros(nfft)
    acc_c = np.zeros(nfft, dtype=np.complex128)
    for i in range(nseg):
        seg = data[i * period:(i + 1) * period]
        beat = seg * np.conj(ref)
        spec = np.fft.fft(beat * win, nfft)
        acc += np.abs(spec) ** 2
        acc_c += spec        # beat tones are phase-stable across chirps
    power = acc / nseg
    coherent = acc_c / nseg

    freqs = np.fft.fftfreq(nfft, 1.0 / fs)
    # dechirp against conj(ref) puts a target at delay tau at beat -k*tau,
    # so delay is the NEGATED beat frequency over the chirp rate
    tau = -freqs / k
    tau = tau - ctx.cable_delay_s              # cable delay correction (FR-CAL-002)

    medium = ctx.medium or {}
    velocity = medium.get("velocity_m_per_s", C_VACUUM)
    rng = velocity * tau / 2.0

    # mask in the delay domain (max_range_m interpreted as free-space) so the
    # bin axis is identical regardless of medium — keeps an air-captured
    # background subtractable under any propagation model
    max_tau = 2.0 * max_range_m / C_VACUUM
    mask = (tau >= 0) & (tau <= max_tau)
    rng, tau_m, power, coherent = rng[mask], tau[mask], power[mask], coherent[mask]
    order = np.argsort(rng)
    rng, tau_m, power, coherent = (rng[order], tau_m[order], power[order],
                                   coherent[order])

    mag_db = 10 * np.log10(power + 1e-20)
    resolution_m = velocity / (2 * bandwidth)
    products["range_profile"] = {
        "ranges_m": np.round(rng, 4).tolist(),
        "delays_s": tau_m.tolist(),
        "magnitude_db": np.round(mag_db, 2).tolist(),
        "resolution_m": resolution_m,
        "velocity_m_per_s": velocity,
        "chirps_averaged": nseg,
        "cable_delay_s": ctx.cable_delay_s,
    }
    products["_range_power_linear"] = power       # internal
    products["_range_complex"] = coherent         # internal, for coherent subtraction
    return data


@stage("background_subtract", "2.0",
       "Coherent (complex-domain) background subtraction — cancels static "
       "clutter and leakage including cross-terms")
def background_subtract(data, ctx, products, warnings, floor_db: float = -140.0):
    profile = products.get("range_profile")
    if profile is None:
        warnings.append("background_subtract: no range profile computed yet")
        return data
    if ctx.background is None:
        warnings.append("background_subtract: no background loaded; skipped")
        return data
    current = products["_range_complex"]
    bg = np.asarray(ctx.background)
    if len(bg) != len(current):
        warnings.append("background length mismatch; skipped subtraction")
        return data
    diff_c = current - bg
    diff = np.clip(np.abs(diff_c) ** 2, 10 ** (floor_db / 10), None)
    profile["magnitude_db_raw"] = profile["magnitude_db"]
    profile["magnitude_db"] = np.round(10 * np.log10(diff), 2).tolist()
    profile["background_subtracted"] = True
    products["_range_power_linear"] = diff
    products["_range_complex"] = diff_c
    return data


@stage("detect_peaks", "1.0", "CFAR-style peak detection on the range profile")
def detect_peaks(data, ctx, products, warnings,
                 threshold_db: float = 10.0, min_separation_m: float = 0.0,
                 max_peaks: int = 10):
    profile = products.get("range_profile")
    if profile is None:
        warnings.append("detect_peaks: no range profile computed yet")
        return data
    peaks = detect_peaks_from_profile(
        profile, ctx, threshold_db=threshold_db,
        min_separation_m=min_separation_m, max_peaks=max_peaks)
    products["peaks"] = peaks
    return data


@stage("quality_metrics", "1.0", "SNR/noise/clipping quality summary (FR-DSP-008)")
def quality_metrics(data, ctx, products, warnings):
    mag = np.abs(data)
    peak = float(np.max(mag)) if len(mag) else 0.0
    rms = float(np.sqrt(np.mean(mag ** 2))) if len(mag) else 0.0
    clipped = peak > 0.98
    q = {
        "peak_amplitude": round(peak, 4),
        "rms_amplitude": round(rms, 5),
        "crest_factor_db": round(20 * np.log10((peak + 1e-12) / (rms + 1e-12)), 1),
        "near_clipping": clipped,
    }
    profile = products.get("range_profile")
    if profile is not None:
        m = np.asarray(profile["magnitude_db"])
        noise_floor = float(np.median(m))
        q["profile_noise_floor_db"] = round(noise_floor, 1)
        q["profile_peak_snr_db"] = round(float(np.max(m)) - noise_floor, 1)
    if clipped:
        warnings.append("signal near clipping; reduce gain or amplitude")
    products["quality"] = q
    return data
