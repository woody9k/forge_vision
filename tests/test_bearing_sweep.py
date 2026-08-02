"""Power against bearing, for air-looking work (FR-POS-003, FR-ACQ-001).

The platform's imaging is built for ground radar: position is linear, B-scans
are distance-versus-depth, and migration assumes a downward-looking,
co-located pair. Pointing an antenna at things needs the other axis — power
against *bearing* — and this is the receive-only half of it, which is what
the bench can do today. It is not a PPI: there is no range axis, because
range needs the radio to transmit and time its own echo.

The rule that shapes the whole product: a bearing the antenna never pointed
at is **unmeasured**, and that is a different claim from "nothing was there".
An unvisited sector must not render as a null in the pattern.
"""

from __future__ import annotations

import math

import pytest

from forge_vision.positioning import PositionSample
from forge_vision.server.runtime import bin_by_bearing


def _point(heading, peak, clipped=False):
    return {"heading_deg": heading, "peak_dbfs": peak, "clipped": clipped}


# -- the honesty rule --------------------------------------------------------

def test_an_unvisited_bearing_is_unmeasured_not_zero():
    """The failure this exists to prevent: a blank sector drawn as a null."""
    out = bin_by_bearing([_point(0, -40), _point(90, -55)], bin_deg=90.0)
    by = {b["bearing_deg"]: b for b in out["bins"]}
    assert by[180.0]["samples"] == 0
    assert by[180.0]["peak_dbfs"] is None      # not a floor value, not 0
    assert by[180.0]["mean_dbfs"] is None


def test_coverage_says_how_much_of_the_circle_was_swept():
    out = bin_by_bearing([_point(0, -40), _point(90, -50)], bin_deg=90.0)
    assert out["bins_total"] == 4
    assert out["bins_measured"] == 2
    assert out["coverage"] == 0.5
    assert sorted(out["unmeasured_bearings"]) == [180.0, 270.0]


def test_each_bin_reports_how_many_captures_landed_in_it():
    """A sector crossed once while swinging is a weaker claim than one held."""
    pts = [_point(0, -40), _point(1, -42), _point(2, -41), _point(90, -55)]
    out = bin_by_bearing(pts, bin_deg=10.0)
    by = {b["bearing_deg"]: b for b in out["bins"]}
    assert by[0.0]["samples"] == 3
    assert by[90.0]["samples"] == 1


def test_a_bin_reports_its_spread_so_a_noisy_sector_is_visible():
    out = bin_by_bearing([_point(0, -40), _point(0, -60)], bin_deg=10.0)
    by = {b["bearing_deg"]: b for b in out["bins"]}
    assert by[0.0]["peak_dbfs"] == -40.0
    assert by[0.0]["mean_dbfs"] == -50.0
    assert by[0.0]["spread_db"] == 20.0


def test_captures_without_a_heading_are_dropped_not_guessed():
    """No bearing means it cannot be placed; assuming one would invent it."""
    out = bin_by_bearing([_point(None, -40), _point(0, -50)], bin_deg=90.0)
    assert out["bins_measured"] == 1


def test_a_capture_with_no_power_reading_is_dropped():
    out = bin_by_bearing([{"heading_deg": 0, "peak_dbfs": None}], bin_deg=90.0)
    assert out["bins_measured"] == 0


# -- bearings and wrapping ---------------------------------------------------

def test_headings_wrap_around_the_circle():
    for heading in (0.0, 360.0, 720.0, -360.0):
        out = bin_by_bearing([_point(heading, -40)], bin_deg=90.0)
        by = {b["bearing_deg"]: b for b in out["bins"]}
        assert by[0.0]["samples"] == 1, heading


def test_a_heading_just_below_360_lands_in_the_zero_bin():
    """359 degrees is 1 degree from north, not 359 degrees from it."""
    out = bin_by_bearing([_point(359.0, -40)], bin_deg=10.0)
    by = {b["bearing_deg"]: b for b in out["bins"]}
    assert by[0.0]["samples"] == 1


def test_negative_headings_are_accepted():
    out = bin_by_bearing([_point(-90.0, -40)], bin_deg=90.0)
    by = {b["bearing_deg"]: b for b in out["bins"]}
    assert by[270.0]["samples"] == 1


def test_bin_size_must_be_positive():
    with pytest.raises(ValueError):
        bin_by_bearing([_point(0, -40)], bin_deg=0)


# -- derived figures ---------------------------------------------------------

def test_the_strongest_bearing_is_reported():
    out = bin_by_bearing([_point(0, -60), _point(90, -35), _point(180, -55)],
                         bin_deg=90.0)
    assert out["strongest"]["bearing_deg"] == 90.0
    assert out["strongest"]["peak_dbfs"] == -35.0


def test_front_to_back_needs_the_opposite_bearing_measured():
    """Without the back bearing the ratio is unavailable, not zero."""
    out = bin_by_bearing([_point(0, -35), _point(90, -60)], bin_deg=90.0)
    assert "front_to_back_db" not in out
    assert "never measured" in out["front_to_back_note"]


def test_front_to_back_is_computed_when_both_bearings_exist():
    out = bin_by_bearing([_point(0, -35), _point(180, -60)], bin_deg=90.0)
    assert out["front_to_back_db"] == 25.0


def test_an_empty_sweep_reports_nothing_rather_than_a_shape():
    out = bin_by_bearing([], bin_deg=90.0)
    assert out["bins_measured"] == 0
    assert "strongest" not in out
    assert out["coverage"] == 0.0


# -- acquisition -------------------------------------------------------------

class HeadingSource:
    """A position source that reports a bearing, as an orientation rig would."""

    name = "test-imu"

    def __init__(self, headings):
        self._headings = list(headings)
        self._i = 0

    def read(self):
        h = self._headings[min(self._i, len(self._headings) - 1)]
        self._i += 1
        return PositionSample(x_m=0.0, timestamp=0.0, source=self.name,
                              heading_deg=h)

    def status(self):
        return {"kind": self.name}


def test_a_sweep_without_a_heading_source_is_refused(runtime):
    """ManualSource reports no heading; plotting against assumed angles would
    be inventing the axis the whole product is about."""
    runtime.connect("sim-pluto-0")
    with pytest.raises(ValueError, match="not reporting a heading"):
        runtime.bearing_sweep("sim-pluto-0", duration_s=1.0)


def test_a_sweep_records_bearings_and_finalizes_an_experiment(runtime):
    runtime.connect("sim-pluto-0")
    runtime.position_source = HeadingSource([0, 45, 90, 135, 180])
    out = runtime.bearing_sweep("sim-pluto-0", duration_s=1.0, bin_deg=45.0,
                                samples=4096)
    assert out["captures"] >= 1
    assert out["bins_total"] == 8
    assert out["experiment_id"]
    manifest = runtime.store.load(out["experiment_id"])
    assert manifest["identity"]["kind"] == "bearing_sweep"


def test_the_sweep_restores_the_operators_configuration(runtime):
    runtime.connect("sim-pluto-0")
    runtime.position_source = HeadingSource([0])
    before = runtime.device("sim-pluto-0").config.to_dict()
    runtime.bearing_sweep("sim-pluto-0", center_hz=2437e6, duration_s=1.0,
                          samples=4096)
    assert runtime.device("sim-pluto-0").config.to_dict() == before


def test_a_sweep_is_audited(runtime):
    runtime.connect("sim-pluto-0")
    runtime.position_source = HeadingSource([0])
    runtime.bearing_sweep("sim-pluto-0", duration_s=1.0, samples=4096)
    assert any(e["event"] == "bearing_sweep"
               for e in runtime.safety.audit_tail(20))


def test_a_sweep_transmits_nothing(runtime):
    """Receive-only, so it needs no arming — assert it did not key anything."""
    runtime.connect("sim-pluto-0")
    runtime.position_source = HeadingSource([0])
    runtime.bearing_sweep("sim-pluto-0", duration_s=1.0, samples=4096)
    assert runtime.safety.state.armed is False
    assert runtime.device("sim-pluto-0").tx_enabled is False
