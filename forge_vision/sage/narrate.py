"""Optional LLM narration over grounded facts (§8, FR-API-006).

The deterministic engine remains the source of truth. A language model may
only *rephrase* the facts SAGE already derived from stored measurements; it
may not introduce a claim, a number, or an entity. Three mechanisms enforce
that, because prompting alone does not:

1. The prompt carries only the fact list, and instructs the model to use
   nothing else.
2. Every number in the returned prose is checked against the numbers present
   in the facts. Prose containing a figure that no fact supports is marked
   ungrounded and withheld — this catches the common failure where a model
   invents a plausible depth or confidence.
3. Narration is additive. The facts are always returned and displayed; the
   prose is a convenience layer over them, never a replacement.

If the endpoint is unreachable, slow, or returns something that fails
verification, the answer degrades to the deterministic facts. Offline
operation (§12) is therefore never contingent on a model being available.

Endpoints are OpenAI-compatible (`/v1/models`, `/v1/chat/completions`), which
covers Ollama, LM Studio, vLLM, llama.cpp, LocalAI, and friends.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

SYSTEM_PROMPT = (
    "You narrate findings for Forge Vision, a subsurface RF imaging "
    "instrument used for scientific measurement.\n\n"
    "You will be given a QUESTION and a list of FACTS that the instrument "
    "derived from stored measurements. Each fact has an epistemic label:\n"
    "  observation  = measured directly\n"
    "  calculation  = derived by a documented method\n"
    "  inference    = concluded under stated assumptions\n"
    "  hypothesis   = a candidate explanation, not established\n"
    "  unknown      = not determined by these measurements\n\n"
    "Rules you must follow exactly:\n"
    "1. Use ONLY the supplied facts. Introduce no new numbers, positions, "
    "depths, confidences, materials, or object identities.\n"
    "2. Never guess what a detected object is. If a fact says something is "
    "not determined, say so plainly.\n"
    "3. Preserve uncertainty. Do not turn an inference or hypothesis into a "
    "statement of fact, and do not drop an interval or a confidence rating.\n"
    "4. Do not recommend enabling transmission or changing radio settings.\n"
    "5. Write 2-5 short sentences of plain prose. No preamble, no bullet "
    "lists, no markdown headings, no restating these rules.\n"
)


@dataclass
class LLMEndpoint:
    name: str
    base_url: str                       # e.g. http://192.168.99.173:1234/v1
    model: str = ""
    api_key: str = ""
    timeout_s: float = 60.0
    max_tokens: int = 700
    enabled: bool = False

    def to_dict(self, redact: bool = True) -> dict:
        d = asdict(self)
        if redact and d["api_key"]:
            d["api_key"] = "***"
        return d


def _post(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def health(endpoint: LLMEndpoint) -> dict:
    """Reachability and available models, without generating anything."""
    url = endpoint.base_url.rstrip("/") + "/models"
    started = time.time()
    try:
        headers = {}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=min(endpoint.timeout_s, 15)) as r:
            data = json.load(r)
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return {"reachable": True, "models": models,
                "latency_s": round(time.time() - started, 2), "error": ""}
    except Exception as exc:  # noqa: BLE001 - an absent endpoint is normal
        return {"reachable": False, "models": [],
                "latency_s": round(time.time() - started, 2), "error": str(exc)}


# -- groundedness verification ---------------------------------------------
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# figures that carry no claim about the measurements
_INNOCUOUS = {0.0, 1.0, 2.0, 3.0}


def _numbers_in(value) -> set:
    """Every number reachable in a fact, from its prose and its values."""
    found: set = set()
    if isinstance(value, str):
        found.update(float(m) for m in _NUM_RE.findall(value))
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        found.add(float(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= _numbers_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            found |= _numbers_in(v)
    return found


def verify_narration(text: str, facts: list) -> dict:
    """Check that the prose introduces no figure the facts do not support.

    A model that invents "2.4 m deep" where the instrument measured 0.78 m is
    the exact failure §17 warns about, and it is detectable: the number simply
    is not in the evidence.
    """
    supported: set = set()
    for f in facts:
        supported |= _numbers_in(f.get("statement", ""))
        supported |= _numbers_in(f.get("values", {}))

    # allow rounding of a supported figure (0.781 -> 0.78), and plain counts
    def is_supported(n: float) -> bool:
        if n in _INNOCUOUS:
            return True
        for s in supported:
            if abs(n - s) <= max(0.011, abs(s) * 0.02):
                return True
            if round(s, 1) == n or round(s, 0) == n:
                return True
        return False

    ungrounded = sorted({n for n in (float(m) for m in _NUM_RE.findall(text))
                         if not is_supported(n)})
    return {"grounded": not ungrounded, "ungrounded_numbers": ungrounded,
            "checked_numbers": len(_NUM_RE.findall(text))}


def _fact_block(facts: list) -> str:
    lines = []
    for i, f in enumerate(facts, 1):
        lines.append(f"{i}. [{f['kind']}] {f['statement']}")
        if f.get("action"):
            lines.append(f"   suggested action: {f['action']}")
    return "\n".join(lines)


def narrate(answer: dict, endpoint: LLMEndpoint) -> dict:
    """Add a `narration` block to a SAGE answer. Never raises."""
    facts = answer.get("facts", [])
    out = dict(answer)
    if not endpoint or not endpoint.enabled or not facts:
        return out
    if not endpoint.model:
        out["narration"] = {"available": False,
                            "error": "no model selected for this endpoint"}
        return out

    prompt = (f"QUESTION: {answer.get('question') or 'Summarise these findings.'}"
              f"\n\nFACTS:\n{_fact_block(facts)}\n\n"
              "Write the plain-prose summary now, following the rules.")
    started = time.time()
    try:
        data = _post(endpoint.base_url.rstrip("/") + "/chat/completions",
                     {"model": endpoint.model, "temperature": 0,
                      "max_tokens": endpoint.max_tokens,
                      "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": prompt}]},
                     endpoint.api_key, endpoint.timeout_s)
        message = data["choices"][0].get("message", {})
        text = (message.get("content") or "").strip()
        usage = data.get("usage", {})
    except Exception as exc:  # noqa: BLE001 - degrade to the facts
        out["narration"] = {
            "available": False, "error": str(exc),
            "note": "The deterministic findings above are unaffected."}
        return out

    elapsed = round(time.time() - started, 1)
    if not text:
        # reasoning models can spend the whole budget before emitting content
        detail = usage.get("completion_tokens_details", {})
        out["narration"] = {
            "available": False, "latency_s": elapsed,
            "error": "model returned no content"
                     + (f" ({detail.get('reasoning_tokens')} tokens went to "
                        "reasoning — raise max_tokens for this model)"
                        if detail.get("reasoning_tokens") else ""),
            "note": "The deterministic findings above are unaffected."}
        return out

    check = verify_narration(text, facts)
    out["narration"] = {
        "available": True,
        "grounded": check["grounded"],
        "text": text if check["grounded"] else "",
        "withheld_text": "" if check["grounded"] else text,
        "ungrounded_numbers": check["ungrounded_numbers"],
        "model": endpoint.model,
        "endpoint": endpoint.name,
        "latency_s": elapsed,
        "note": ("" if check["grounded"] else
                 "Narration withheld: it contained figures "
                 f"({', '.join(str(n) for n in check['ungrounded_numbers'])}) "
                 "that no measurement supports. The findings below are the "
                 "instrument's own and are unaffected."),
    }
    return out


# -- endpoint persistence ---------------------------------------------------
@dataclass
class EndpointStore:
    path: str
    endpoints: dict = field(default_factory=dict)

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            self.endpoints = {k: LLMEndpoint(**v) for k, v in raw.items()}
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            self.endpoints = {}
        return self.endpoints

    def save(self) -> None:
        import os
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({k: asdict(v) for k, v in self.endpoints.items()}, f,
                      indent=1)

    def put(self, ep: LLMEndpoint) -> LLMEndpoint:
        if not ep.name or not ep.base_url:
            raise ValueError("endpoint needs a name and base_url")
        # exactly one endpoint may be enabled, so narration is unambiguous
        if ep.enabled:
            for other in self.endpoints.values():
                other.enabled = False
        self.endpoints[ep.name] = ep
        self.save()
        return ep

    def remove(self, name: str) -> None:
        self.endpoints.pop(name, None)
        self.save()

    def active(self) -> LLMEndpoint | None:
        return next((e for e in self.endpoints.values() if e.enabled), None)
