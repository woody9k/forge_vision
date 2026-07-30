"""Optional LLM narration tests.

The deterministic facts are the instrument's output. A language model may
rephrase them and nothing more, so the tests concentrate on the ways a model
can go wrong: inventing figures, returning nothing, being unreachable, or
being slow. In every case the facts must survive untouched.

A mock OpenAI-compatible server keeps these tests fully offline (§12).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from forge_vision.sage.narrate import (EndpointStore, LLMEndpoint, health,
                                       narrate, verify_narration)

FACTS = [
    {"kind": "calculation",
     "statement": "Finding #1 sits at (1.20, 1.20) m in the site frame, "
                  "0.78 m deep.",
     "values": {"x_m": 1.2, "y_m": 1.2, "depth_m": 0.78}, "action": ""},
    {"kind": "observation",
     "statement": "Highlighted because 2 focused responses across 2 scans "
                  "fell within tolerance. Strongest contrast 7.3 dB.",
     "values": {"supporting_scans": 2, "max_contrast_db": 7.3}, "action": ""},
    {"kind": "unknown",
     "statement": "Material and object class are not determined.",
     "values": {}, "action": ""},
]


class _Handler(BaseHTTPRequestHandler):
    reply = "OK"
    usage = {}
    status = 200

    def log_message(self, *a):  # silence the test server
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "mock-model"}, {"id": "other"}]})
        else:
            self._send({}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if type(self).status != 200:
            self._send({"error": "boom"}, type(self).status)
            return
        self._send({"choices": [{"message": {"content": type(self).reply}}],
                    "usage": type(self).usage})

    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def mock_llm():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def _ep(base_url, **kw):
    return LLMEndpoint(name="mock", base_url=base_url, model="mock-model",
                       enabled=True, timeout_s=5, **kw)


def _answer():
    return {"question": "why is finding 1 highlighted?", "understood": True,
            "facts": list(FACTS), "note": "", "evidence_count": 4}


# -- groundedness verification ---------------------------------------------
def test_verifier_accepts_faithful_prose():
    text = ("Finding #1 is at (1.20, 1.20) m, about 0.78 m deep. Two scans "
            "agree, with 7.3 dB contrast. What it is remains undetermined.")
    check = verify_narration(text, FACTS)
    assert check["grounded"], check["ungrounded_numbers"]


def test_verifier_catches_invented_depth():
    """The failure §17 warns about: a plausible number nothing measured."""
    text = "Finding #1 is a pipe about 2.4 m deep, detected with 95% certainty."
    check = verify_narration(text, FACTS)
    assert not check["grounded"]
    assert 2.4 in check["ungrounded_numbers"]
    assert 95.0 in check["ungrounded_numbers"]


def test_verifier_tolerates_rounding():
    check = verify_narration("roughly 0.8 m deep with 7 dB of contrast", FACTS)
    assert check["grounded"], check["ungrounded_numbers"]


# -- narration behaviour ----------------------------------------------------
def test_grounded_narration_is_returned(mock_llm):
    _Handler.reply = ("Finding #1 sits at (1.20, 1.20) m and about 0.78 m "
                      "deep. Two scans agree. The material is not determined.")
    out = narrate(_answer(), _ep(mock_llm))
    assert out["narration"]["available"] is True
    assert out["narration"]["grounded"] is True
    assert "0.78" in out["narration"]["text"]
    assert out["facts"] == FACTS, "facts must never be altered by narration"


def test_hallucinated_narration_is_withheld(mock_llm):
    _Handler.reply = ("This is almost certainly a buried steel pipe at 2.4 m "
                      "depth, with 95% confidence.")
    out = narrate(_answer(), _ep(mock_llm))
    n = out["narration"]
    assert n["available"] is True
    assert n["grounded"] is False
    assert n["text"] == "", "ungrounded prose must not be presented as an answer"
    assert n["withheld_text"]
    assert 2.4 in n["ungrounded_numbers"]
    assert "no measurement supports" in n["note"]
    assert out["facts"] == FACTS


def test_unreachable_endpoint_degrades_to_facts():
    out = narrate(_answer(), _ep("http://127.0.0.1:9/v1"))
    assert out["narration"]["available"] is False
    assert out["narration"]["error"]
    assert out["facts"] == FACTS


def test_server_error_degrades_to_facts(mock_llm):
    _Handler.status = 500
    try:
        out = narrate(_answer(), _ep(mock_llm))
        assert out["narration"]["available"] is False
        assert out["facts"] == FACTS
    finally:
        _Handler.status = 200


def test_empty_reply_from_reasoning_model_is_explained(mock_llm):
    """Observed with a real reasoning model: the whole budget goes to
    reasoning tokens and content comes back empty."""
    _Handler.reply = ""
    _Handler.usage = {"completion_tokens_details": {"reasoning_tokens": 186}}
    try:
        out = narrate(_answer(), _ep(mock_llm))
        assert out["narration"]["available"] is False
        assert "reasoning" in out["narration"]["error"]
        assert "max_tokens" in out["narration"]["error"]
    finally:
        _Handler.usage = {}
        _Handler.reply = "OK"


def test_disabled_endpoint_produces_no_narration(mock_llm):
    ep = _ep(mock_llm)
    ep.enabled = False
    out = narrate(_answer(), ep)
    assert "narration" not in out


def test_narration_never_runs_without_facts(mock_llm):
    out = narrate({"question": "?", "facts": [], "understood": False},
                  _ep(mock_llm))
    assert "narration" not in out


def test_health_lists_models(mock_llm):
    h = health(_ep(mock_llm))
    assert h["reachable"] is True
    assert "mock-model" in h["models"]


def test_health_on_dead_endpoint():
    h = health(_ep("http://127.0.0.1:9/v1"))
    assert h["reachable"] is False
    assert h["error"]


# -- endpoint store ---------------------------------------------------------
def test_endpoint_store_roundtrip_and_single_active(tmp_path):
    store = EndpointStore(str(tmp_path / "llm.json"))
    store.load()
    store.put(LLMEndpoint("a", "http://a/v1", model="m", enabled=True))
    store.put(LLMEndpoint("b", "http://b/v1", model="m", enabled=True))
    assert store.active().name == "b"
    assert store.endpoints["a"].enabled is False, "only one may be active"

    reloaded = EndpointStore(str(tmp_path / "llm.json"))
    reloaded.load()
    assert set(reloaded.endpoints) == {"a", "b"}
    assert reloaded.active().name == "b"

    reloaded.remove("b")
    assert reloaded.active() is None


def test_endpoint_requires_name_and_url(tmp_path):
    store = EndpointStore(str(tmp_path / "llm.json"))
    with pytest.raises(ValueError):
        store.put(LLMEndpoint("", "http://x/v1"))


def test_api_key_is_redacted(tmp_path):
    ep = LLMEndpoint("a", "http://a/v1", api_key="secret-key")
    assert ep.to_dict()["api_key"] == "***"
    assert ep.to_dict(redact=False)["api_key"] == "secret-key"


# -- integration through the runtime ---------------------------------------
def test_runtime_ask_degrades_when_no_endpoint_configured(runtime):
    """With no LLM configured the platform answers exactly as before —
    offline operation must not depend on a model (§12)."""
    out = runtime.sage_ask("what should I measure next")
    assert "narration" not in out
    assert "facts" in out


def test_runtime_narration_can_be_declined(runtime, mock_llm):
    runtime.llm_put({"name": "mock", "base_url": mock_llm,
                     "model": "mock-model", "enabled": True, "timeout_s": 5})
    out = runtime.sage_ask("what should I measure next", narrate=False)
    assert "narration" not in out


# -- two-phase flow: findings first, narration second -----------------------
def test_ask_does_not_block_on_the_model(runtime, mock_llm):
    """A local model can take minutes. The instrument's own answer must be
    returned without waiting for it."""
    runtime.connect("sim-pluto-0")
    exp = runtime.record_capture("sim-pluto-0", num_samples=8192,
                                 name="narration timing")["experiment_id"]
    runtime.llm_put({"name": "mock", "base_url": mock_llm,
                     "model": "mock-model", "enabled": True, "timeout_s": 5})
    out = runtime.sage_ask("summarize this experiment", experiment_id=exp)
    assert "narration" not in out, "ask() must not call the model"
    assert out["narration_available"] is True
    assert out["facts"], "the instrument's own answer must still be complete"


def test_narrate_phase_returns_only_the_narration(runtime, mock_llm):
    _Handler.reply = "Two scans agree at 0.78 m depth."
    runtime.llm_put({"name": "mock", "base_url": mock_llm,
                     "model": "mock-model", "enabled": True, "timeout_s": 5})
    answer = {"question": "why?", "facts": list(FACTS)}
    n = runtime.sage_narrate(answer)
    assert n["available"] is True and n["grounded"] is True
    assert "0.78" in n["text"]


def test_narrate_without_endpoint_is_explicit(runtime):
    n = runtime.sage_narrate({"question": "why?", "facts": list(FACTS)})
    assert n["available"] is False
    assert "no language model endpoint is enabled" in n["error"]
    assert "do not depend on a model" in n["note"]


def test_narration_available_flag_tracks_configuration(runtime, mock_llm):
    assert runtime.sage_ask("what next")["narration_available"] is False
    runtime.llm_put({"name": "mock", "base_url": mock_llm,
                     "model": "mock-model", "enabled": True})
    assert runtime.sage_ask("what next")["narration_available"] is True
