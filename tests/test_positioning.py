"""Position and pose sources (FR-POS-001..008, UX-SCN-002).

Position error becomes image error directly, so these tests concentrate on
the ways a rig lies: a stalled link reporting a stale position, an encoder
without the constants needed to convert counts to metres, and a wheel that
has drifted away from the planned grid.
"""

import json
import time

import pytest

from forge_vision.positioning import (ManualSource, PositionSample,
                                      ReplaySource, SerialSource,
                                      pose_from_sample)


class FakeSerial:
    """Stands in for a microcontroller on USB."""

    def __init__(self, lines=(), block=False):
        self._lines = [l.encode() if isinstance(l, str) else l for l in lines]
        self._i = 0
        self.closed = False
        self._block = block

    def readline(self):
        if self._i < len(self._lines):
            self._i += 1
            return self._lines[self._i - 1] + b"\n"
        time.sleep(0.01)
        return b""

    def close(self):
        self.closed = True


def _serial_source(lines, **kw):
    fake = FakeSerial(lines)
    src = SerialSource(port="fake", open_fn=lambda: fake, **kw)
    deadline = time.time() + 2
    while time.time() < deadline and src.read() is None:
        time.sleep(0.01)
    return src, fake


# -- manual (FR-POS-001) ----------------------------------------------------
def test_manual_source_records_what_the_operator_typed():
    src = ManualSource(uncertainty_m=0.02)
    assert src.read() is None
    s = src.set(1.4, height_m=0.15)
    assert s.x_m == 1.4 and s.uncertainty_m == 0.02
    assert s.height_m == 0.15
    assert src.read().x_m == 1.4


# -- serial / survey wheel (FR-POS-002) -------------------------------------
def test_serial_source_reads_position_lines():
    src, _ = _serial_source(['{"t": 1.0, "x_m": 1.372, "counts": 1830}'])
    s = src.read()
    assert s.x_m == pytest.approx(1.372)
    assert s.counts == 1830
    assert src.status()["lines_received"] >= 1
    src.close()


def test_encoder_counts_converted_with_wheel_constants():
    """The wheel is the measurement: circumference and counts per revolution
    turn pulses into metres."""
    src, _ = _serial_source(['{"t": 1.0, "counts": 1200}'],
                            wheel_circumference_m=0.3141593,
                            counts_per_revolution=2400)
    s = src.read()
    assert s.x_m == pytest.approx(0.15708, rel=1e-4)   # half a turn
    src.close()


def test_counts_without_wheel_constants_are_refused_not_guessed():
    """Deriving distance from counts needs the wheel geometry. Without it the
    sample is dropped rather than reported as some default."""
    fake = FakeSerial(['{"t": 1.0, "counts": 1200}'])
    src = SerialSource(port="fake", open_fn=lambda: fake)
    time.sleep(0.2)
    assert src.read() is None
    src.close()


def test_malformed_lines_are_counted_not_fatal():
    src, _ = _serial_source([
        "not json at all",
        "# a comment banner",
        '{"t": 1.0, "x_m": 2.5}',
    ])
    assert src.read().x_m == 2.5
    assert src.status()["bad_lines"] >= 1
    assert src.status()["last_error"]
    src.close()


def test_stale_position_is_flagged():
    """A stalled link must not present an old position as current."""
    old = time.time() - 5.0
    src, _ = _serial_source([json.dumps({"t": old, "x_m": 1.0})])
    s = src.read()
    assert s.stale_s >= 4.0
    assert any("old" in w for w in s.warnings)
    src.close()


def test_close_releases_the_port():
    src, fake = _serial_source(['{"t": 1.0, "x_m": 1.0}'])
    src.close()
    assert fake.closed is True


# -- pose model (FR-POS-003) ------------------------------------------------
def test_pose_separates_measured_from_assumed():
    """An orientation that was measured and one that was assumed must not
    look the same in the record."""
    sample = PositionSample(x_m=1.0, timestamp=time.time(), source="serial",
                            heading_deg=91.2)
    pose = pose_from_sample(sample, {"antenna_height_m": 0.2})
    assert pose["heading_deg"] == 91.2
    assert "heading_deg" in pose["measured_fields"]
    assert "pitch_deg" in pose["assumed_fields"]
    assert pose["pitch_deg"] is None, "an unmeasured angle must stay unknown"
    assert pose["height_m"] == 0.2, "plan value used where nothing was measured"


def test_measured_height_beats_the_plan():
    sample = PositionSample(x_m=1.0, timestamp=time.time(), source="serial",
                            height_m=0.42)
    pose = pose_from_sample(sample, {"antenna_height_m": 0.2})
    assert pose["height_m"] == 0.42


# -- replay (position import) -----------------------------------------------
def test_replay_source_consumes_in_order():
    src = ReplaySource([
        {"x_m": 0.0, "timestamp": 1.0, "source": "replay"},
        {"x_m": 0.5, "timestamp": 2.0, "source": "replay"},
    ])
    assert src.read().x_m == 0.0
    assert src.read().x_m == 0.5
    assert src.read() is None


# -- through the runtime ----------------------------------------------------
def _scan(rt, **over):
    plan = {"start_m": 0.0, "end_m": 1.0, "step_m": 0.25, "medium": "air",
            "chirps": 2, **over}
    return rt.scan_start("sim-pluto-0", plan)["scan_id"]


def test_scan_point_takes_position_from_the_wheel(armed_runtime):
    """UX-SCN-002: the rig supplies the position, not the operator."""
    rt = armed_runtime
    rt.set_position_source("replay", samples=[
        {"x_m": 0.02, "timestamp": time.time(), "source": "replay",
         "uncertainty_m": 0.01, "heading_deg": 90.0},
    ])
    scan_id = _scan(rt)
    out = rt.scan_point(scan_id, operator_override=True)
    assert out["accepted"] is True
    seg = rt.store.load(scan_id)["segments"][0]
    pos = seg["position"]
    assert pos["x_m"] == 0.0, "snapped to the planned grid"
    assert pos["reported_x_m"] == pytest.approx(0.02)
    assert pos["snap_error_m"] == pytest.approx(0.02)
    assert pos["heading_deg"] == 90.0
    assert pos["source"] == "replay"


def test_wheel_rolled_past_the_line_fails_the_gate(armed_runtime):
    """Inside the planned range nothing is ever more than half a step from a
    grid point, so this gate is really about a rig that has rolled off the
    end of the line — a measurement problem, not a rounding detail."""
    rt = armed_runtime
    rt.set_position_source("replay", samples=[
        {"x_m": 1.6, "timestamp": time.time(), "source": "replay"},
    ])
    scan_id = _scan(rt)                       # plan covers 0.0 - 1.0 m
    out = rt.scan_point(scan_id)
    assert out["accepted"] is False
    assert any("half a step" in g for g in out["gate_failures"])
    assert any("1.600" in g for g in out["gate_failures"])


def test_position_within_the_grid_snaps_quietly(armed_runtime):
    """A reading between two planned points is normal and must not nag."""
    rt = armed_runtime
    rt.set_position_source("replay", samples=[
        {"x_m": 0.26, "timestamp": time.time(), "source": "replay"},
    ])
    scan_id = _scan(rt)
    out = rt.scan_point(scan_id, operator_override=True)
    assert out["accepted"] is True
    pos = rt.store.load(scan_id)["segments"][0]["position"]
    assert pos["x_m"] == 0.25
    assert pos["snap_error_m"] == pytest.approx(0.01)


def test_missing_position_reading_is_an_error_not_a_guess(armed_runtime):
    rt = armed_runtime
    rt.set_position_source("replay", samples=[])
    scan_id = _scan(rt)
    with pytest.raises(ValueError, match="no reading"):
        rt.scan_point(scan_id)


def test_explicit_position_still_works(armed_runtime):
    """Manual entry remains available and is recorded as operator-supplied."""
    rt = armed_runtime
    scan_id = _scan(rt)
    out = rt.scan_point(scan_id, 0.25, operator_override=True)
    assert out["accepted"] is True
    pos = rt.store.load(scan_id)["segments"][0]["position"]
    assert pos["x_m"] == 0.25 and pos["source"] == "operator"


def test_unknown_source_kind_rejected(runtime):
    with pytest.raises(KeyError, match="unknown position source"):
        runtime.set_position_source("telepathy")


def test_position_status_reports_the_active_source(runtime):
    st = runtime.position_status()
    assert st["kind"] == "manual"
    runtime.set_position_source("replay", samples=[
        {"x_m": 1.0, "timestamp": time.time(), "source": "replay"}])
    st = runtime.position_status()
    assert st["kind"] == "replay"
    assert st["latest"]["x_m"] == 1.0


def test_serial_source_without_pyserial_explains_itself(monkeypatch):
    import forge_vision.positioning as pos
    monkeypatch.setattr(pos, "HAVE_SERIAL", False)
    with pytest.raises(RuntimeError, match="pyserial"):
        pos.SerialSource(port="/dev/ttyUSB0")
