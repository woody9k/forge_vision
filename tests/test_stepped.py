"""Stepped-frequency synthesis (FR-WAV-002, §17 bandwidth mitigation).

The claim under test is a strong one — that sweeping the LO and combining
chunks coherently resolves targets a single 40 MHz sweep cannot separate — so
these tests check the claim end to end against a simulator that models the
thing which makes it hard: a PLL landing on an arbitrary phase at every
retune.
"""

import numpy as np
import pytest

from forge_vision.config import C_VACUUM


def _profile_peaks(profile, threshold_db=-12.0, min_sep_m=0.1):
    """Local maxima within `threshold_db` of the strongest response."""
    r = np.asarray(profile["ranges_m"])
    m = np.asarray(profile["magnitude_db"])
    idx = [i for i in range(1, len(m) - 1)
           if m[i] > m[i - 1] and m[i] >= m[i + 1] and m[i] >= threshold_db]
    idx.sort(key=lambda i: -m[i])
    chosen = []
    for i in idx:
        if all(abs(r[i] - r[j]) >= min_sep_m for j in chosen):
            chosen.append(i)
    return sorted(r[j] for j in chosen)


def _stepped(rt, **kw):
    params = dict(device_id="sim-pluto-0", start_hz=100e6, stop_hz=500e6,
                  waveform_name="fmcw_pluto_40M", overlap=0.5, chirps=2,
                  medium="air", max_range_m=30.0)
    params.update(kw)
    return rt.stepped_run(**params)


# -- the headline claim -----------------------------------------------------
def test_synthetic_bandwidth_far_exceeds_the_chunk(armed_runtime):
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 6.0,
                               "amplitude": 0.3}],
                     leakage_amplitude=1e-4)
    out = _stepped(rt)
    assert out["synthetic_bandwidth_hz"] > 350e6, \
        f"expected >350 MHz synthetic, got {out['synthetic_bandwidth_hz']/1e6:.0f}"
    # resolution must follow the synthetic span, not the 40 MHz chunk
    assert out["resolution_m"] < 0.5
    single_chunk_res = C_VACUUM / (2 * 40e6)          # 3.75 m in air
    assert out["resolution_m"] < single_chunk_res / 5


def test_resolves_two_targets_a_single_sweep_cannot(armed_runtime):
    """Two plates 1.5 m apart are inside one 40 MHz resolution cell (3.75 m
    in air) but must separate under synthesis."""
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0", targets=[
        {"kind": "plate", "range_m": 6.0, "amplitude": 0.3},
        {"kind": "plate", "range_m": 7.5, "amplitude": 0.3},
    ], leakage_amplitude=1e-4, noise_floor_dbfs=-300)

    out = _stepped(rt, max_range_m=15.0)
    peaks = _profile_peaks(out, threshold_db=-10.0, min_sep_m=0.4)
    near6 = [p for p in peaks if abs(p - 6.0) <= 0.6]
    near75 = [p for p in peaks if abs(p - 7.5) <= 0.6]
    assert near6 and near75, (
        f"expected separate returns near 6.0 and 7.5 m; got {peaks} "
        f"(resolution {out['resolution_m']:.2f} m)")


def test_target_range_is_accurate(armed_runtime):
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 9.0,
                               "amplitude": 0.3}],
                     leakage_amplitude=1e-4, noise_floor_dbfs=-300)
    out = _stepped(rt, max_range_m=20.0)
    peaks = _profile_peaks(out, threshold_db=-8.0, min_sep_m=0.5)
    assert peaks, "no return detected"
    best = min(peaks, key=lambda p: abs(p - 9.0))
    assert abs(best - 9.0) <= max(0.5, out["resolution_m"]), \
        f"range {best:.2f} m, expected 9.0 (peaks {peaks})"


# -- the correction has to earn its place -----------------------------------
def test_phase_correction_is_what_makes_it_work(armed_runtime):
    """With the PLL phase jump modelled, skipping the correction must give a
    materially worse image — otherwise the correction is unproven."""
    rt = armed_runtime
    scene = dict(targets=[{"kind": "plate", "range_m": 8.0, "amplitude": 0.3}],
                 leakage_amplitude=1e-4, noise_floor_dbfs=-300)
    rt.set_sim_scene("sim-pluto-0", **scene)
    corrected = _stepped(rt, correction="overlap", max_range_m=20.0)
    rt.set_sim_scene("sim-pluto-0", **scene)
    raw = _stepped(rt, correction="none", max_range_m=20.0)

    def contrast(p):
        m = np.asarray(p["magnitude_db"])
        return float(m.max() - np.median(m))

    assert contrast(corrected) > contrast(raw) + 3, (
        f"correction gave {contrast(corrected):.1f} dB peak-to-median vs "
        f"{contrast(raw):.1f} dB uncorrected — the correction is not earning "
        "its place")

    r = np.asarray(corrected["ranges_m"])
    peak_corrected = r[int(np.argmax(corrected["magnitude_db"]))]
    assert abs(peak_corrected - 8.0) <= max(0.5, corrected["resolution_m"])


def test_phase_steps_are_recorded(armed_runtime):
    """The per-chunk correction is part of the provenance, not hidden."""
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 6.0, "amplitude": 0.3}],
                     leakage_amplitude=1e-4)
    out = _stepped(rt)
    assert len(out["phase_steps_deg"]) == out["chunks"]
    assert out["correction"] == "overlap"
    assert out["uncorrected_chunks"] == []


# -- honesty about what was measured ----------------------------------------
def test_reported_resolution_matches_measured_span(armed_runtime):
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 6.0, "amplitude": 0.3}])
    out = _stepped(rt, start_hz=200e6, stop_hz=400e6)
    expected = out["velocity_m_per_s"] / (2 * out["synthetic_bandwidth_hz"])
    assert out["resolution_m"] == pytest.approx(expected, rel=1e-6)
    assert "degrades with depth" in out["coverage_note"]


def test_unambiguous_range_reported(armed_runtime):
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 6.0, "amplitude": 0.3}])
    out = _stepped(rt)
    assert out["unambiguous_range_m"] > out["resolution_m"]


def test_medium_scales_resolution(armed_runtime):
    """Resolution is v/(2B), so a slower medium resolves finer."""
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 4.0, "amplitude": 0.3}])
    air = _stepped(rt, medium="air")
    soil = _stepped(rt, medium="soil_dry")
    assert soil["resolution_m"] < air["resolution_m"]
    assert soil["resolution_m"] == pytest.approx(air["resolution_m"] / 2, rel=0.02)


# -- guard rails ------------------------------------------------------------
def test_band_outside_device_range_is_refused(armed_runtime):
    rt = armed_runtime
    rt.set_caps_profile("sim-pluto-0", "pluto_rev_b")     # 325 MHz - 3.8 GHz
    with pytest.raises(ValueError, match="leaves no room"):
        _stepped(rt, start_hz=70e6, stop_hz=100e6,
                 waveform_name="fmcw_pluto_40M")


def test_absurd_chunk_count_refused(armed_runtime):
    with pytest.raises(ValueError, match="limit 256"):
        _stepped(armed_runtime, start_hz=100e6, stop_hz=5000e6, overlap=0.95)


def test_transmit_is_off_afterwards_and_config_restored(armed_runtime):
    """A sweep keys the transmitter once per chunk; none of that may leak."""
    rt = armed_runtime
    before = rt.device("sim-pluto-0").config.to_dict()
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 6.0, "amplitude": 0.3}])
    _stepped(rt, start_hz=200e6, stop_hz=300e6)
    assert rt.device("sim-pluto-0").tx_enabled is False
    assert rt.safety.status()["tx_active"] is False
    assert rt.device("sim-pluto-0").config.to_dict() == before


def test_result_is_stored_with_provenance(armed_runtime):
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 6.0, "amplitude": 0.3}])
    out = _stepped(rt, start_hz=200e6, stop_hz=320e6)
    m = rt.store.load(out["experiment_id"])
    assert m["identity"]["kind"] == "stepped"
    assert m["rf_config"]["centers_hz"]
    derived = rt.store.load_derived(out["experiment_id"], "stepped_profile")
    assert derived["processing"]["stages"][1]["params"]["correction"] == "overlap"
    assert rt.store.verify(out["experiment_id"])["ok"] is True
