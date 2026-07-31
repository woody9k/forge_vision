"""Keeping the reported configuration honest about the radio (FR-DEV-002/007).

`device.config` is what was asked for. On real hardware that is a different
claim from what the radio holds: the AD9361 driver clamps and quantizes
silently, AGC overrides a gain the moment it is written, and anything else on
the bench moves the board with no notification. Showing the requested value as
the actual one is rule 1; noticing and staying quiet is rule 3.

The fake below stands in for a radio whose settings can be changed behind the
platform's back, which is the case these tests exist for.
"""

from __future__ import annotations

import time

import pytest

from forge_vision.devices.base import SYNC_TOLERANCES, ConfigurationError
from forge_vision.devices.simulated import SimulatedPluto


class DriftingRadio(SimulatedPluto):
    """A simulated radio with hardware state that can diverge from its config."""

    def __init__(self, device_id="drifty"):
        super().__init__(device_id)
        self._hw = None
        self._readable = True
        self._raise = None

    def _sync_hw(self):
        self._hw = self.config.to_dict()
        self._hw.update({"tx_lo_hz": self.config.center_frequency_hz,
                         "gain_control_mode": "manual"})

    def connect(self):
        super().connect()
        self._sync_hw()

    def configure(self, cfg):
        super().configure(cfg)
        self._sync_hw()

    def read_hardware_config(self):
        if self._raise:
            raise RuntimeError(self._raise)
        if not self._readable:
            return None
        hw = dict(self._hw)
        hw["_expected"] = {"tx_lo_hz": hw["center_frequency_hz"],
                           "gain_control_mode": "manual"}
        return hw

    # -- what something else on the bench would do -------------------------
    def bench_changed(self, **fields):
        self._hw.update(fields)


@pytest.fixture
def radio():
    r = DriftingRadio()
    r.connect()
    return r


# -- detection ---------------------------------------------------------------

def test_a_matching_radio_reports_in_sync(radio):
    s = radio.sync_status()
    assert s["readable"] is True
    assert s["in_sync"] is True
    assert s["drift"] == []


def test_quantization_is_not_drift(radio):
    """The LO is fractional-N; landing 2 Hz off is the setting, not a fault."""
    radio.bench_changed(
        center_frequency_hz=radio.config.center_frequency_hz + 2.0)
    assert radio.sync_status()["in_sync"] is True


def test_a_move_beyond_tolerance_is_drift(radio):
    radio.bench_changed(center_frequency_hz=921e6)
    s = radio.sync_status()
    assert s["in_sync"] is False
    fields = [d["field"] for d in s["drift"]]
    assert "center_frequency_hz" in fields
    hit = next(d for d in s["drift"] if d["field"] == "center_frequency_hz")
    assert hit["actual"] == 921e6
    assert hit["requested"] == radio.config.center_frequency_hz


def test_every_configured_field_is_watched(radio):
    """A setting nobody checks is a setting that drifts unnoticed."""
    radio.bench_changed(center_frequency_hz=921e6, sample_rate_hz=1e6,
                        rx_bandwidth_hz=1e6, rx_gain_db=5.0, tx_gain_db=-70.0)
    fields = {d["field"] for d in radio.sync_status()["drift"]}
    assert set(SYNC_TOLERANCES) <= fields


def test_a_tx_lo_that_stops_tracking_rx_is_drift(radio):
    """_apply() sets both LOs from one centre; divergence means something else did."""
    radio.bench_changed(tx_lo_hz=radio.config.center_frequency_hz + 5e6)
    s = radio.sync_status()
    assert s["in_sync"] is False
    assert any(d["field"] == "tx_lo_hz" for d in s["drift"])


def test_agc_reverting_to_automatic_is_drift(radio):
    """A recorded RX gain is fiction if AGC is moving it."""
    radio.bench_changed(gain_control_mode="slow_attack")
    s = radio.sync_status()
    assert s["in_sync"] is False
    assert any(d["field"] == "gain_control_mode" for d in s["drift"])


def test_an_unreadable_radio_is_not_reported_as_in_sync(radio):
    """'Cannot tell' must never render as 'fine'."""
    radio._readable = False
    s = radio.sync_status()
    assert s["readable"] is False
    assert s["in_sync"] is None          # not False, and certainly not True
    assert s["error"]


def test_a_driver_exception_is_reported_not_raised(radio):
    radio._raise = "libiio timed out"
    s = radio.sync_status()
    assert s["readable"] is False
    assert s["in_sync"] is None
    assert "libiio timed out" in s["error"]


def test_actual_values_are_always_reported(radio):
    """Tolerance decides a boolean; it never decides what gets shown."""
    radio.bench_changed(center_frequency_hz=radio.config.center_frequency_hz + 2)
    s = radio.sync_status()
    assert s["in_sync"] is True
    assert s["hardware"]["center_frequency_hz"] is not None


# -- adoption ----------------------------------------------------------------

def test_adopting_takes_the_radios_values(radio):
    radio.bench_changed(center_frequency_hz=921e6, rx_gain_db=12.0)
    out = radio.adopt_hardware_state()
    assert radio.config.center_frequency_hz == 921e6
    assert radio.config.rx_gain_db == 12.0
    assert set(out["adopted"]) >= {"center_frequency_hz", "rx_gain_db"}


def test_adoption_reports_what_it_could_not_fix(radio):
    """tx_lo and AGC mode are not config fields, so adopting cannot resolve them."""
    radio.bench_changed(gain_control_mode="slow_attack")
    out = radio.adopt_hardware_state()
    assert out["in_sync"] is False              # re-read, not the pre-adopt value
    assert any(d["field"] == "gain_control_mode" for d in out["drift"])
    assert "did not resolve everything" in out["note"]


def test_adoption_flags_a_gain_read_under_agc(radio):
    """With AGC running the gain is a moving value, not a setting."""
    radio.bench_changed(gain_control_mode="slow_attack", rx_gain_db=61.0)
    out = radio.adopt_hardware_state()
    assert out.get("rx_gain_unstable") is True
    assert "moving value" in out["note"]


def test_adoption_refuses_when_the_radio_cannot_be_read(radio):
    radio._readable = False
    with pytest.raises(ConfigurationError):
        radio.adopt_hardware_state()


# -- runtime wiring ----------------------------------------------------------

def test_runtime_records_and_reports_sync(runtime):
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    s = runtime.check_device_sync("drifty")
    assert s["in_sync"] is True
    assert runtime.sync_record("drifty")["in_sync"] is True


def test_status_exposes_sync_beside_the_config(runtime):
    """The UI must not show `config` with no indication of whether it holds."""
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    runtime.check_device_sync("drifty")
    dev = [d for d in runtime.status()["devices"] if d["device_id"] == "drifty"][0]
    assert dev["sync"]["in_sync"] is True


def test_unchecked_device_reports_null_not_in_sync(runtime):
    """Never checked is a different claim from checked and fine."""
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    dev = [d for d in runtime.status()["devices"] if d["device_id"] == "drifty"][0]
    assert dev["sync"] is None


def test_drift_reaches_the_safety_audit_log(runtime):
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    runtime.check_device_sync("drifty")
    runtime.device("drifty").bench_changed(center_frequency_hz=921e6)
    runtime.check_device_sync("drifty")
    events = [e for e in runtime.safety.audit_tail(50)
              if e["event"] == "device_drift_detected"]
    assert events and events[-1]["device"] == "drifty"


def test_only_transitions_are_audited(runtime):
    """A radio adrift for an hour must not bury the moment it happened."""
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    runtime.check_device_sync("drifty")
    runtime.device("drifty").bench_changed(center_frequency_hz=921e6)
    for _ in range(5):
        runtime.check_device_sync("drifty")
    events = [e for e in runtime.safety.audit_tail(50)
              if e["event"] == "device_drift_detected"]
    assert len(events) == 1


def test_recovery_is_audited_too(runtime):
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    runtime.check_device_sync("drifty")
    dev = runtime.device("drifty")
    dev.bench_changed(center_frequency_hz=921e6)
    runtime.check_device_sync("drifty")
    dev.bench_changed(center_frequency_hz=dev.config.center_frequency_hz)
    runtime.check_device_sync("drifty")
    assert any(e["event"] == "device_drift_cleared"
               for e in runtime.safety.audit_tail(50))


def test_a_busy_device_is_skipped_not_waited_on(runtime):
    """The watchdog must never add latency to the acquisition path."""
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    runtime.device_locks["drifty"].acquire()
    try:
        out = runtime.check_device_sync("drifty", blocking=False)
        assert out.get("skipped") == "device busy"
    finally:
        runtime.device_locks["drifty"].release()


def test_disconnected_device_is_not_claimed_in_sync(runtime):
    runtime._register(DriftingRadio("drifty"))
    out = runtime.check_device_sync("drifty")
    assert out["in_sync"] is None
    assert "not connected" in out["error"]


def test_resync_withdraws_transmit_permission(armed_runtime):
    """Permission was granted against a configuration that has now moved (rule 5)."""
    runtime = armed_runtime
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    runtime.device("drifty").bench_changed(center_frequency_hz=921e6)
    out = runtime.resync_device("drifty")
    assert runtime.device("drifty").config.center_frequency_hz == 921e6
    assert any(e["event"] == "device_resynced"
               for e in runtime.safety.audit_tail(50))


# -- watchdog ----------------------------------------------------------------

def test_watchdog_starts_stops_and_reports(runtime):
    out = runtime.start_sync_watchdog(interval_s=1.0)
    try:
        assert out["running"] is True
        assert runtime.sync_watchdog_status()["running"] is True
    finally:
        assert runtime.stop_sync_watchdog()["running"] is False
    assert runtime.sync_watchdog_status()["running"] is False


def test_watchdog_finds_drift_without_being_asked(runtime):
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    runtime.check_device_sync("drifty")
    runtime.device("drifty").bench_changed(center_frequency_hz=921e6)
    runtime.start_sync_watchdog(interval_s=0.05)
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            rec = runtime.sync_record("drifty")
            if rec and rec.get("in_sync") is False:
                break
            time.sleep(0.05)
        assert runtime.sync_record("drifty")["in_sync"] is False
    finally:
        runtime.stop_sync_watchdog()


def test_watchdog_checks_before_its_first_sleep(runtime):
    """Otherwise every device reads 'not yet checked' for a whole interval
    after a restart — exactly when someone is most likely to be looking."""
    runtime._register(DriftingRadio("drifty"))
    runtime.connect("drifty")
    runtime.start_sync_watchdog(interval_s=30.0)   # far longer than this test
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline and runtime.sync_record("drifty") is None:
            time.sleep(0.02)
        assert runtime.sync_record("drifty") is not None
    finally:
        runtime.stop_sync_watchdog()


def test_starting_twice_does_not_stack_threads(runtime):
    runtime.start_sync_watchdog(interval_s=1.0)
    try:
        first = runtime._sync_thread
        runtime.start_sync_watchdog(interval_s=1.0)
        assert runtime._sync_thread is first
    finally:
        runtime.stop_sync_watchdog()
