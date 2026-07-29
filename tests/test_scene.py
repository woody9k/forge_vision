"""Scene Builder tests (release 0.4): migration, site registration, cross-scan
fusion, depth slices, and report export.

Milestone D from the spec: "Run perpendicular or repeated scans, align them,
and show a persistent anomaly in world coordinates."
"""

import math

import pytest

from forge_vision.imaging.migration import focused_targets, migrate_bscan
from forge_vision.sites import depth_slice, fuse_targets, scan_to_site


def _synthetic_bscan(x0: float, depth: float, positions, ranges,
                     amplitude_db: float = 0.0, width: float = 0.35):
    """A textbook hyperbola for a point target at (x0, depth)."""
    cols = []
    for x in positions:
        r = math.hypot(depth, x - x0)
        col = [amplitude_db - 40 * ((rr - r) / width) ** 2 - 30
               for rr in ranges]
        cols.append([max(v, -90.0) for v in col])
    return cols


def test_migration_focuses_hyperbola_to_apex():
    """A hyperbola from a point target must collapse to a focused spot at
    the target's true position — that is what migration is for."""
    positions = [i * 0.1 for i in range(33)]        # 0 .. 3.2 m
    ranges = [i * 0.05 for i in range(120)]         # 0 .. 6 m
    bscan = _synthetic_bscan(1.6, 1.2, positions, ranges)

    mig = migrate_bscan(positions, ranges, bscan, depth_step_m=0.05,
                        max_depth_m=4.0)
    targets = focused_targets(mig, threshold_db=-6.0)
    assert targets, "migration found no focused target"
    best = max(targets, key=lambda t: t["amplitude_db"])
    assert abs(best["x_m"] - 1.6) <= 0.15, f"lateral error {best['x_m']}"
    assert abs(best["depth_m"] - 1.2) <= 0.2, f"depth error {best['depth_m']}"


def test_migration_resolves_two_targets():
    positions = [i * 0.1 for i in range(41)]
    ranges = [i * 0.05 for i in range(120)]
    a = _synthetic_bscan(1.0, 1.0, positions, ranges)
    b = _synthetic_bscan(3.0, 1.8, positions, ranges)
    combined = [[max(pa, pb) for pa, pb in zip(ra, rb)] for ra, rb in zip(a, b)]

    mig = migrate_bscan(positions, ranges, combined, depth_step_m=0.05,
                        max_depth_m=4.0)
    targets = focused_targets(mig, threshold_db=-10.0, min_separation_m=0.5)
    xs = sorted(t["x_m"] for t in targets)
    assert any(abs(x - 1.0) <= 0.2 for x in xs), f"missing target near 1.0: {xs}"
    assert any(abs(x - 3.0) <= 0.2 for x in xs), f"missing target near 3.0: {xs}"


def test_migration_needs_measured_columns():
    positions = [0.0, 0.1, 0.2]
    ranges = [0.0, 0.1, 0.2]
    with pytest.raises(ValueError, match="two measured scan points"):
        migrate_bscan(positions, ranges, [None, None, None])


def test_scan_to_site_transform():
    """A scan heading 90 degrees runs along +y, not +x."""
    p = {"origin_x_m": 2.0, "origin_y_m": 1.0, "heading_deg": 90.0}
    x, y = scan_to_site(p, 3.0)
    assert x == pytest.approx(2.0, abs=1e-9)
    assert y == pytest.approx(4.0, abs=1e-9)


def _result(exp_id, origin, heading, targets, eps=4.0, eps_u=2.0, label=""):
    return {
        "experiment_id": exp_id,
        "placement": {"experiment_id": exp_id, "origin_x_m": origin[0],
                      "origin_y_m": origin[1], "heading_deg": heading,
                      "label": label or exp_id, "position_uncertainty_m": 0.05},
        "medium": {"name": "soil_dry", "epsilon_r": eps,
                   "epsilon_r_uncertainty": eps_u},
        "targets": targets,
    }


def test_perpendicular_scans_confirm_one_anomaly():
    """Milestone D: two scans crossing the same buried object must fuse into
    a single finding supported by both, not two separate findings."""
    # object at site (2.0, 1.5), 1.2 m deep.
    # scan A runs along +x from (0, 1.5): sees it 2.0 m along.
    # scan B runs along +y from (2.0, 0): sees it 1.5 m along.
    a = _result("expA", (0.0, 1.5), 0.0,
                [{"x_m": 2.0, "depth_m": 1.2, "amplitude_db": -1.0,
                  "contrast_db": 14.0}], label="east-west")
    b = _result("expB", (2.0, 0.0), 90.0,
                [{"x_m": 1.5, "depth_m": 1.25, "amplitude_db": -2.0,
                  "contrast_db": 11.0}], label="north-south")

    findings = fuse_targets([a, b])
    assert len(findings) == 1, f"expected one fused finding, got {findings}"
    f = findings[0]
    assert f["supporting_scans"] == 2
    assert set(f["scan_ids"]) == {"expA", "expB"}
    assert abs(f["site_x_m"] - 2.0) <= 0.1
    assert abs(f["site_y_m"] - 1.5) <= 0.1
    assert f["confidence"]["lateral_position"] == "high"
    # permittivity is uncertain, so depth must not claim high confidence
    assert f["confidence"]["depth"] == "low"
    assert f["confidence"]["overall"] == "low"
    lo, hi = f["depth_interval_m"]
    assert lo < f["depth_m"] < hi, "depth interval must bracket the estimate"
    assert f["classification"] == "persistent anomaly, unknown type"


def test_distant_responses_stay_separate():
    a = _result("expA", (0.0, 0.0), 0.0,
                [{"x_m": 1.0, "depth_m": 1.0, "amplitude_db": 0.0,
                  "contrast_db": 12.0}])
    b = _result("expB", (0.0, 5.0), 0.0,
                [{"x_m": 1.0, "depth_m": 1.0, "amplitude_db": 0.0,
                  "contrast_db": 12.0}])
    findings = fuse_targets([a, b])
    assert len(findings) == 2
    assert all(f["supporting_scans"] == 1 for f in findings)


def test_single_scan_finding_is_not_high_confidence():
    """A response seen once is a candidate, not a confirmed anomaly."""
    a = _result("expA", (0.0, 0.0), 0.0,
                [{"x_m": 1.0, "depth_m": 1.0, "amplitude_db": 0.0,
                  "contrast_db": 8.0}], eps_u=0.0)
    f = fuse_targets([a])[0]
    assert f["supporting_scans"] == 1
    assert f["confidence"]["lateral_position"] != "high"


def test_known_medium_gives_tight_depth_interval():
    a = _result("expA", (0.0, 0.0), 0.0,
                [{"x_m": 1.0, "depth_m": 1.0, "amplitude_db": 0.0,
                  "contrast_db": 20.0}], eps=1.0, eps_u=0.0)
    b = _result("expB", (1.0, -1.0), 90.0,
                [{"x_m": 1.0, "depth_m": 1.0, "amplitude_db": 0.0,
                  "contrast_db": 20.0}], eps=1.0, eps_u=0.0)
    f = fuse_targets([a, b])[0]
    assert f["confidence"]["depth"] == "high"
    assert f["confidence"]["overall"] == "high"
    lo, hi = f["depth_interval_m"]
    assert hi - lo < 0.01, "a known medium should not widen the depth interval"


def test_depth_slice_only_reports_measured_paths():
    """FR-IMG-003 / §2.4: a slice must not fill the plane between scan lines."""
    mig = {"positions_m": [0.0, 0.5, 1.0], "depths_m": [0.5, 1.0, 1.5],
           "amplitude_db": [[-20, -3, -25], [-22, -10, -26], [-21, -8, -24]]}
    res = [{"experiment_id": "expA", "migrated": mig,
            "placement": {"origin_x_m": 0.0, "origin_y_m": 2.0,
                          "heading_deg": 0.0}}]
    out = depth_slice(res, depth_m=1.0, thickness_m=0.2)
    assert len(out["samples"]) == 3
    assert all(s["y_m"] == 2.0 for s in out["samples"])
    assert out["samples"][1]["x_m"] == 0.5
    assert "unmeasured, not empty" in out["coverage_note"]


# -- end-to-end through the runtime ----------------------------------------

def _run_scan(rt, plan):
    r = rt.scan_start("sim-pluto-0", plan)
    for x in r["positions_m"]:
        rt.scan_point(r["scan_id"], x, operator_override=True)
    rt.scan_finalize(r["scan_id"])
    return r["scan_id"]


def test_end_to_end_perpendicular_scans_in_world_coordinates(armed_runtime):
    """Milestone D end to end: two crossing scans over the same buried target
    fuse into one finding placed in site coordinates."""
    rt = armed_runtime
    # a single buried point target; each scan crosses it at a different offset
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "point", "x_m": 1.2, "depth_m": 0.9,
                               "amplitude": 0.35, "label": "buried"}],
                     medium="soil_dry", leakage_amplitude=1e-4)
    scan_a = _run_scan(rt, {"start_m": 0.0, "end_m": 2.4, "step_m": 0.2,
                            "medium": "soil_dry", "chirps": 2,
                            "max_range_m": 8.0})
    scan_b = _run_scan(rt, {"start_m": 0.0, "end_m": 2.4, "step_m": 0.2,
                            "medium": "soil_dry", "chirps": 2,
                            "max_range_m": 8.0})

    site = rt.sites.create(name="bench site", notes="two crossing lines")
    sid = site["site_id"]
    # scan A east-west through the target; scan B north-south through it
    rt.sites.register_scan(sid, scan_a, origin_x_m=0.0, origin_y_m=1.2,
                           heading_deg=0.0, label="east-west")
    rt.sites.register_scan(sid, scan_b, origin_x_m=1.2, origin_y_m=0.0,
                           heading_deg=90.0, label="north-south")

    scene = rt.site_scene(sid, tolerance_m=0.8)
    assert scene["errors"] == [], scene["errors"]
    assert len(scene["scans"]) == 2
    assert all(s["path"] for s in scene["scans"])
    assert scene["findings"], "no findings fused from two scans"

    confirmed = [f for f in scene["findings"] if f["supporting_scans"] == 2]
    assert confirmed, ("expected a finding supported by both scans, got "
                       f"{[(f['site_x_m'], f['site_y_m'], f['supporting_scans']) for f in scene['findings']]}")
    f = confirmed[0]
    assert abs(f["site_x_m"] - 1.2) <= 0.5
    assert abs(f["site_y_m"] - 1.2) <= 0.5
    assert len(f["evidence"]) >= 2
    assert {e["experiment_id"] for e in f["evidence"]} == {scan_a, scan_b}


def test_scene_reports_unfinalized_scan_instead_of_crashing(armed_runtime):
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0", preset="scan")
    started = rt.scan_start("sim-pluto-0", {"start_m": 0.0, "end_m": 0.4,
                                            "step_m": 0.2, "chirps": 2})
    site = rt.sites.create(name="partial")
    rt.sites.register_scan(site["site_id"], started["scan_id"])
    scene = rt.site_scene(site["site_id"])
    assert scene["findings"] == []
    assert scene["errors"] and "B-scan" in scene["errors"][0]["error"]


def test_site_report_states_limits_and_provenance(armed_runtime):
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "point", "x_m": 1.0, "depth_m": 0.8,
                               "amplitude": 0.35}],
                     medium="soil_dry", leakage_amplitude=1e-4)
    scan = _run_scan(rt, {"start_m": 0.0, "end_m": 2.0, "step_m": 0.2,
                          "medium": "soil_dry", "chirps": 2,
                          "max_range_m": 8.0})
    site = rt.sites.create(name="report site")
    rt.sites.register_scan(site["site_id"], scan, label="line 1")

    out = rt.site_report(site["site_id"])
    md = out["markdown"]
    assert "# Site report — report site" in md
    assert "Alternative explanations and limitations" in md
    assert "Recommended next experiment" in md
    assert scan in md                      # provenance names the experiment
    assert "no material or object class" in md.lower()
    # a single-scan finding must prompt a crossing scan, not a conclusion
    if out["findings"]:
        assert "perpendicular" in md


def test_registration_is_idempotent(runtime):
    site = runtime.sites.create(name="s")
    sid = site["site_id"]
    runtime.sites.register_scan(sid, "exp1", origin_x_m=1.0)
    s = runtime.sites.register_scan(sid, "exp1", origin_x_m=2.0)
    assert len(s["scans"]) == 1
    assert s["scans"][0]["origin_x_m"] == 2.0
    s = runtime.sites.unregister_scan(sid, "exp1")
    assert s["scans"] == []


def test_mean_trace_removal_exposes_target_under_direct_wave():
    """A laterally-invariant direct wave must not dominate the migrated
    image; removing the mean trace should leave the buried target as the
    strongest focused response."""
    positions = [i * 0.1 for i in range(33)]
    ranges = [i * 0.05 for i in range(120)]
    hyperbola = _synthetic_bscan(1.6, 1.2, positions, ranges, amplitude_db=-6.0)
    # add a strong direct wave: same shallow response in every trace
    direct = [-3.0 - 40 * ((r - 0.15) / 0.25) ** 2 for r in ranges]
    bscan = [[max(h, d) for h, d in zip(col, direct)] for col in hyperbola]

    raw = migrate_bscan(positions, ranges, bscan, depth_step_m=0.05,
                        max_depth_m=4.0, remove_mean_trace=False)
    cleaned = migrate_bscan(positions, ranges, bscan, depth_step_m=0.05,
                            max_depth_m=4.0, remove_mean_trace=True)
    assert cleaned["mean_trace_removed"] is True

    def strongest(mig):
        db = mig["amplitude_db"]
        best = max(((db[i][j], i, j) for i in range(len(db))
                    for j in range(len(db[0]))))
        return mig["positions_m"][best[1]], mig["depths_m"][best[2]]

    # without removal the brightest cell sits in the shallow direct-wave band
    _, raw_depth = strongest(raw)
    assert raw_depth < 0.6, f"expected direct wave to dominate, got {raw_depth}"
    # with removal the target wins
    cx, cdepth = strongest(cleaned)
    assert abs(cx - 1.6) <= 0.2, f"lateral {cx}"
    assert abs(cdepth - 1.2) <= 0.25, f"depth {cdepth}"


def test_migration_discards_unsupported_depths():
    """Cells whose slant ranges were never measured must be dropped, not
    filled with zeros that later read as structure."""
    positions = [i * 0.1 for i in range(21)]
    ranges = [i * 0.05 for i in range(41)]        # range axis stops at 2.0 m
    bscan = _synthetic_bscan(1.0, 0.8, positions, ranges)
    mig = migrate_bscan(positions, ranges, bscan, depth_step_m=0.05,
                        max_depth_m=6.0)          # ask well beyond the data
    assert mig["max_supported_depth_m"] <= 2.0, \
        "migration reported depths deeper than any measurement supports"
    assert max(mig["depths_m"]) <= 2.0


def test_migration_warns_when_depth_cannot_be_focused():
    """A shallow target seen through a coarse range cell focuses laterally
    but not in depth; the operator must be told, not left to guess."""
    positions = [i * 0.2 for i in range(13)]        # 2.4 m of aperture
    coarse = [i * 1.34 for i in range(8)]           # 1.34 m range bins (soil)
    bscan = _synthetic_bscan(1.2, 0.8, positions, coarse, width=1.34)
    mig = migrate_bscan(positions, coarse, bscan, depth_step_m=0.1,
                        max_depth_m=4.0)
    assert mig["depth_focus_warning"], "expected a depth-focus warning"
    assert "depth is not well focused" in mig["depth_focus_warning"]

    # a fine range cell over the same geometry should not warn
    fine = [i * 0.05 for i in range(120)]
    ok = migrate_bscan(positions, fine,
                       _synthetic_bscan(1.2, 0.8, positions, fine),
                       depth_step_m=0.05, max_depth_m=3.0)
    assert ok["depth_focus_warning"] == ""
