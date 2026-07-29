"""Grounded natural-language filtering (FR-AI-004).

Intent is resolved deterministically. That is a deliberate choice: the spec
requires the platform to work with no network (§12 offline operation) and
names AI overinterpretation as a headline risk (§17). A parser that
occasionally answers "I did not understand that" is far safer here than a
generator that always produces fluent text, because every answer this returns
is assembled from stored measurements and carries their provenance.

The parser is intentionally transparent about its limits: unmatched questions
return `understood=False` with the forms it does support, rather than a
plausible-sounding guess.
"""

from __future__ import annotations

import re

from .analysis import (assess_experiment, compare_experiments, explain_finding,
                       recommend_next, summarize_experiment)
from .facts import Fact, answer, experiment_evidence, site_evidence

SUPPORTED = [
    "why is finding 2 highlighted",
    "show anomalies between 1 and 3 meters deep",
    "which findings are confirmed by more than one scan",
    "what is wrong with this experiment",
    "summarize this experiment",
    "what should I measure next",
    "compare this experiment with <experiment id>",
]

_NUM = r"(-?\d+(?:\.\d+)?)"


def _depth_range(q: str):
    m = re.search(rf"between\s+{_NUM}\s*(?:m|meters?)?\s*(?:and|to|-)\s*{_NUM}",
                  q)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(rf"(?:deeper than|below|more than)\s+{_NUM}", q)
    if m:
        return float(m.group(1)), float("inf")
    m = re.search(rf"(?:shallower than|above|less than)\s+{_NUM}", q)
    if m:
        return 0.0, float(m.group(1))
    return None


def ask(question: str, *, store=None, scene: dict | None = None,
        experiment_id: str = "") -> dict:
    """Answer a grounded question about the loaded context."""
    q = (question or "").strip().lower()
    if not q:
        return answer([], question, understood=False,
                      note="Ask a question about the selected site or "
                           "experiment. Supported forms: "
                           + "; ".join(SUPPORTED))

    # -- why is finding N highlighted (Milestone E) -------------------------
    m = re.search(r"(?:why|explain).*?(?:finding|anomaly)\s*#?\s*(\d+)", q)
    if m and scene:
        idx = int(m.group(1)) - 1
        findings = scene["findings"]
        if not 0 <= idx < len(findings):
            return answer([], question, understood=True,
                          note=f"This site has {len(findings)} finding(s); "
                               f"there is no #{idx + 1}.")
        return answer(explain_finding(scene["site"], findings[idx], idx),
                      question)

    if re.search(r"(?:why|explain)", q) and scene and scene["findings"]:
        return answer(explain_finding(scene["site"], scene["findings"][0], 0),
                      question,
                      note="Interpreted as a question about finding #1.")

    # -- depth / confidence filters ----------------------------------------
    if scene and re.search(r"anomal|finding|target", q):
        return _filter_findings(q, question, scene)

    # -- quality -----------------------------------------------------------
    if store and experiment_id and re.search(
            r"wrong|quality|problem|issue|trust|wrong with|bad", q):
        return answer(assess_experiment(store, experiment_id), question)

    # -- summary -----------------------------------------------------------
    if store and experiment_id and re.search(r"summar|describe|what happened|"
                                             r"overview|setup", q):
        return answer(summarize_experiment(store, experiment_id)
                      + assess_experiment(store, experiment_id), question)

    # -- next measurement --------------------------------------------------
    if re.search(r"next|recommend|should i|what now|improve", q):
        if scene:
            return answer(recommend_next(scene), question)
        return answer([], question, understood=True,
                      note="Select a site first — recommendations are derived "
                           "from its registered scans and findings.")

    # -- comparison --------------------------------------------------------
    m = re.search(r"compare.*?([0-9]{8}-[0-9]{6}-[0-9a-f]{6})", q)
    if m and store and experiment_id:
        return answer(compare_experiments(store, experiment_id, m.group(1)),
                      question)

    return answer([], question, understood=False,
                  note="I did not understand that, and I will not guess. "
                       "I only answer from stored measurements. Try: "
                       + "; ".join(SUPPORTED))


def _filter_findings(q: str, question: str, scene: dict) -> dict:
    findings = list(enumerate(scene["findings"]))
    sid = scene["site"]["site_id"]
    criteria = []

    rng = _depth_range(q)
    if rng:
        lo, hi = rng
        # a finding qualifies if its depth *interval* overlaps the request —
        # excluding one whose interval straddles the boundary would hide a
        # candidate on a technicality
        findings = [(i, f) for i, f in findings
                    if f["depth_interval_m"][1] >= lo
                    and f["depth_interval_m"][0] <= hi]
        criteria.append(f"depth interval overlapping {lo:g}–"
                        f"{'∞' if hi == float('inf') else format(hi, 'g')} m")

    if re.search(r"persist|confirm|more than one|multiple|two or more|"
                 r"both scans|repeat", q):
        findings = [(i, f) for i, f in findings if f["supporting_scans"] >= 2]
        criteria.append("supported by two or more scans")

    if re.search(r"high confidence|reliable|strong", q):
        findings = [(i, f) for i, f in findings
                    if f["confidence"]["lateral_position"] == "high"]
        criteria.append("high lateral-position confidence")

    crit = " and ".join(criteria) if criteria else "no filter"
    if not findings:
        return answer([Fact(
            f"No finding at this site matches: {crit}. That is a statement "
            "about what has been measured here, not about what is present.",
            "unknown", [site_evidence(sid)], {"criteria": criteria})], question)

    facts = [Fact(
        f"{len(findings)} finding(s) match: {crit}.",
        "calculation", [site_evidence(sid)],
        {"matched": len(findings), "criteria": criteria})]
    for i, f in findings:
        lo, hi = f["depth_interval_m"]
        facts.append(Fact(
            f"Finding #{i + 1} at ({f['site_x_m']:.2f}, {f['site_y_m']:.2f}) m, "
            f"depth {f['depth_m']:.2f} m (interval {lo:.2f}–{hi:.2f} m), "
            f"supported by {f['supporting_scans']} scan(s), "
            f"{f['confidence']['overall']} overall confidence.",
            "calculation",
            [experiment_evidence(e["experiment_id"], "derived/bscan.json",
                                 f"{e['scan_x_m']:.2f} m along scan")
             for e in f["evidence"]],
            {"index": i + 1, "depth_m": f["depth_m"],
             "supporting_scans": f["supporting_scans"]}))
    return answer(facts, question)
