"""Grounded analyses: quality, explanation, comparison, recommendation.

Every function here turns stored measurements into `Fact` objects. Nothing
invents a number; each statement carries the artifact it came from.
"""

from __future__ import annotations

import math

from .facts import Fact, experiment_evidence, site_evidence


# -- FR-AI-002: quality assistant ------------------------------------------
def assess_experiment(store, exp_id: str) -> list[Fact]:
    """Identify saturation, weak SNR, calibration mismatch, position
    inconsistency, and incomplete metadata in a stored experiment."""
    manifest = store.load(exp_id)
    ident = manifest["identity"]
    ev = lambda a="", loc="", d="": experiment_evidence(exp_id, a, loc, d)  # noqa: E731
    facts: list[Fact] = []

    segments = manifest.get("segments", [])
    facts.append(Fact(
        f"Experiment '{ident['name']}' ({ident['kind']}) holds "
        f"{len(segments)} raw capture segment(s) and finalized as "
        f"'{ident['status']}'.",
        "observation", [ev("manifest.json")],
        {"segments": len(segments), "status": ident["status"],
         "kind": ident["kind"]}))

    clipped = [s["segment_id"] for s in segments if s.get("clipped")]
    if clipped:
        facts.append(Fact(
            f"{len(clipped)} segment(s) hit the converter's full scale. "
            "Saturated samples are not proportional to the field, so any "
            "amplitude or range derived from them is unreliable.",
            "observation",
            [ev("raw/" + s + ".json", s, "clipped=true") for s in clipped[:5]],
            {"clipped_segments": clipped}, severity="critical",
            action="Reduce RX gain (or increase attenuation) and re-run."))

    lossy = [(s["segment_id"], len(s.get("loss_events", [])))
             for s in segments if s.get("loss_events")]
    if lossy:
        total = sum(n for _, n in lossy)
        facts.append(Fact(
            f"{total} sample-loss event(s) were recorded across "
            f"{len(lossy)} segment(s). The sample stream has gaps, so phase "
            "continuity across those points is broken.",
            "observation",
            [ev("raw/" + sid + ".json", sid, f"{n} loss event(s)")
             for sid, n in lossy[:5]],
            {"loss_events": total}, severity="warn",
            action="Reduce sample rate or capture length, then repeat."))

    cal = manifest.get("calibration", {})
    if cal.get("cable_delay_s", 0.0) == 0.0 and ident["kind"] in ("range", "scan"):
        facts.append(Fact(
            "Cable delay is not calibrated, so every reported range includes "
            "the propagation time of the cabling as if it were distance to a "
            "target.",
            "inference", [ev("manifest.json", "calibration.cable_delay_s")],
            {"cable_delay_s": 0.0}, severity="warn",
            action="Measure the cable delay and enter it in Range Lab."))

    status = cal.get("status_at_run", {})
    for warning in status.get("warnings", []):
        facts.append(Fact(
            f"Calibration warning recorded at run time: {warning}",
            "observation", [ev("manifest.json", "calibration.status_at_run")],
            {}, severity="warn"))

    model = cal.get("propagation_model", {})
    if model.get("epsilon_r_uncertainty", 0) > 0:
        facts.append(Fact(
            f"Depth was derived assuming relative permittivity "
            f"{model['epsilon_r']} ± {model['epsilon_r_uncertainty']} "
            f"({model.get('name', 'medium')}). That uncertainty maps directly "
            "onto depth and cannot be reduced by more averaging.",
            "inference", [ev("manifest.json", "calibration.propagation_model")],
            model, severity="warn",
            action="Calibrate velocity against a target at known depth."))

    missing = [k for k in ("hardware", "rf_config") if not manifest.get(k)]
    if missing:
        facts.append(Fact(
            f"Metadata is incomplete: {', '.join(missing)} not recorded. The "
            "run cannot be fully reproduced from its own record.",
            "observation", [ev("manifest.json")], {"missing": missing},
            severity="warn"))

    facts.extend(_derived_quality(store, exp_id, ident, ev))

    if not any(f.severity != "info" for f in facts):
        facts.append(Fact(
            "No saturation, sample loss, calibration mismatch, or missing "
            "metadata was found in this experiment's record.",
            "observation", [ev("manifest.json")], {}))
    return facts


def _derived_quality(store, exp_id: str, ident: dict, ev) -> list[Fact]:
    facts: list[Fact] = []
    try:
        prof = store.load_derived(exp_id, "range_profile")["product"]
    except (FileNotFoundError, KeyError):
        prof = None
    if prof:
        q = prof.get("quality", {})
        snr = q.get("profile_peak_snr_db")
        if snr is not None:
            weak = snr < 10
            facts.append(Fact(
                f"Strongest return sits {snr:.1f} dB above the profile noise "
                f"floor." + (" Below about 10 dB a peak is not reliably "
                             "distinguishable from noise." if weak else ""),
                "calculation", [ev("derived/range_profile.json", "quality")],
                {"peak_snr_db": snr}, severity="warn" if weak else "info",
                action="Increase averaging (more chirps), improve antenna "
                       "isolation, or subtract a background." if weak else ""))
        peaks = prof.get("peaks", [])
        leak = [p for p in peaks if p.get("suspected_leakage")]
        if leak:
            facts.append(Fact(
                f"{len(leak)} detected peak(s) lie within a few nanoseconds of "
                "zero delay and are most likely direct transmit-to-receive "
                "coupling rather than a reflector.",
                "inference", [ev("derived/range_profile.json", "peaks")],
                {"leakage_peaks": len(leak)}, severity="warn",
                action="Capture a background and re-run with subtraction, or "
                       "increase antenna separation."))

    if ident["kind"] == "scan":
        try:
            b = store.load_derived(exp_id, "bscan")["product"]
        except (FileNotFoundError, KeyError):
            return facts
        st = b.get("status", {})
        pending = st.get("pending", [])
        if pending:
            facts.append(Fact(
                f"{len(pending)} of {st.get('total_points')} scan positions "
                "were never captured, so the image has gaps.",
                "observation", [ev("derived/bscan.json", "status.pending")],
                {"pending": len(pending)}, severity="warn",
                action="Resume the scan and capture the remaining positions."))
        low = st.get("low_quality", [])
        if low:
            facts.append(Fact(
                f"{len(low)} scan position(s) were flagged low quality "
                "(weak SNR or clipping) but retained in the image.",
                "observation", [ev("derived/bscan.json", "status.low_quality")],
                {"low_quality_positions": low}, severity="warn"))
        unc = b.get("quality", {}).get("position_uncertainty_m", [])
        spread = [u for u in unc if u]
        if spread and max(spread) > 0.1:
            facts.append(Fact(
                f"Position uncertainty reaches {max(spread):.2f} m, which is "
                "comparable to typical target spacing and will blur focusing.",
                "observation", [ev("derived/bscan.json", "quality")],
                {"max_position_uncertainty_m": max(spread)}, severity="warn",
                action="Use an encoder or tighter position control."))
    return facts


# -- FR-AI-001: experiment summary -----------------------------------------
def summarize_experiment(store, exp_id: str) -> list[Fact]:
    manifest = store.load(exp_id)
    ident = manifest["identity"]
    rf = manifest.get("rf_config", {})
    ev = lambda a="", loc="": experiment_evidence(exp_id, a, loc)  # noqa: E731
    facts = []

    if rf:
        facts.append(Fact(
            f"Configured at {rf.get('center_frequency_hz', 0)/1e6:.1f} MHz, "
            f"{rf.get('sample_rate_hz', 0)/1e6:.2f} MSPS, RX gain "
            f"{rf.get('rx_gain_db')} dB, TX gain {rf.get('tx_gain_db')} dB.",
            "observation", [ev("manifest.json", "rf_config")], rf))

    try:
        prof = store.load_derived(exp_id, "range_profile")["product"]
        rp = prof.get("range_profile", {})
        if rp:
            facts.append(Fact(
                f"Range resolution is {rp['resolution_m']:.2f} m, set by the "
                f"swept bandwidth and the assumed propagation velocity "
                f"({rp['velocity_m_per_s']/1e8:.2f}e8 m/s). Two reflectors "
                "closer than that appear as one.",
                "calculation", [ev("derived/range_profile.json",
                                   "range_profile.resolution_m")],
                {"resolution_m": rp["resolution_m"]}))
        real = [p for p in prof.get("peaks", [])
                if not p.get("suspected_leakage")]
        if real:
            best = max(real, key=lambda p: p["snr_db"])
            facts.append(Fact(
                f"{len(real)} candidate reflector(s) detected; the strongest "
                f"is at {best['range_m']:.2f} m "
                f"(interval {best['range_interval_m'][0]:.2f}–"
                f"{best['range_interval_m'][1]:.2f} m) at "
                f"{best['snr_db']:.1f} dB SNR.",
                "calculation", [ev("derived/range_profile.json", "peaks")],
                {"peaks": len(real), "strongest": best}))
        else:
            facts.append(Fact(
                "No reflector was detected above the threshold. This bounds "
                "the sensitivity of this run; it is not evidence that nothing "
                "is present.",
                "unknown", [ev("derived/range_profile.json", "peaks")]))
    except (FileNotFoundError, KeyError):
        pass

    facts.append(Fact(
        "Object class and material are not determined by these measurements.",
        "unknown", [], {}))
    return facts


# -- Milestone E: why was this anomaly highlighted? -------------------------
def explain_finding(site: dict, finding: dict, index: int) -> list[Fact]:
    sid = site["site_id"]
    facts = [Fact(
        f"Finding #{index + 1} sits at ({finding['site_x_m']:.2f}, "
        f"{finding['site_y_m']:.2f}) m in the site frame, "
        f"{finding['depth_m']:.2f} m deep.",
        "calculation", [site_evidence(sid, site["coordinate_system"])],
        {"x_m": finding["site_x_m"], "y_m": finding["site_y_m"],
         "depth_m": finding["depth_m"]})]

    ev_links = [experiment_evidence(
        e["experiment_id"], "derived/bscan.json",
        f"{e['scan_x_m']:.2f} m along scan",
        f"focused response at {e['depth_m']:.2f} m depth, "
        f"{e['contrast_db']:.1f} dB contrast") for e in finding["evidence"]]

    facts.append(Fact(
        f"It was highlighted because {finding['observations']} focused "
        f"response(s) across {finding['supporting_scans']} scan(s) fell "
        f"within the clustering tolerance of the same place. Strongest "
        f"contrast {finding['max_contrast_db']:.1f} dB above the migrated "
        "image median.",
        "observation", ev_links,
        {"observations": finding["observations"],
         "supporting_scans": finding["supporting_scans"],
         "max_contrast_db": finding["max_contrast_db"]}))

    if finding["supporting_scans"] >= 2:
        facts.append(Fact(
            f"Independent scans agree to within {finding['position_spread_m']:.2f} m. "
            "Agreement from separate geometries is what distinguishes a real "
            "reflector from a multipath artefact, which generally moves when "
            "the geometry changes.",
            "inference", ev_links,
            {"position_spread_m": finding["position_spread_m"]}))
    else:
        facts.append(Fact(
            "Only one scan supports this response, so a multipath artefact or "
            "a surface feature has not been ruled out.",
            "hypothesis", ev_links, {"supporting_scans": 1}, severity="warn",
            action="Run a crossing scan through this position."))

    lo, hi = finding["depth_interval_m"]
    facts.append(Fact(
        f"Depth is constrained to {lo:.2f}–{hi:.2f} m. Lateral position is "
        f"rated {finding['confidence']['lateral_position']} confidence and "
        f"depth {finding['confidence']['depth']}, because depth inherits the "
        "uncertainty of the assumed permittivity while lateral position does "
        "not.",
        "calculation", ev_links,
        {"depth_interval_m": finding["depth_interval_m"],
         "confidence": finding["confidence"]}))

    facts.append(Fact(
        f"What this is — material, object class, whether it is a void, pipe, "
        f"rock, or anything else — is not determined. The platform classifies "
        f"it only as '{finding['classification']}'.",
        "unknown", [], {"classification": finding["classification"]}))
    return facts


# -- FR-AI-003: comparative analyst ----------------------------------------
def compare_experiments(store, a_id: str, b_id: str) -> list[Fact]:
    a, b = store.load(a_id), store.load(b_id)
    ev = [experiment_evidence(a_id, "manifest.json"),
          experiment_evidence(b_id, "manifest.json")]
    facts = [Fact(
        f"Comparing '{a['identity']['name']}' with '{b['identity']['name']}'.",
        "observation", ev,
        {"a": a["identity"]["name"], "b": b["identity"]["name"]})]

    diffs = []
    for key in ("center_frequency_hz", "sample_rate_hz", "rx_gain_db",
                "tx_gain_db", "rx_bandwidth_hz"):
        va, vb = a.get("rf_config", {}).get(key), b.get("rf_config", {}).get(key)
        if va != vb:
            diffs.append((key, va, vb))
    if diffs:
        facts.append(Fact(
            "Radio configuration differs: "
            + "; ".join(f"{k} {va} vs {vb}" for k, va, vb in diffs)
            + ". Differences in the results may follow from the configuration "
              "rather than from the scene.",
            "observation", ev, {"differences": diffs}, severity="warn"))
    else:
        facts.append(Fact(
            "Radio configuration is identical across the two runs, so "
            "differences in the results are not explained by the settings.",
            "observation", ev, {}))

    ma = a.get("calibration", {}).get("propagation_model", {}).get("name")
    mb = b.get("calibration", {}).get("propagation_model", {}).get("name")
    if ma != mb:
        facts.append(Fact(
            f"Different propagation models were assumed ({ma} vs {mb}), so "
            "reported depths are not directly comparable.",
            "inference", ev, {"medium_a": ma, "medium_b": mb}, severity="warn"))

    pa, pb = _peaks(store, a_id), _peaks(store, b_id)
    if pa is not None and pb is not None:
        facts.extend(_compare_peaks(a_id, b_id, pa, pb, ev))
    return facts


def _peaks(store, exp_id):
    try:
        prof = store.load_derived(exp_id, "range_profile")["product"]
    except (FileNotFoundError, KeyError):
        return None
    return [p for p in prof.get("peaks", []) if not p.get("suspected_leakage")]


def _compare_peaks(a_id, b_id, pa, pb, ev) -> list[Fact]:
    facts = []
    ev_peaks = [experiment_evidence(a_id, "derived/range_profile.json", "peaks"),
                experiment_evidence(b_id, "derived/range_profile.json", "peaks")]
    matched, only_a = [], []
    for p in pa:
        near = [q for q in pb if abs(q["range_m"] - p["range_m"]) <= 1.0]
        (matched if near else only_a).append(p)
    only_b = [q for q in pb
              if not any(abs(q["range_m"] - p["range_m"]) <= 1.0 for p in pa)]

    if matched:
        facts.append(Fact(
            f"{len(matched)} reflector(s) appear at the same range in both "
            "runs, within 1 m.",
            "calculation", ev_peaks,
            {"matched_ranges": [round(p["range_m"], 2) for p in matched]}))
    for label, only, other in (("first", only_a, b_id), ("second", only_b, a_id)):
        if only:
            facts.append(Fact(
                f"{len(only)} reflector(s) appear only in the {label} run, at "
                + ", ".join(f"{p['range_m']:.2f} m" for p in only[:4])
                + ". Something changed in the scene, the geometry, or the "
                  "sensitivity between runs.",
                "observation", ev_peaks,
                {"unmatched": [round(p["range_m"], 2) for p in only]},
                severity="warn"))
    if not matched and not only_a and not only_b:
        facts.append(Fact(
            "Neither run detected a reflector above threshold, so the two "
            "cannot be distinguished by their findings.",
            "unknown", []))
    return facts


# -- FR-AI-005: next measurement -------------------------------------------
def recommend_next(scene: dict) -> list[Fact]:
    site = scene["site"]
    sid = site["site_id"]
    findings = scene["findings"]
    facts: list[Fact] = []

    if not scene["scans"]:
        return [Fact(
            "No scans are registered to this site, so there is nothing to "
            "reason about yet.",
            "unknown", [], action="Register a finalized scan in World View.")]

    unconfirmed = [f for f in findings if f["supporting_scans"] < 2]
    if unconfirmed:
        f = max(unconfirmed, key=lambda x: x["max_contrast_db"])
        heading = _crossing_heading(scene, f)
        half = 1.2
        x, y = f["site_x_m"], f["site_y_m"]
        dx, dy = math.cos(math.radians(heading)), math.sin(math.radians(heading))
        facts.append(Fact(
            f"The strongest unconfirmed response is at ({x:.2f}, {y:.2f}) m. "
            f"Scan along heading {heading:.0f}° from "
            f"({x - dx*half:.2f}, {y - dy*half:.2f}) m to "
            f"({x + dx*half:.2f}, {y + dy*half:.2f}) m, stepping 0.1 m. "
            "Crossing the response from a different direction is the single "
            "measurement that most reduces uncertainty, because artefacts "
            "generally move with geometry and real reflectors do not.",
            "inference",
            [experiment_evidence(e["experiment_id"], "derived/bscan.json",
                                 f"{e['scan_x_m']:.2f} m along scan")
             for e in f["evidence"]] + [site_evidence(sid)],
            {"heading_deg": heading, "start": [x - dx*half, y - dy*half],
             "end": [x + dx*half, y + dy*half], "step_m": 0.1},
            action="Run this scan, then register it to the site."))

    uncertain_depth = [f for f in findings if f["confidence"]["depth"] != "high"]
    if uncertain_depth:
        f = uncertain_depth[0]
        lo, hi = f["depth_interval_m"]
        facts.append(Fact(
            f"Depth for finding at ({f['site_x_m']:.2f}, {f['site_y_m']:.2f}) m "
            f"spans {lo:.2f}–{hi:.2f} m — a {hi - lo:.2f} m window that comes "
            "from the assumed permittivity, not from noise. More averaging "
            "will not narrow it; a velocity calibration will.",
            "inference",
            [experiment_evidence(e["experiment_id"], "derived/bscan.json")
             for e in f["evidence"]],
            {"depth_interval_m": [lo, hi]},
            action="Bury or place a reflector at a measured depth, scan it, "
                   "and solve for the true velocity."))

    for scan in scene["scans"]:
        mig = (scene.get("migrated") or {}).get(scan["experiment_id"], {})
        if mig.get("depth_focus_warning"):
            facts.append(Fact(
                f"Scan '{scan['placement']['label']}' cannot focus depth: "
                f"{mig['depth_focus_warning']}",
                "calculation",
                [experiment_evidence(scan["experiment_id"], "derived/bscan.json")],
                {"range_bin_m": mig.get("range_bin_m"),
                 "shallow_curvature_m": mig.get("shallow_curvature_m")},
                severity="warn",
                action="Extend the scan line, or use a wider sweep for a "
                       "finer range cell."))
            break

    if not facts:
        facts.append(Fact(
            "Every finding is supported by two or more scans with a "
            "well-constrained depth. The next useful step is coverage: scan "
            "ground this site has not sampled.",
            "inference", [site_evidence(sid)],
            {"findings": len(findings)},
            action="Add scan lines over unsampled parts of the site."))
    return facts


def _crossing_heading(scene: dict, finding: dict) -> float:
    """A heading roughly perpendicular to the scan(s) that saw the response."""
    supporting = {e["experiment_id"] for e in finding["evidence"]}
    headings = [s["placement"]["heading_deg"] for s in scene["scans"]
                if s["experiment_id"] in supporting]
    return ((headings[0] if headings else 0.0) + 90.0) % 180.0
