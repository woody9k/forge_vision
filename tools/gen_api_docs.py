#!/usr/bin/env python3
"""Generate docs/API.md from a running Forge Vision instance.

Examples are captured by actually calling the API rather than being written
by hand, so they cannot drift from what the server does. Anything that would
transmit is excluded from the live calls — the generator is safe to run
against a bench with a real radio attached.

    .venv/bin/python tools/gen_api_docs.py [--base http://127.0.0.1:8347]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# Endpoints exercised live to capture a real example. Each entry is
# (method, path, body or None, note). Nothing here keys a transmitter.
PROBES = [
    ("GET", "/api/status", None, "Platform state: devices, safety, storage, waveforms."),
    ("GET", "/api/experiments", None, "Experiment index. Filter with ?query=, ?tag=, ?kind=."),
    ("GET", "/api/jobs", None, "Background job queue."),
    ("GET", "/api/sites", None, "Sites available for cross-scan fusion."),
    ("GET", "/api/components", None, "RF component inventory."),
    ("GET", "/api/rf_chain", None, "Declared cable/adapter chain, recorded with every experiment."),
    ("GET", "/api/position", None, "Active position source and its latest reading."),
    ("GET", "/api/safety/checklist", None, "Pre-transmit checks; all required items must be confirmed before arming."),
    ("GET", "/api/safety/rx_protection?device_id=sim-pluto-0", None,
     "Estimated power at the receive port for the current settings."),
    ("GET", "/api/llm", None, "Configured narration endpoints."),
]

# Endpoints that transmit or change safety state. Called out so an automated
# consumer treats them as operator actions rather than routine calls.
TRANSMITS = {
    "/api/devices/{device_id}/tx", "/api/range/run", "/api/stepped/run",
    "/api/scan/{scan_id}/point", "/api/calibration/{device_id}/background",
}
SAFETY_STATE = {
    "/api/safety/arm", "/api/safety/disarm", "/api/safety/checklist",
    "/api/safety/profile", "/api/safety/path_attenuation",
}


def call(base: str, method: str, path: str, body=None, timeout: float = 30.0):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "detail": e.read().decode()[:200]}
    except Exception as e:  # noqa: BLE001
        return {"_unreachable": str(e)}


def field_type(prop: dict) -> str:
    """A short human description of a JSON-Schema property."""
    if "anyOf" in prop:
        parts = [field_type(x) for x in prop["anyOf"]]
        return " or ".join(dict.fromkeys(p for p in parts if p != "null"))
    t = prop.get("type", "")
    if t == "array":
        return f"list of {field_type(prop.get('items', {}))}"
    if t == "null":
        return "null"
    bits = [t or "any"]
    if "pattern" in prop:
        bits.append(f"matching `{prop['pattern']}`")
    rng = []
    for key, sym in (("minimum", ">="), ("exclusiveMinimum", ">"),
                     ("maximum", "<="), ("exclusiveMaximum", "<")):
        if key in prop:
            rng.append(f"{sym} {prop[key]}")
    if "minLength" in prop and prop["minLength"]:
        rng.append(f"at least {prop['minLength']} character(s)")
    if rng:
        bits.append("(" + ", ".join(rng) + ")")
    return " ".join(bits)


def request_body_reference(spec: dict) -> list:
    """A field table per endpoint, derived from the OpenAPI components."""
    schemas = spec.get("components", {}).get("schemas", {})
    out = []
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in sorted(ops.items()):
            ref = ((op.get("requestBody") or {}).get("content", {})
                   .get("application/json", {}).get("schema", {}).get("$ref"))
            if not ref:
                continue
            model = schemas.get(ref.rsplit("/", 1)[-1], {})
            props = model.get("properties", {})
            if not props:
                continue
            required = set(model.get("required", []))
            out += [f"### `{method.upper()} {path}`", ""]
            if model.get("description"):
                out += [model["description"].strip(), ""]
            out += ["| field | type | required | default | notes |",
                    "|---|---|---|---|---|"]
            for name, prop in props.items():
                # "no default declared" and "defaults to empty string" are
                # different claims; rendering both as "" would invent one.
                default = (f"`{json.dumps(prop['default'])}`"
                           if "default" in prop else "—")
                out.append(
                    f"| `{name}` | {field_type(prop)} | "
                    f"{'yes' if name in required else 'no'} | {default} | "
                    f"{prop.get('description', '')} |")
            out.append("")
    return out


def truncate(obj, depth=0):
    """Shorten long arrays so examples stay readable."""
    if isinstance(obj, list):
        if len(obj) > 3:
            return [truncate(x, depth + 1) for x in obj[:3]] + [f"... {len(obj)} total"]
        return [truncate(x, depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {k: truncate(v, depth + 1) for k, v in obj.items()}
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8347")
    ap.add_argument("--out", default="docs/API.md")
    args = ap.parse_args()

    spec = call(args.base, "GET", "/openapi.json")
    if "_unreachable" in spec:
        print(f"Forge Vision is not reachable at {args.base}: {spec['_unreachable']}",
              file=sys.stderr)
        print("Start it first:  .venv/bin/uvicorn forge_vision.server.app:app "
              "--port 8347", file=sys.stderr)
        return 1

    lines = [
        "# Forge Vision HTTP API",
        "",
        "Generated by `tools/gen_api_docs.py` against a running instance — the",
        "examples below are real responses, not hand-written ones. Regenerate",
        "after changing the API:",
        "",
        "```bash",
        ".venv/bin/python tools/gen_api_docs.py",
        "```",
        "",
        "Base URL defaults to `http://127.0.0.1:8347`. All bodies and responses",
        "are JSON. Errors return `{\"detail\": \"...\"}` with **400** for a bad",
        "request, **403** for a safety violation, **404** for an unknown id.",
        "",
        "> **Read [AGENTS.md](AGENTS.md) first** if you are an automated client.",
        "> It covers the concepts, the safety boundaries, and which calls a",
        "> non-human should not make.",
        "",
        "## Endpoints that transmit or change safety state",
        "",
        "These are **operator actions**. An automated client should not call",
        "them without explicit human instruction for that specific action:",
        "",
    ]
    for p in sorted(TRANSMITS):
        lines.append(f"- `{p}` — keys the transmitter")
    for p in sorted(SAFETY_STATE):
        lines.append(f"- `{p}` — changes safety state or asserts a physical fact")
    lines += ["", "Everything else reads stored data or runs analysis and is safe.",
              "", "---", "", "## Live examples", ""]

    for method, path, body, note in PROBES:
        result = call(args.base, method, path, body)
        lines += [f"### `{method} {path}`", "", note, "",
                  "```json", json.dumps(truncate(result), indent=1)[:1400], "```", ""]

    lines += ["---", "", "## Request bodies", "",
              "Generated from the declared request models, so this cannot drift",
              "from what the server actually accepts. Every body rejects unknown",
              "fields: a misspelled key is a 422, not a silent default.", ""]
    body_ref = request_body_reference(spec)
    lines += body_ref

    lines += ["---", "", "## Full endpoint index", "",
              "| method | path | summary |", "|---|---|---|"]
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method_name, op in sorted(ops.items()):
            flag = ""
            if path in TRANSMITS:
                flag = " ⚠️ transmits"
            elif path in SAFETY_STATE:
                flag = " ⚠️ safety state"
            lines.append(f"| {method_name.upper()} | `{path}` | "
                         f"{op.get('summary', '')}{flag} |")

    import os
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    total = sum(len(v) for v in spec.get("paths", {}).values())
    print(f"wrote {args.out}: {total} endpoints, {len(PROBES)} live examples, "
          f"{sum(1 for l in body_ref if l.startswith(chr(35) * 3))} documented request bodies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
