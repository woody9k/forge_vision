"""Scan Studio tests (REF-05, REF-08, UX-SCN-*, FR-IMG-*)."""

import pytest


def _start_scan(rt, **plan_over):
    rt.set_sim_scene("sim-pluto-0", preset="scan")
    plan = {"start_m": 0.0, "end_m": 1.6, "step_m": 0.2,
            "medium": "soil_dry", "chirps": 2, **plan_over}
    return rt.scan_start("sim-pluto-0", plan)


def test_scan_plan_positions(armed_runtime):
    r = _start_scan(armed_runtime)
    assert r["positions_m"] == pytest.approx([0.0, 0.2, 0.4, 0.6, 0.8,
                                             1.0, 1.2, 1.4, 1.6])


def test_ref05_bscan_spatially_coherent(armed_runtime):
    """REF-05: a single buried point target produces a response whose apex
    (minimum apparent range) sits at the target's lateral position."""
    armed_runtime.set_sim_scene(
        "sim-pluto-0",
        targets=[{"kind": "point", "x_m": 0.8, "depth_m": 0.8,
                  "amplitude": 0.3, "label": "target A"}],
        medium="soil_dry",
        leakage_amplitude=1e-4)   # well-isolated antenna pair for this bench test
    r = armed_runtime.scan_start("sim-pluto-0", {
        "start_m": 0.0, "end_m": 1.6, "step_m": 0.2,
        "medium": "soil_dry", "chirps": 2})
    scan_id = r["scan_id"]
    for x in r["positions_m"]:
        out = armed_runtime.scan_point(scan_id, x, operator_override=True)
        assert out["accepted"]
    img = armed_runtime.scan_render(scan_id)

    import numpy as np
    ranges = np.asarray(img["ranges_m"])
    # power-weighted centroid gives a sub-bin apparent range per column
    apex_ranges = []
    for col in img["magnitude_db"]:
        p = 10 ** (np.asarray(col, dtype=float) / 10)
        apex_ranges.append(float(np.sum(ranges * p) / np.sum(p)))
    apex_col = int(np.argmin(apex_ranges))
    apex_x = img["positions_m"][apex_col]
    assert abs(apex_x - 0.8) <= 0.2, \
        f"hyperbola apex at {apex_x} m, expected near 0.8 m " \
        f"(column ranges: {[round(a, 3) for a in apex_ranges]})"
    # the apparent range must increase away from the apex (hyperbola shape)
    assert apex_ranges[0] > apex_ranges[apex_col]
    assert apex_ranges[-1] > apex_ranges[apex_col]


def test_position_outside_plan_rejected(armed_runtime):
    r = _start_scan(armed_runtime)
    with pytest.raises(ValueError, match="outside"):
        armed_runtime.scan_point(r["scan_id"], 5.0)


def test_scan_resume_no_duplication(armed_runtime):
    """UX-SCN-008 / REF-08: resume after interruption without duplicating
    or corrupting prior samples — even across a process restart."""
    r = _start_scan(armed_runtime)
    scan_id = r["scan_id"]
    for x in [0.0, 0.2, 0.4]:
        armed_runtime.scan_point(scan_id, x, operator_override=True)

    # simulate an application restart: fresh runtime over the same data dir
    from forge_vision.server.runtime import Runtime
    rt2 = Runtime(data_dir=armed_runtime.data_dir)
    rt2.connect("sim-pluto-0")
    rt2.safety.arm("op", "resumed session ack")
    rt2.set_sim_scene("sim-pluto-0", preset="scan")
    resumed = rt2.scan_resume(scan_id)
    assert resumed["status"]["completed_points"] == 3

    rt2.scan_point(scan_id, 0.6, operator_override=True)
    status = rt2.scans[scan_id]["builder"].status()
    assert status["completed_points"] == 4
    # re-capturing an existing point replaces it, never duplicates
    out = rt2.scan_point(scan_id, 0.6, operator_override=True)
    assert out["progress"]["repeated"] is True
    assert rt2.scans[scan_id]["builder"].status()["completed_points"] == 4


def test_interpolated_columns_marked_inferred(armed_runtime):
    """FR-IMG-005: interpolation only on request and clearly marked."""
    r = _start_scan(armed_runtime)
    scan_id = r["scan_id"]
    for x in [0.0, 0.2, 0.6, 0.8]:      # gap at 0.4
        armed_runtime.scan_point(scan_id, x, operator_override=True)
    plain = armed_runtime.scan_render(scan_id, interpolate=False)
    assert plain["magnitude_db"][2] is None or \
        all(v is None for v in plain["magnitude_db"][2] or [None])
    interp = armed_runtime.scan_render(scan_id, interpolate=True)
    assert interp["inferred_columns"][2] is True
    assert interp["inferred_columns"][1] is False
    assert interp["magnitude_db"][2] is not None


def test_quality_gate_blocks_bad_point(armed_runtime):
    """UX-SCN-003: a failing point is rejected unless the operator overrides."""
    r = _start_scan(armed_runtime)
    dev = armed_runtime.device("sim-pluto-0")
    dev.inject_sample_loss = True
    out = armed_runtime.scan_point(r["scan_id"], 0.0)
    assert out["accepted"] is False
    assert any("sample-loss" in g for g in out["gate_failures"])
    out2 = armed_runtime.scan_point(r["scan_id"], 0.0, operator_override=True)
    assert out2["accepted"] is True
    assert out2["override_used"] is True
    dev.inject_sample_loss = False


def test_finalize_partial_vs_complete(armed_runtime):
    r = _start_scan(armed_runtime, end_m=0.4)
    scan_id = r["scan_id"]
    armed_runtime.scan_point(scan_id, 0.0, operator_override=True)
    out = armed_runtime.scan_finalize(scan_id)
    assert out["status"] == "partial"

    r2 = _start_scan(armed_runtime, end_m=0.2)
    for x in r2["positions_m"]:
        armed_runtime.scan_point(r2["scan_id"], x, operator_override=True)
    out2 = armed_runtime.scan_finalize(r2["scan_id"])
    assert out2["status"] == "finalized"
    # the bscan derived product is part of the package with lineage
    m = armed_runtime.store.load(r2["scan_id"])
    assert any(d["name"] == "bscan" for d in m["derived"])
