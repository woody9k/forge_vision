"""Band survey tests (receive-only occupancy sweep)."""

import pytest


def test_survey_is_receive_only(runtime):
    """The sweep must never transmit — no arming, no TX, ever."""
    runtime.connect("sim-pluto-0")
    assert runtime.safety.status()["armed"] is False
    out = runtime.band_survey("sim-pluto-0", 902e6, 912e6, step_hz=2e6,
                              samples=16384)
    assert len(out["points"]) == 6
    assert runtime.device("sim-pluto-0").tx_enabled is False
    assert runtime.safety.status()["tx_active"] is False
    assert "tx_started" not in [e["event"] for e in runtime.safety.audit_tail()]


def test_survey_restores_operator_config(runtime):
    runtime.connect("sim-pluto-0")
    runtime.configure("sim-pluto-0", {"center_frequency_hz": 2.4e9,
                                      "rx_gain_db": 55})
    before = runtime.device("sim-pluto-0").config.to_dict()
    runtime.band_survey("sim-pluto-0", 902e6, 908e6, step_hz=2e6, samples=16384)
    assert runtime.device("sim-pluto-0").config.to_dict() == before


def test_survey_reports_occupancy_and_extremes(runtime):
    runtime.connect("sim-pluto-0")
    out = runtime.band_survey("sim-pluto-0", 902e6, 920e6, step_hz=2e6,
                              samples=16384)
    for p in out["points"]:
        assert p["noise_floor_dbfs"] is not None
        assert 0.0 <= p["occupancy"] <= 1.0
        assert p["peak_dbfs"] >= p["noise_floor_dbfs"]
    assert out["quietest"]["peak_dbfs"] <= out["busiest"]["peak_dbfs"]


def test_survey_stored_as_experiment(runtime):
    runtime.connect("sim-pluto-0")
    out = runtime.band_survey("sim-pluto-0", 902e6, 906e6, step_hz=2e6,
                              samples=16384)
    m = runtime.store.load(out["experiment_id"])
    assert m["identity"]["kind"] == "survey"
    assert m["identity"]["status"] == "finalized"
    product = runtime.store.load_derived(out["experiment_id"], "band_survey")
    assert product["product"]["points"]
    assert runtime.store.verify(out["experiment_id"])["ok"] is True


def test_survey_clamps_to_device_range(runtime):
    """A request outside the radio's tuning range is fitted, not attempted."""
    runtime.connect("sim-pluto-0")
    runtime.set_caps_profile("sim-pluto-0", "pluto_rev_b")   # 325 MHz-3.8 GHz
    out = runtime.band_survey("sim-pluto-0", 100e6, 400e6, step_hz=25e6,
                              samples=16384)
    assert out["start_hz"] == 325e6
    assert all(p["center_hz"] >= 325e6 for p in out["points"])


def test_survey_rejects_absurd_step_count(runtime):
    runtime.connect("sim-pluto-0")
    with pytest.raises(ValueError, match="limit 400"):
        runtime.band_survey("sim-pluto-0", 500e6, 3e9, step_hz=1e6)


def test_survey_requires_connected_device(runtime):
    with pytest.raises(ValueError, match="not connected"):
        runtime.band_survey("sim-pluto-0", 902e6, 906e6)
