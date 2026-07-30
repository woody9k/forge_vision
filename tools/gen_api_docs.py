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

# Request shapes the server accepts but cannot advertise, because the handlers
# take an untyped dict. Documented here so an agent knows what to send.
BODY_SCHEMAS = {
    "/api/range/run": {
        "device_id": "str, default 'sim-pluto-0'",
        "waveform": "str, must be in the device's compatible_waveforms",
        "chirps": "int, coherent averages (default 8)",
        "medium": "str preset ('air','soil_dry','soil_moist',...) or object with epsilon_r",
        "use_background": "bool, subtract the stored background (default true)",
        "name": "str", "tags": "list[str]", "operator": "str",
        "pipeline_overrides": "object, per-stage DSP parameter overrides",
    },
    "/api/stepped/run": {
        "device_id": "str", "start_hz": "float", "stop_hz": "float",
        "waveform": "str, the FMCW chunk waveform (default fmcw_pluto_40M)",
        "overlap": "float in [0,1), chunk overlap used to solve PLL phase steps",
        "chirps": "int", "medium": "str or object",
        "correction": "'overlap' (default) or 'none'",
        "max_range_m": "float",
    },
    "/api/scan/start": {
        "device_id": "str",
        "plan": "object: start_m, end_m, step_m, waveform, chirps, medium, "
                "antenna_height_m, position_uncertainty_m, max_range_m, notes",
    },
    "/api/scan/{scan_id}/point": {
        "x_m": "float, or omit entirely to take the position from the active source",
        "operator_override": "bool, accept a point that failed the quality gate",
    },
    "/api/survey": {
        "device_id": "str", "start_hz": "float", "stop_hz": "float",
        "step_hz": "float", "sample_rate_hz": "float", "rx_gain_db": "float",
        "samples": "int", "name": "str",
    },
    "/api/sites/{site_id}/register": {
        "experiment_id": "str, a finalized scan",
        "origin_x_m": "float", "origin_y_m": "float",
        "heading_deg": "float, CCW from +x", "label": "str",
        "position_uncertainty_m": "float",
    },
    "/api/sage/ask": {
        "question": "str", "site_id": "str, optional context",
        "experiment_id": "str, optional context",
        "narrate": "bool, default false — narration is fetched separately",
    },
    "/api/jobs": {"kind": "'survey' | 'site_scene' | 'replay'",
                  "params": "object, the arguments for that job kind"},
    "/api/position/source": {
        "kind": "'manual' | 'serial' | 'replay'",
        "port": "str (serial)", "baud": "int (serial)",
        "wheel_circumference_m": "float (serial)",
        "counts_per_revolution": "int (serial)",
        "samples": "list[object] (replay)",
    },
    "/api/devices/{device_id}/configure": {
        "center_frequency_hz": "float", "sample_rate_hz": "float",
        "rx_bandwidth_hz": "float", "rx_gain_db": "float", "tx_gain_db": "float",
    },
    "/api/safety/arm": {"operator": "str", "acknowledgement": "str"},
    "/api/safety/path_attenuation": {"attenuation_db": "float"},
    "/api/rf_chain": {"tx_ids": "list[str]", "rx_ids": "list[str]",
                      "antenna_tx": "str", "antenna_rx": "str"},
    "/api/components": {"kind": "antenna|cable|adapter|attenuator|filter|...",
                        "name": "str", "connector": "str", "claimed_band": "str",
                        "nominal_loss_db": "float", "nominal_delay_ns": "float"},
    "/api/llm": {"name": "str", "base_url": "str ending in /v1", "model": "str",
                 "api_key": "str", "max_tokens": "int", "enabled": "bool"},
}

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
              "FastAPI cannot advertise these because the handlers accept an",
              "untyped object, so they are documented here.", ""]
    for path, schema in sorted(BODY_SCHEMAS.items()):
        lines += [f"### `{path}`", "", "| field | meaning |", "|---|---|"]
        for k, v in schema.items():
            lines.append(f"| `{k}` | {v} |")
        lines.append("")

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
          f"{len(BODY_SCHEMAS)} documented request bodies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
