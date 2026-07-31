"""Safety tests (§15.1 'Safety tests', FR-SAF-*, AC-004)."""

import pytest

from forge_vision.safety import SafetyViolation
from forge_vision.waveforms import CATALOG


def test_tx_disabled_at_startup(runtime):
    """AC-004 / FR-DEV-004: nothing transmits on startup or connect."""
    dev = runtime.device("sim-pluto-0")
    assert dev.tx_enabled is False
    runtime.connect("sim-pluto-0")
    assert dev.tx_enabled is False
    assert runtime.safety.status()["tx_active"] is False


def test_tx_requires_session_arm(runtime):
    """FR-SAF-001: transmit interlock must be armed explicitly each session."""
    runtime.connect("sim-pluto-0")
    with pytest.raises(SafetyViolation, match="interlock"):
        runtime.set_tx("sim-pluto-0", True, "fmcw_bench_56M")
    assert runtime.device("sim-pluto-0").tx_enabled is False


def test_arm_requires_operator_and_ack(runtime):
    with pytest.raises(SafetyViolation):
        runtime.safety.arm("", "")
    with pytest.raises(SafetyViolation):
        runtime.safety.arm("bob", "")


def test_amplitude_limit_enforced(armed_runtime):
    """FR-SAF-004: over-limit waveform amplitude is refused."""
    from dataclasses import replace
    hot = replace(CATALOG["fmcw_bench_56M"], amplitude=0.9)
    with pytest.raises(SafetyViolation, match="amplitude"):
        armed_runtime.safety.validate_tx(915e6, hot, -30.0)


def test_tx_gain_limit_enforced(armed_runtime):
    with pytest.raises(SafetyViolation, match="tx gain"):
        armed_runtime.safety.validate_tx(915e6, CATALOG["fmcw_bench_56M"], 0.0)


def test_frequency_profile_enforced(armed_runtime):
    """FR-SAF-007: frequencies outside the active profile are refused."""
    armed_runtime.safety.limits.active_profile = "ism_conservative"
    wf = CATALOG["fmcw_narrow_20M"]                          # 905-925 MHz at 915
    with pytest.raises(SafetyViolation, match="profile"):
        armed_runtime.safety.validate_tx(1.3e9, wf, -30.0)   # not an ISM band
    armed_runtime.safety.validate_tx(915e6, wf, -30.0)       # fits 902-928: fine


def test_a_sweep_wider_than_the_allocation_is_refused(armed_runtime):
    """The whole occupied span must be legal, not just its midpoint.

    This test previously asserted the opposite. fmcw_bench_56M centred at
    915 MHz occupies 887-943 MHz while the ISM allocation is 902-928, and a
    centre-only check passed it — so the platform would transmit outside the
    profile for most of every chirp while reporting itself compliant.
    """
    armed_runtime.safety.limits.active_profile = "ism_conservative"
    wide = CATALOG["fmcw_bench_56M"]
    with pytest.raises(SafetyViolation, match="occupies"):
        armed_runtime.safety.validate_tx(915e6, wide, -30.0)
    # the same sweep is fine where the allocation is wide enough for it
    armed_runtime.safety.limits.active_profile = "bench_cabled"
    armed_runtime.safety.validate_tx(915e6, wide, -30.0)


def test_a_receive_only_waveform_has_no_span_but_is_still_band_checked(armed_runtime):
    """It has no sweep to constrain, but it is not exempt from the band.

    Enabling TX keys the power amplifier, and a real transmitter leaks its LO
    at the centre frequency even with zero baseband — so the centre is still
    checked while the span check is skipped.
    """
    armed_runtime.safety.limits.active_profile = "ism_conservative"
    assert CATALOG["rx_only"].occupied_range(1.3e9) is None
    with pytest.raises(SafetyViolation, match="profile"):
        armed_runtime.safety.validate_tx(1.3e9, CATALOG["rx_only"], -30.0)
    armed_runtime.safety.validate_tx(915e6, CATALOG["rx_only"], -30.0)


def test_emergency_stop_kills_all_tx(armed_runtime):
    """FR-SAF-003: stop disables TX everywhere and disarms the session."""
    armed_runtime.set_tx("sim-pluto-0", True, "fmcw_bench_56M")
    dev = armed_runtime.device("sim-pluto-0")
    assert dev.tx_enabled is True
    armed_runtime.emergency_stop()
    assert dev.tx_enabled is False
    assert armed_runtime.safety.status()["armed"] is False
    assert armed_runtime.safety.status()["tx_active"] is False


def test_disconnect_is_fault_safe(armed_runtime):
    """FR-SAF-008 / FR-DEV-005: disconnect forces TX off."""
    armed_runtime.set_tx("sim-pluto-0", True, "fmcw_bench_56M")
    armed_runtime.disconnect("sim-pluto-0")
    assert armed_runtime.device("sim-pluto-0").tx_enabled is False


def test_capture_failure_never_leaves_tx_on(armed_runtime, monkeypatch):
    """A crash mid-capture must not leave the transmitter running."""
    dev = armed_runtime.device("sim-pluto-0")

    def boom(*a, **k):
        raise RuntimeError("injected acquisition fault")
    monkeypatch.setattr(dev, "receive", boom)
    with pytest.raises(RuntimeError):
        armed_runtime.range_run("sim-pluto-0")
    assert dev.tx_enabled is False


def test_audit_log_records_tx_lifecycle(armed_runtime):
    """FR-SAF-010: arm, start, stop are all in the audit trail."""
    armed_runtime.set_tx("sim-pluto-0", True, "fmcw_bench_56M")
    armed_runtime.set_tx("sim-pluto-0", False)
    events = [e["event"] for e in armed_runtime.safety.audit_tail()]
    assert "tx_armed" in events
    assert "tx_started" in events
    assert "tx_stopped" in events


def test_invalid_config_rejected(armed_runtime):
    """FR-DEV-003: unsupported parameter combinations are rejected."""
    from forge_vision.devices.base import ConfigurationError
    with pytest.raises(ConfigurationError):
        armed_runtime.configure("sim-pluto-0", {"center_frequency_hz": 10e9})
    with pytest.raises(ConfigurationError):
        armed_runtime.configure("sim-pluto-0", {"sample_rate_hz": 100e6})


def test_checklist_gates_arming(runtime):
    """FR-SAF-009: required pre-transmit checks must be confirmed first."""
    runtime.connect("sim-pluto-0")
    with pytest.raises(SafetyViolation, match="checklist incomplete"):
        runtime.safety.arm("op", "ack")
    assert runtime.safety.status()["armed"] is False

    status = runtime.safety.checklist_status()
    assert status["complete"] is False
    for item in runtime.safety.checklist:
        if item["required"]:
            status = runtime.safety.confirm_checklist_item(item["id"])
    assert status["complete"] is True

    runtime.safety.arm("op", "ack")          # now permitted
    assert runtime.safety.status()["armed"] is True


def test_advisory_items_do_not_block(runtime):
    runtime.connect("sim-pluto-0")
    for item in runtime.safety.checklist:
        if item["required"]:
            runtime.safety.confirm_checklist_item(item["id"])
    advisory = [i for i in runtime.safety.checklist if not i["required"]]
    assert advisory and all(not i["confirmed"] for i in advisory)
    runtime.safety.arm("op", "ack")          # advisory items still unconfirmed
    assert runtime.safety.status()["armed"] is True


def test_checklist_reset_and_audit(armed_runtime):
    status = armed_runtime.safety.reset_checklist()
    assert status["complete"] is False
    events = [e["event"] for e in armed_runtime.safety.audit_tail()]
    assert "checklist_item" in events and "checklist_reset" in events


def test_unknown_checklist_item(runtime):
    with pytest.raises(KeyError):
        runtime.safety.confirm_checklist_item("no_such_item")
