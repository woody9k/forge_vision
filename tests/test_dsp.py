"""DSP and ranging tests (REF-02, REF-03, REF-07, FR-DSP-010)."""

import numpy as np

from forge_vision.dsp.pipeline import Pipeline, PipelineContext
from forge_vision.waveforms import CATALOG


def _bench_scene(runtime, targets):
    runtime.set_sim_scene("sim-pluto-0", targets=targets)


def test_pipeline_determinism(armed_runtime):
    """FR-DSP-010: identical input + pipeline -> identical output."""
    wf = CATALOG["fmcw_bench_56M"]
    iq = (np.random.default_rng(7).normal(size=2 * wf.num_samples)
          + 1j * np.random.default_rng(8).normal(size=2 * wf.num_samples)) * 0.01
    pipe = Pipeline([("dc_remove", {}),
                     ("range_profile_fmcw", {"zero_pad_factor": 4}),
                     ("detect_peaks", {})])
    ctx = lambda: PipelineContext(  # noqa: E731
        sample_rate_hz=wf.sample_rate, center_frequency_hz=915e6,
        waveform=wf.preview())
    r1 = pipe.run(iq, ctx())
    r2 = pipe.run(iq, ctx())
    assert r1.products["range_profile"]["magnitude_db"] == \
        r2.products["range_profile"]["magnitude_db"]
    assert r1.record["fingerprint"] == r2.record["fingerprint"]


def test_ref02_plate_at_known_distance(armed_runtime):
    """REF-02: range peak appears near the expected distance within the
    documented resolution."""
    _bench_scene(armed_runtime, [
        {"kind": "plate", "range_m": 8.0, "amplitude": 0.08, "label": "plate"}])
    result = armed_runtime.range_run("sim-pluto-0", use_background=False)
    profile = result["range_profile"]
    peaks = result["peaks"]
    assert peaks, "no peaks detected"
    resolution = profile["resolution_m"]
    best = min(peaks, key=lambda p: abs(p["range_m"] - 8.0))
    error = abs(best["range_m"] - 8.0)
    assert error <= resolution, f"range error {error:.2f} m > resolution {resolution:.2f} m"
    # uncertainty interval must bracket the estimate (AC-003)
    lo, hi = best["range_interval_m"]
    assert lo <= best["range_m"] <= hi


def test_ref03_background_subtraction_highlights_change(armed_runtime):
    """REF-03: static background then a moved/added plate — coherent
    difference processing highlights the change and suppresses everything
    static (including the strong TX->RX leakage)."""
    # static scene (leakage + far wall) becomes the background
    _bench_scene(armed_runtime, [
        {"kind": "plate", "range_m": 12.0, "amplitude": 0.06, "label": "wall"}])
    armed_runtime.capture_background("sim-pluto-0")
    # a plate is placed at 9 m; wall and leakage are unchanged
    _bench_scene(armed_runtime, [
        {"kind": "plate", "range_m": 12.0, "amplitude": 0.06, "label": "wall"},
        {"kind": "plate", "range_m": 9.0, "amplitude": 0.02, "label": "new plate"}])
    result = armed_runtime.range_run("sim-pluto-0", use_background=True)
    assert result["range_profile"]["background_subtracted"] is True
    peaks = result["peaks"]
    assert peaks, "no peaks after subtraction"
    strongest = max(peaks, key=lambda p: p["power_db"])
    assert abs(strongest["range_m"] - 9.0) <= result["range_profile"]["resolution_m"], \
        f"strongest change at {strongest['range_m']} m, expected near 9 m"
    # unchanged returns (leakage ~1.2 m, wall 12 m) must be well suppressed
    for p in peaks:
        if abs(p["range_m"] - 9.0) > 2.0:
            assert p["power_db"] < strongest["power_db"] - 10, \
                f"static return at {p['range_m']} m not suppressed"


def test_ref07_unknown_medium_shows_interval_not_false_precision(armed_runtime):
    """REF-07: with an uncertain permittivity the platform reports a depth
    interval and the assumption, not a single confident number."""
    _bench_scene(armed_runtime, [
        {"kind": "plate", "range_m": 8.0, "amplitude": 0.08}])
    result = armed_runtime.range_run(
        "sim-pluto-0", medium="soil_moist", use_background=False)
    assert result["peaks"], "no peaks detected"
    peak = result["peaks"][0]
    lo, hi = peak["range_interval_m"]
    # soil_moist has epsilon 12 +/- 6 -> the interval must be materially wide
    assert hi - lo > 0.5, "interval suspiciously narrow for an uncertain medium"
    assert peak["confidence"]["propagation_model"] == "low"
    assert "epsilon_r" in peak["epistemic"]["derived"]
    # measured delay is preserved separately from derived range (UX-RNG-004)
    assert peak["measured_delay_s"] > 0


def test_quality_metrics_present(armed_runtime):
    result = armed_runtime.range_run("sim-pluto-0", use_background=False)
    q = result["quality"]
    assert "profile_peak_snr_db" in q
    assert "rms_amplitude" in q


def test_clipping_reported_not_concealed(armed_runtime):
    """UX-LIVE-005 / AC-005: saturation is visible in results."""
    armed_runtime.set_sim_scene(
        "sim-pluto-0",
        targets=[{"kind": "plate", "range_m": 1.0, "amplitude": 30.0}])
    result = armed_runtime.range_run("sim-pluto-0", use_background=False)
    assert result["segment"]["clipped"] is True
