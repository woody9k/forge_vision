"""SAGE tests (release 0.5, §8).

The two MUST requirements get the most attention: every factual statement
carries evidence (FR-AI-006) and an epistemic label (FR-AI-007). Milestone E
is the headline behaviour: ask why an anomaly was highlighted and receive an
evidence-linked answer.
"""

import pytest

from forge_vision.sage.analysis import (assess_experiment, compare_experiments,
                                        explain_finding, recommend_next,
                                        summarize_experiment)
from forge_vision.sage.facts import (EPISTEMIC_KINDS, Fact, UngroundedStatement,
                                     experiment_evidence)
from forge_vision.sage.query import ask


# -- FR-AI-006 / FR-AI-007: the contract ------------------------------------
def test_factual_statement_without_evidence_is_refused():
    """An assistant that sometimes cites evidence is the failure mode the
    spec warns about, so grounding is enforced at construction."""
    for kind in ("observation", "calculation", "inference", "hypothesis"):
        with pytest.raises(UngroundedStatement):
            Fact("there is a pipe at 2 m", kind, evidence=[])


def test_unknown_may_stand_without_evidence():
    """Saying 'the measurements do not determine this' is a statement about
    the absence of evidence and must remain expressible (FR-INT-008)."""
    f = Fact("Material type is not determined.", "unknown", [])
    assert f.to_dict()["kind"] == "unknown"


def test_invalid_epistemic_kind_rejected():
    with pytest.raises(ValueError, match="unknown epistemic kind"):
        Fact("x", "certainly_true", [experiment_evidence("e")])


def _all_grounded(facts):
    return all(f.evidence or f.kind == "unknown" for f in facts)


def _all_labelled(facts):
    return all(f.kind in EPISTEMIC_KINDS for f in facts)


# -- fixtures ---------------------------------------------------------------
def _run_scan(rt, plan):
    r = rt.scan_start("sim-pluto-0", plan)
    for x in r["positions_m"]:
        rt.scan_point(r["scan_id"], x, operator_override=True)
    rt.scan_finalize(r["scan_id"])
    return r["scan_id"]


@pytest.fixture
def scene_runtime(armed_runtime):
    """A site with two crossing scans over one buried target."""
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "point", "x_m": 1.2, "depth_m": 0.9,
                               "amplitude": 0.35}],
                     medium="soil_dry", leakage_amplitude=1e-4)
    plan = {"start_m": 0.0, "end_m": 2.4, "step_m": 0.2, "medium": "soil_dry",
            "chirps": 2, "max_range_m": 8.0}
    a, b = _run_scan(rt, plan), _run_scan(rt, plan)
    site = rt.sites.create(name="sage site")
    sid = site["site_id"]
    rt.sites.register_scan(sid, a, origin_x_m=0.0, origin_y_m=1.2,
                           heading_deg=0.0, label="east-west")
    rt.sites.register_scan(sid, b, origin_x_m=1.2, origin_y_m=0.0,
                           heading_deg=90.0, label="north-south")
    return rt, sid, a, b


# -- Milestone E ------------------------------------------------------------
def test_milestone_e_explains_anomaly_with_evidence_links(scene_runtime):
    """Ask why an anomaly was highlighted, receive an evidence-linked answer."""
    rt, sid, a, b = scene_runtime
    out = rt.sage_explain(sid, 0)
    facts = out["facts"]
    assert facts, "no explanation produced"
    assert out["evidence_count"] >= 2

    # every factual statement links to a stored artifact
    for f in facts:
        if f["kind"] != "unknown":
            assert f["evidence"], f"ungrounded statement: {f['statement']}"
            assert f["kind"] in EPISTEMIC_KINDS

    # the evidence names the actual scans that produced the finding
    linked = {e.get("experiment_id") for f in facts for e in f["evidence"]}
    assert {a, b} <= linked

    # it explains *why*: agreement across independent scans
    text = " ".join(f["statement"] for f in facts).lower()
    assert "scan" in text and ("agree" in text or "fell within" in text)
    # and it declines to say what the object is
    assert any(f["kind"] == "unknown" for f in facts)


def test_explanation_flags_single_scan_as_hypothesis(armed_runtime):
    """One scan is a candidate, not a confirmation — and must be labelled
    hypothesis rather than inference."""
    finding = {
        "site_x_m": 1.0, "site_y_m": 2.0, "depth_m": 1.0,
        "depth_interval_m": [0.8, 1.3], "position_spread_m": 0.0,
        "supporting_scans": 1, "observations": 1, "max_contrast_db": 9.0,
        "classification": "persistent anomaly, unknown type",
        "confidence": {"lateral_position": "medium", "depth": "low",
                       "overall": "low"},
        "evidence": [{"experiment_id": "expA", "label": "l", "scan_x_m": 1.0,
                      "depth_m": 1.0, "contrast_db": 9.0}],
        "epistemic": {},
    }
    site = {"site_id": "s1", "coordinate_system": "local"}
    facts = explain_finding(site, finding, 0)
    kinds = {f.kind for f in facts}
    assert "hypothesis" in kinds
    assert any("not been ruled out" in f.statement for f in facts)
    assert _all_grounded(facts) and _all_labelled(facts)


def test_index_out_of_range_is_reported(scene_runtime):
    rt, sid, _, _ = scene_runtime
    with pytest.raises(KeyError):
        rt.sage_explain(sid, 99)


# -- FR-AI-002: quality assistant -------------------------------------------
def test_quality_assistant_flags_clipping(armed_runtime):
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 1.0,
                               "amplitude": 30.0}])
    result = rt.range_run("sim-pluto-0", use_background=False)
    facts = assess_experiment(rt.store, result["experiment_id"])
    clipping = [f for f in facts if "full scale" in f.statement]
    assert clipping, "saturation not reported"
    assert clipping[0].severity == "critical"
    assert clipping[0].action
    assert _all_grounded(facts) and _all_labelled(facts)


def test_quality_assistant_flags_uncalibrated_and_uncertain_medium(armed_runtime):
    rt = armed_runtime
    result = rt.range_run("sim-pluto-0", medium="soil_moist",
                          use_background=False)
    facts = assess_experiment(rt.store, result["experiment_id"])
    text = " ".join(f.statement for f in facts)
    assert "Cable delay is not calibrated" in text
    assert "permittivity" in text
    assert _all_grounded(facts)


def test_clean_experiment_says_so(armed_runtime):
    """When nothing is wrong, the assistant says so rather than staying
    silent — silence is indistinguishable from 'not checked'."""
    rt = armed_runtime
    result = rt.record_capture("sim-pluto-0", num_samples=16384,
                               name="clean passive capture")
    facts = assess_experiment(rt.store, result["experiment_id"])
    assert not [f for f in facts if f.severity != "info"]
    assert any("No saturation" in f.statement for f in facts)


def test_calibrated_run_has_no_critical_issues(armed_runtime):
    """A calibrated, background-subtracted run should raise nothing critical.

    It still reports the near-zero-delay return: on a monostatic radar there
    is essentially always residual coupling there, and calling it out is the
    conservative reading, not a defect."""
    rt = armed_runtime
    rt.set_cable_delay("sim-pluto-0", 1.2e-9)
    # background must be the *empty* scene, otherwise subtraction cancels the
    # very target we are about to measure
    rt.set_sim_scene("sim-pluto-0", targets=[], leakage_amplitude=1e-5)
    rt.capture_background("sim-pluto-0")
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 8.0,
                               "amplitude": 0.08}],
                     leakage_amplitude=1e-5)
    result = rt.range_run("sim-pluto-0", medium="air", use_background=True)
    facts = assess_experiment(rt.store, result["experiment_id"])
    assert not [f for f in facts if f.severity == "critical"]
    assert not any("Cable delay is not calibrated" in f.statement
                   for f in facts)
    assert not any("permittivity" in f.statement for f in facts), \
        "air has no permittivity uncertainty to warn about"


# -- FR-AI-001: summary -----------------------------------------------------
def test_summary_is_grounded_and_admits_unknowns(armed_runtime):
    rt = armed_runtime
    result = rt.range_run("sim-pluto-0", use_background=False)
    facts = summarize_experiment(rt.store, result["experiment_id"])
    assert _all_grounded(facts) and _all_labelled(facts)
    assert any(f.kind == "unknown" for f in facts), \
        "summary must state what it does not determine"


# -- FR-AI-003: comparison --------------------------------------------------
def test_comparison_attributes_differences_to_configuration(armed_runtime):
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "plate", "range_m": 8.0,
                               "amplitude": 0.08}])
    a = rt.range_run("sim-pluto-0", use_background=False)["experiment_id"]
    rt.configure("sim-pluto-0", {"rx_gain_db": 20})
    b = rt.range_run("sim-pluto-0", use_background=False)["experiment_id"]
    facts = compare_experiments(rt.store, a, b)
    text = " ".join(f.statement for f in facts)
    assert "rx_gain_db" in text
    assert "configuration rather than from the scene" in text
    assert _all_grounded(facts) and _all_labelled(facts)


def test_comparison_notes_incomparable_media(armed_runtime):
    rt = armed_runtime
    a = rt.range_run("sim-pluto-0", medium="air", use_background=False)["experiment_id"]
    b = rt.range_run("sim-pluto-0", medium="soil_dry", use_background=False)["experiment_id"]
    facts = compare_experiments(rt.store, a, b)
    assert any("not directly comparable" in f.statement for f in facts)


# -- FR-AI-005: next measurement --------------------------------------------
def test_recommendation_gives_concrete_crossing_scan(armed_runtime):
    """A recommendation must be actionable: direction and extent, not advice."""
    rt = armed_runtime
    rt.set_sim_scene("sim-pluto-0",
                     targets=[{"kind": "point", "x_m": 1.2, "depth_m": 0.9,
                               "amplitude": 0.35}],
                     medium="soil_dry", leakage_amplitude=1e-4)
    scan = _run_scan(rt, {"start_m": 0.0, "end_m": 2.4, "step_m": 0.2,
                          "medium": "soil_dry", "chirps": 2,
                          "max_range_m": 8.0})
    site = rt.sites.create(name="one line")
    rt.sites.register_scan(site["site_id"], scan, origin_x_m=0.0,
                           origin_y_m=1.2, heading_deg=0.0)
    scene = rt.site_scene(site["site_id"])
    facts = recommend_next(scene)
    crossing = [f for f in facts if "heading" in f.statement]
    assert crossing, "no crossing-scan recommendation"
    v = crossing[0].values
    assert v["heading_deg"] == 90.0, "must cross the existing 0-degree line"
    assert "start" in v and "end" in v and v["step_m"] > 0
    assert _all_grounded(facts) and _all_labelled(facts)


def test_recommendation_without_scans_says_nothing_to_reason_about(runtime):
    site = runtime.sites.create(name="empty")
    facts = recommend_next(runtime.site_scene(site["site_id"]))
    assert facts[0].kind == "unknown"


# -- FR-AI-004: grounded query ----------------------------------------------
def test_depth_filter_query(scene_runtime):
    rt, sid, _, _ = scene_runtime
    out = rt.sage_ask("show anomalies between 0.5 and 1.5 meters deep",
                      site_id=sid)
    assert out["understood"]
    assert out["facts"]
    assert "depth interval overlapping" in out["facts"][0]["statement"]


def test_depth_filter_excludes_out_of_range(scene_runtime):
    rt, sid, _, _ = scene_runtime
    out = rt.sage_ask("show anomalies between 20 and 30 meters deep",
                      site_id=sid)
    assert out["facts"][0]["kind"] == "unknown"
    assert "not about what is present" in out["facts"][0]["statement"]


def test_confirmed_only_filter(scene_runtime):
    rt, sid, _, _ = scene_runtime
    out = rt.sage_ask("which findings are confirmed by more than one scan",
                      site_id=sid)
    assert out["understood"]
    assert "two or more scans" in out["facts"][0]["statement"]


def test_unknown_question_refuses_to_guess(scene_runtime):
    rt, sid, _, _ = scene_runtime
    out = rt.sage_ask("is there gold buried here", site_id=sid)
    assert out["understood"] is False
    assert "will not guess" in out["note"]
    assert out["facts"] == []


def test_empty_question_lists_supported_forms(runtime):
    out = ask("", store=runtime.store)
    assert out["understood"] is False
    assert "why is finding" in out["note"]


def test_every_answer_is_labelled_and_grounded(scene_runtime):
    """Sweep the supported query surface; nothing may slip through
    ungrounded or unlabelled."""
    rt, sid, a, _ = scene_runtime
    questions = [
        "why is finding 1 highlighted",
        "show anomalies between 0 and 5 meters deep",
        "which findings are confirmed by more than one scan",
        "what should I measure next",
    ]
    for q in questions:
        out = rt.sage_ask(q, site_id=sid, experiment_id=a)
        for f in out["facts"]:
            assert f["kind"] in EPISTEMIC_KINDS, q
            assert f["kind_meaning"]
            if f["kind"] != "unknown":
                assert f["evidence"], f"ungrounded in {q!r}: {f['statement']}"


# -- FR-AI-008: no silent control -------------------------------------------
def test_assistant_cannot_transmit_or_change_settings(scene_runtime):
    """The assistant is read-only by construction: no question may arm the
    interlock, enable TX, or alter device configuration."""
    rt, sid, a, _ = scene_runtime
    rt.safety.disarm()
    before = rt.device("sim-pluto-0").config.to_dict()
    for q in ["enable tx", "arm the transmitter and scan",
              "set tx gain to 0 dB", "turn on the radio and transmit",
              "why is finding 1 highlighted"]:
        rt.sage_ask(q, site_id=sid, experiment_id=a)
    assert rt.safety.status()["armed"] is False
    assert rt.safety.status()["tx_active"] is False
    assert rt.device("sim-pluto-0").tx_enabled is False
    assert rt.device("sim-pluto-0").config.to_dict() == before
