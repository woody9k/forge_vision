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
    wf = CATALOG["fmcw_bench_56M"]
    with pytest.raises(SafetyViolation, match="profile"):
        armed_runtime.safety.validate_tx(1.3e9, wf, -30.0)   # not an ISM band
    armed_runtime.safety.validate_tx(915e6, wf, -30.0)       # ISM: fine


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
