"""Capability-fit tests (FR-DEV-002/003, FR-WAV-004).

The platform's defaults target a wideband Pluto+. A stock ADI PlutoSDR
Rev.B/C carries an AD9363: 325 MHz - 3.8 GHz and 20 MHz of RF bandwidth.
Connecting one must not push out-of-range values at the driver, and the
56 MHz waveforms must be refused with an actionable message rather than
silently mis-transmitted.
"""

import pytest

from forge_vision.devices.base import DeviceConfig
from forge_vision.devices.simulated import PLUTO_REV_B_CAPS, SimulatedPluto
from forge_vision.waveforms import CATALOG


def test_rev_b_profile_limits():
    """Measured from real hardware (Rev.B, AD9363A, firmware v0.39): the
    driver permits more bandwidth than the AD9363 datasheet specifies, and
    transmit is narrower than receive."""
    dev = SimulatedPluto("sim-revb", caps_profile="pluto_rev_b")
    caps = dev.capabilities
    assert caps.max_frequency == PLUTO_REV_B_CAPS.max_frequency == 3.8e9
    assert caps.max_bandwidth == 56e6        # RX
    assert caps.tx_bandwidth == 40e6         # TX is narrower
    assert caps.min_sample_rate == pytest.approx(2.083333e6)
    assert dev.kind == "simulated_pluto_rev_b"


def test_defaults_are_clamped_to_device_limits():
    """Platform defaults must be fitted to whatever the device really is."""
    dev = SimulatedPluto("sim-revb", caps_profile="pluto_rev_b")
    cfg, notes = dev.clamp_config(DeviceConfig(sample_rate_hz=0.5e6,
                                               rx_bandwidth_hz=56e6))
    assert cfg.sample_rate_hz == pytest.approx(2.083333e6)
    assert cfg.rx_bandwidth_hz <= cfg.sample_rate_hz     # BW cannot exceed rate
    assert any("sample rate" in n for n in notes)
    assert dev.validate_config(cfg) == []


def test_clamp_pulls_frequency_into_range():
    dev = SimulatedPluto("sim-revb", caps_profile="pluto_rev_b")
    cfg, notes = dev.clamp_config(DeviceConfig(center_frequency_hz=5.8e9))
    assert cfg.center_frequency_hz == 3.8e9
    assert any("center frequency" in n for n in notes)


def test_wideband_defaults_untouched_on_pluto_plus():
    dev = SimulatedPluto("sim-plus")
    cfg, notes = dev.clamp_config(DeviceConfig())
    assert cfg.rx_bandwidth_hz == 56e6
    assert notes == []


def test_compatible_waveform_list():
    revb = SimulatedPluto("sim-revb", caps_profile="pluto_rev_b")
    plus = SimulatedPluto("sim-plus")
    revb_ok = revb.compatible_waveforms(CATALOG)
    plus_ok = plus.compatible_waveforms(CATALOG)
    assert "fmcw_bench_56M" not in revb_ok       # 56 MHz > 40 MHz TX limit
    assert "fmcw_pluto_40M" in revb_ok           # widest this board can send
    assert "fmcw_narrow_20M" in revb_ok
    assert "fmcw_bench_56M" in plus_ok
    # a receive-only waveform transmits nothing, so it is always available
    assert "rx_only" in revb_ok and "rx_only" in plus_ok


def test_incompatible_waveform_refused_with_alternatives(armed_runtime):
    """Range Lab must refuse an impossible waveform and name usable ones."""
    armed_runtime.set_caps_profile("sim-pluto-0", "pluto_rev_b")
    with pytest.raises(ValueError, match="not supported") as exc:
        armed_runtime.range_run("sim-pluto-0", waveform_name="fmcw_bench_56M")
    assert "fmcw_pluto_40M" in str(exc.value)
    assert armed_runtime.device("sim-pluto-0").tx_enabled is False


def _revb_bench(rt, leakage: float):
    rt.set_caps_profile("sim-pluto-0", "pluto_rev_b")
    rt.configure("sim-pluto-0", {"sample_rate_hz": 30.72e6,
                                 "rx_bandwidth_hz": 20e6})
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 20.0,
                               "amplitude": 0.5}],
                     leakage_amplitude=leakage)


def test_narrowband_device_still_ranges(armed_runtime):
    """With a compatible waveform a Rev.B produces a valid range profile,
    at the coarser resolution its bandwidth allows (~7.5 m in air)."""
    _revb_bench(armed_runtime, leakage=1e-4)   # well-isolated antenna pair
    result = armed_runtime.range_run("sim-pluto-0",
                                     waveform_name="fmcw_narrow_20M",
                                     use_background=False)
    profile = result["range_profile"]
    assert profile["resolution_m"] == pytest.approx(7.5, rel=0.02)
    peaks = [p for p in result["peaks"] if not p["suspected_leakage"]]
    assert peaks, "no target peaks detected"
    best = min(peaks, key=lambda p: abs(p["range_m"] - 20.0))
    assert abs(best["range_m"] - 20.0) <= profile["resolution_m"]


def test_narrowband_leakage_flagged_and_removable(armed_runtime):
    """A 20 MHz sweep has a 7.5 m resolution cell, so TX->RX leakage smears
    across far more range than it does at 56 MHz. The platform must label
    the leakage return rather than present it as a target, and background
    subtraction must remove it while improving the real target's SNR."""
    _revb_bench(armed_runtime, leakage=3e-3)   # poorly isolated antenna pair
    raw = armed_runtime.range_run("sim-pluto-0",
                                  waveform_name="fmcw_narrow_20M",
                                  use_background=False)
    leak = [p for p in raw["peaks"] if p["suspected_leakage"]]
    target = [p for p in raw["peaks"] if not p["suspected_leakage"]]
    assert leak, "leakage return must be flagged, not offered as a target"
    assert target, "the 20 m target should still be detected"
    raw_snr = min(target, key=lambda p: abs(p["range_m"] - 20.0))["snr_db"]

    # capture the leakage-only background, then re-introduce the target
    armed_runtime.set_sim_scene("sim-pluto-0", targets=[],
                                leakage_amplitude=3e-3)
    armed_runtime.capture_background("sim-pluto-0",
                                     waveform_name="fmcw_narrow_20M")
    _revb_bench(armed_runtime, leakage=3e-3)
    out = armed_runtime.range_run("sim-pluto-0",
                                  waveform_name="fmcw_narrow_20M",
                                  use_background=True)
    assert not [p for p in out["peaks"] if p["suspected_leakage"]], \
        "background subtraction should cancel the static leakage return"
    recovered = [p for p in out["peaks"] if not p["suspected_leakage"]]
    assert recovered, "background subtraction lost the target"
    best = min(recovered, key=lambda p: abs(p["range_m"] - 20.0))
    assert abs(best["range_m"] - 20.0) <= out["range_profile"]["resolution_m"]
    assert best["snr_db"] > raw_snr + 10, \
        "removing the leakage floor should materially improve target SNR"


def test_caps_profile_switch_clamps_live_config(armed_runtime):
    armed_runtime.configure("sim-pluto-0", {"center_frequency_hz": 5.5e9,
                                            "rx_bandwidth_hz": 56e6})
    out = armed_runtime.set_caps_profile("sim-pluto-0", "pluto_rev_b")
    assert out["config"]["center_frequency_hz"] == 3.8e9
    assert out["clamp_notes"]


def test_connect_is_idempotent(runtime):
    """Re-connecting an already-open radio must not try to claim its USB
    interface a second time (which fails with a vague driver error)."""
    dev = runtime.device("sim-pluto-0")
    runtime.connect("sim-pluto-0")
    assert dev.connected is True
    runtime.connect("sim-pluto-0")          # must not raise
    assert dev.connected is True


def test_rescan_reports_already_present(runtime):
    """A second rescan must not register the same radio twice."""
    first = runtime.rescan_hardware()
    if not first["driver"]["available"]:
        pytest.skip("libiio not installed on this host")
    second = runtime.rescan_hardware()
    assert second["added"] == []
    ids = [d for d in runtime.devices if d.startswith("pluto-")]
    assert len(ids) == len(set(ids))
