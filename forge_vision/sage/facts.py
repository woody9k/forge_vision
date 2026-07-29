"""The grounded fact — the only thing SAGE is allowed to say.

Two spec requirements are MUST, and both are enforced here structurally rather
than by convention, because an assistant that *usually* cites evidence is the
failure mode §17 warns about ("plausible but unsupported claims may mislead
users"):

* FR-AI-006 — every factual statement about a run links to one or more
  measurements or derived artifacts. A Fact that claims something about the
  data and carries no evidence raises `UngroundedStatement` at construction.
* FR-AI-007 — every statement is labelled observation, calculation,
  inference, hypothesis, or unknown.

`unknown` is the one kind allowed to stand without evidence: saying "this is
not determined by the measurements" is precisely a statement about the absence
of evidence, and the platform must be able to say it (FR-INT-008).
"""

from __future__ import annotations

from dataclasses import dataclass, field

EPISTEMIC_KINDS = ("observation", "calculation", "inference", "hypothesis",
                   "unknown")

#: how each label should be read by an operator, surfaced in the UI
KIND_MEANING = {
    "observation": "measured directly",
    "calculation": "derived from measurements by a documented method",
    "inference": "concluded from the measurements under stated assumptions",
    "hypothesis": "a candidate explanation, not established",
    "unknown": "not determined by these measurements",
}


class UngroundedStatement(ValueError):
    """Raised when a factual statement is created without evidence."""


@dataclass
class Fact:
    statement: str
    kind: str                                  # one of EPISTEMIC_KINDS
    evidence: list = field(default_factory=list)
    values: dict = field(default_factory=dict)
    severity: str = "info"                     # info | warn | critical
    action: str = ""                           # what the operator could do

    def __post_init__(self):
        if self.kind not in EPISTEMIC_KINDS:
            raise ValueError(f"unknown epistemic kind: {self.kind}; "
                             f"expected one of {EPISTEMIC_KINDS}")
        if self.kind != "unknown" and not self.evidence:
            raise UngroundedStatement(
                f"refusing to assert without evidence ({self.kind}): "
                f"{self.statement!r}")

    def to_dict(self) -> dict:
        return {
            "statement": self.statement,
            "kind": self.kind,
            "kind_meaning": KIND_MEANING[self.kind],
            "evidence": self.evidence,
            "values": self.values,
            "severity": self.severity,
            "action": self.action,
        }


def experiment_evidence(experiment_id: str, artifact: str = "",
                        locator: str = "", detail: str = "") -> dict:
    """A link back to a specific stored artifact (FR-AI-006, UX-WLD-004)."""
    return {"type": "experiment", "experiment_id": experiment_id,
            "artifact": artifact, "locator": locator, "detail": detail}


def site_evidence(site_id: str, detail: str = "") -> dict:
    return {"type": "site", "site_id": site_id, "detail": detail}


def answer(facts: list, question: str = "", understood: bool = True,
           note: str = "") -> dict:
    """Package facts as an assistant answer."""
    return {
        "question": question,
        "understood": understood,
        "note": note,
        "facts": [f.to_dict() for f in facts],
        "counts": {k: sum(1 for f in facts if f.kind == k)
                   for k in EPISTEMIC_KINDS if any(f.kind == k for f in facts)},
        "evidence_count": sum(len(f.evidence) for f in facts),
    }
