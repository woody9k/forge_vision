"""Transmit authorization survives nothing it should not (FR-SAF-004).

These tests exist because of a confirmed defect. `configure()` applied device
settings without consulting the interlock, so a transmitter that had been
approved for one configuration kept transmitting through arbitrary changes to
another. Observed on the simulator before the fix:

    set_tx(1500 MHz)             -> SafetyViolation, not inside profile
    configure(1500 MHz) while TX -> tx_enabled stayed True at 1500 MHz
    tx_gain -30 dB -> 0 dB       -> tx_enabled stayed True, 1000x the power

The gate worked when asked and was walked straight past by reconfiguration,
while CLAUDE.md claimed every TX path ran through SafetyController.

The rule now is that permission belongs to a *configuration*, not a device:
anything that changes what the radio is actually emitting withdraws it.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from forge_vision.safety import SafetyViolation
from forge_vision.waveforms import CATALOG

# 20 MHz wide, so it fits inside the 26 MHz ISM allocation at 915 MHz
WF = "fmcw_narrow_20M"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_VISION_DATA", str(tmp_path / "data"))
    import forge_vision.config as config
    importlib.reload(config)
    import forge_vision.server.runtime as runtime_mod
    importlib.reload(runtime_mod)
    import forge_vision.server.app as app_mod
    importlib.reload(app_mod)
    return TestClient(app_mod.app)


def _tx_on(rt, device_id="sim-pluto-0", waveform=WF):
    rt.set_tx(device_id, True, waveform)
    assert rt.device(device_id).tx_enabled is True


# -- the reconfiguration bypass ---------------------------------------------
def test_changing_frequency_stops_transmit(armed_runtime):
    rt = armed_runtime
    _tx_on(rt)
    rt.configure("sim-pluto-0", {"center_frequency_hz": 2450e6})
    assert rt.device("sim-pluto-0").tx_enabled is False
    assert rt.safety.status()["tx_active"] is False


def test_raising_tx_gain_stops_transmit(armed_runtime):
    """The worst case: -30 dB to 0 dB is a thousandfold power increase."""
    rt = armed_runtime
    _tx_on(rt)
    rt.configure("sim-pluto-0", {"tx_gain_db": 0.0})
    assert rt.device("sim-pluto-0").tx_enabled is False


def test_changing_sample_rate_stops_transmit(armed_runtime):
    rt = armed_runtime
    # narrow the RF bandwidth first so the later rate change is a valid device
    # configuration on its own — otherwise the device rejects it for an
    # unrelated reason and the test proves nothing about authorization
    rt.configure("sim-pluto-0", {"rx_bandwidth_hz": 20e6})
    _tx_on(rt)
    rt.configure("sim-pluto-0", {"sample_rate_hz": 30.72e6})
    assert rt.device("sim-pluto-0").tx_enabled is False


def test_changing_rf_bandwidth_stops_transmit(armed_runtime):
    rt = armed_runtime
    _tx_on(rt)
    rt.configure("sim-pluto-0", {"rx_bandwidth_hz": 20e6})
    assert rt.device("sim-pluto-0").tx_enabled is False


def test_changing_the_frequency_profile_stops_transmit(armed_runtime):
    """The profile decides what is legal, so narrowing it must withdraw a
    permission granted under the wider one."""
    rt = armed_runtime
    rt.safety.limits.active_profile = "bench_cabled"
    rt.configure("sim-pluto-0", {"center_frequency_hz": 1.3e9})
    _tx_on(rt)
    status = rt.set_frequency_profile("ism_conservative")
    assert rt.device("sim-pluto-0").tx_enabled is False
    assert status["tx_revoked"] == ["sim-pluto-0"]


def test_declaring_path_attenuation_stops_transmit(armed_runtime):
    """Path attenuation is an input to the receive-protection decision, so a
    permission granted under the old figure no longer applies."""
    rt = armed_runtime
    _tx_on(rt)
    rt.declare_path_attenuation(40.0)
    assert rt.device("sim-pluto-0").tx_enabled is False


def test_an_unrelated_change_does_not_stop_transmit(armed_runtime):
    """The interlock must not be so eager that it is unusable: a setting the
    emission does not depend on should leave TX alone."""
    rt = armed_runtime
    _tx_on(rt)
    rt.configure("sim-pluto-0", {"buffer_size": 32768})
    assert rt.device("sim-pluto-0").tx_enabled is True


def test_reapplying_the_same_configuration_does_not_stop_transmit(armed_runtime):
    rt = armed_runtime
    _tx_on(rt)
    current = rt.device("sim-pluto-0").config.center_frequency_hz
    rt.configure("sim-pluto-0", {"center_frequency_hz": current})
    assert rt.device("sim-pluto-0").tx_enabled is True


def test_revocation_is_audited(armed_runtime):
    rt = armed_runtime
    _tx_on(rt)
    rt.configure("sim-pluto-0", {"tx_gain_db": 0.0})
    events = [e["event"] for e in rt.safety.audit_tail(50)]
    assert "tx_authorization_revoked" in events


# -- the fingerprint itself ---------------------------------------------------
def test_fingerprint_covers_the_safety_relevant_inputs(runtime):
    s = runtime.safety
    wf = CATALOG[WF]
    base = s.tx_fingerprint(915e6, wf, -30.0, rx_gain_db=40.0)
    assert base == s.tx_fingerprint(915e6, wf, -30.0, rx_gain_db=40.0)
    assert base != s.tx_fingerprint(916e6, wf, -30.0, rx_gain_db=40.0)
    assert base != s.tx_fingerprint(915e6, wf, -29.0, rx_gain_db=40.0)
    assert base != s.tx_fingerprint(915e6, wf, -30.0, rx_gain_db=50.0)
    assert base != s.tx_fingerprint(915e6, CATALOG["cw_probe"], -30.0, rx_gain_db=40.0)
    s.limits.active_profile = "ism_conservative"
    assert base != s.tx_fingerprint(915e6, wf, -30.0, rx_gain_db=40.0)


# -- occupied band, not just the midpoint ------------------------------------
def test_a_sweep_that_overflows_the_allocation_is_refused(armed_runtime):
    rt = armed_runtime
    rt.safety.limits.active_profile = "ism_conservative"
    with pytest.raises(SafetyViolation, match="occupies"):
        rt.set_tx("sim-pluto-0", True, "fmcw_bench_56M")   # 887-943 vs 902-928
    assert rt.device("sim-pluto-0").tx_enabled is False


def test_a_sweep_that_fits_is_allowed(armed_runtime):
    rt = armed_runtime
    rt.safety.limits.active_profile = "ism_conservative"
    rt.set_tx("sim-pluto-0", True, WF)                     # 905-925
    assert rt.device("sim-pluto-0").tx_enabled is True


# -- emergency stop and shutdown ---------------------------------------------
def test_emergency_stop_latches_acquisition(armed_runtime):
    """A stop that leaves sweeps running has not stopped the instrument."""
    rt = armed_runtime
    _tx_on(rt)
    out = rt.emergency_stop()
    assert out["stopped"] is True
    assert rt.device("sim-pluto-0").tx_enabled is False
    assert rt.stop_acquisition.is_set()
    with pytest.raises(ValueError, match="acquisition is stopped"):
        rt.band_survey("sim-pluto-0", 100e6, 120e6, step_hz=10e6)


def test_the_latch_must_be_lifted_deliberately(armed_runtime):
    rt = armed_runtime
    rt.emergency_stop()
    assert rt.status()["acquisition_stopped"] is True
    out = rt.resume_acquisition()
    assert out["was_stopped"] is True
    assert rt.status()["acquisition_stopped"] is False
    rt.band_survey("sim-pluto-0", 100e6, 120e6, step_hz=10e6)


def test_arming_lifts_the_latch(armed_runtime):
    rt = armed_runtime
    rt.emergency_stop()
    assert rt.stop_acquisition.is_set()
    rt.arm("operator", "checked and clear")
    assert rt.stop_acquisition.is_set() is False


def test_shutdown_stops_transmit_and_disarms(armed_runtime):
    rt = armed_runtime
    _tx_on(rt)
    out = rt.shutdown("test")
    assert rt.device("sim-pluto-0").tx_enabled is False
    assert rt.safety.status()["armed"] is False
    assert out["shutdown"] == "test"
    assert "runtime_shutdown" in [e["event"] for e in rt.safety.audit_tail(50)]


# -- API ---------------------------------------------------------------------
def test_configure_over_the_api_stops_transmit(client):
    for item in client.get("/api/safety/checklist").json()["items"]:
        if item["required"]:
            client.post("/api/safety/checklist",
                        json={"id": item["id"], "confirmed": True})
    client.post("/api/safety/arm", json={"operator": "t", "acknowledgement": "ack"})
    client.post("/api/devices/sim-pluto-0/connect")
    client.post("/api/devices/sim-pluto-0/tx", json={"enable": True, "waveform": WF})
    assert client.get("/api/status").json()["safety"]["tx_active"] is True

    client.post("/api/devices/sim-pluto-0/configure", json={"tx_gain_db": 0.0})
    assert client.get("/api/status").json()["safety"]["tx_active"] is False


def test_resume_endpoint(client):
    client.post("/api/safety/stop")
    assert client.get("/api/status").json()["acquisition_stopped"] is True
    assert client.post("/api/safety/resume").json()["was_stopped"] is True
    assert client.get("/api/status").json()["acquisition_stopped"] is False
