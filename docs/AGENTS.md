# Forge Vision — orientation for an automated client

You are talking to a **scientific instrument**, not a data service. It measures
radio reflections and turns them into statements about physical space. The
whole design assumes those statements will be acted on, so it is deliberately
conservative: it separates what was measured from what was inferred, refuses to
report precision it does not have, and will not let software key a transmitter
on its own.

Read this before driving the API. [API.md](API.md) has the endpoint reference.

---

## The one-paragraph summary

An SDR transmits a swept waveform and records the echo. Delay converts to
range through an assumed propagation velocity. Moving the antenna along a line
and stacking those range profiles gives a **B-scan**; a compact buried object
appears as a hyperbola, which **migration** collapses back to a point.
Registering several scans into a **site** and clustering the focused responses
finds anomalies that persist across independent geometries — which is the
difference between a real reflector and an artefact. Everything is stored in an
**experiment package** with the raw samples, so any conclusion can be traced
back and reprocessed.

---

## Concepts

| Term | Meaning |
|---|---|
| **Experiment** | A self-contained package: raw I/Q, metadata, derived products, provenance, checksums. Immutable once finalized. Identified by a timestamp id like `20260729-231057-b15557`. |
| **Segment** | One timed capture inside an experiment. Carries its own position, quality flags, and loss events. |
| **Derived product** | Anything computed from raw data (`range_profile`, `bscan`, `stepped_profile`, `band_survey`), stored alongside the pipeline version and parameters that produced it. |
| **Scan** | A position-indexed experiment. Captures at planned points along a line, assembled into a B-scan. |
| **Site** | A physical place with a coordinate frame. Scans register into it with an origin and heading. |
| **Finding** | A response that survived cross-scan fusion. Carries evidence links, a depth *interval*, and separate confidence for lateral position and depth. |
| **Fact** | SAGE's unit of output. Every one has an epistemic label and, unless labelled `unknown`, evidence links. |

## Epistemic labels

SAGE labels every statement. Preserve these when relaying — collapsing an
`inference` into a statement of fact is the failure mode this platform exists
to prevent.

- `observation` — measured directly
- `calculation` — derived by a documented method
- `inference` — concluded under stated assumptions
- `hypothesis` — a candidate explanation, not established
- `unknown` — **not determined by these measurements**

`unknown` is a real answer and often the correct one. Findings are classified
only as *"persistent anomaly, unknown type"*. The platform never identifies
what an object is, and neither should you.

---

## Safety boundaries — what you must not do

**Do not transmit.** These endpoints key a real radio:

- `POST /api/devices/{id}/tx`
- `POST /api/range/run`
- `POST /api/stepped/run`
- `POST /api/scan/{id}/point`
- `POST /api/calibration/{id}/background`

**Do not change safety state.** `POST /api/safety/arm`, `/disarm`,
`/checklist`, `/profile`, `/path_attenuation`.

`path_attenuation` deserves special mention: it asserts *"there is N dB of
attenuation in the cable path"*. That is a claim about the physical bench that
only a person standing at it can make truthfully, and the receive-protection
interlock depends on it. Asserting it from software hollows out a safeguard
that exists to prevent destroying a receiver.

**`POST /api/safety/stop` is always allowed.** Stopping is safe.

Everything else — reading experiments, running analyses on stored data, asking
SAGE, building site scenes, generating reports — operates on stored data and
cannot touch hardware.

The band survey (`POST /api/survey`) is receive-only and transmits nothing, but
it does retune a physical radio, so prefer it only when the operator has asked
for a survey.

---

## Common tasks

**Understand what exists**
```
GET /api/status                     → devices, capabilities, safety, storage
GET /api/experiments                → index; add ?kind=scan|range|survey|stepped
GET /api/experiments/{id}           → full manifest with annotations
GET /api/experiments/{id}/derived/{name}
```

**Ask about a finding** (this is the highest-value path)
```
GET  /api/sites                                  → pick a site
GET  /api/sites/{id}/scene                       → scans, findings, migrated images
GET  /api/sites/{id}/finding/{index}             → why it was highlighted, with evidence
POST /api/sage/ask  {"question": "...", "site_id": "..."}
GET  /api/sites/{id}/report                      → Markdown report
```

**Assess data quality before trusting it**
```
GET /api/sage/experiment/{id}       → summary plus saturation, SNR, calibration issues
GET /api/experiments/{id}/verify    → checksum integrity
```

**Reprocess without hardware**
```
POST /api/experiments/{id}/replay  {"medium": "soil_dry", "pipeline_overrides": {...}}
```

**Long work** — submit as a job rather than blocking:
```
POST /api/jobs  {"kind": "site_scene", "params": {"site_id": "..."}}
GET  /api/jobs/{job_id}?include_result=true
```

---

## Reading results honestly

**Depth is an interval, not a number.** `depth_interval_m` reflects
permittivity uncertainty. If the medium is uncertain the interval is wide and
that is the finding — do not quote the midpoint alone.

**Confidence is split.** `lateral_position` and `depth` are rated separately,
because an anomaly seen from two directions is confidently *located* even when
its depth is loose. Report both.

**`supporting_scans` is the credibility signal.** One scan is a candidate;
two or more from different geometries is evidence, because artefacts move with
geometry and real reflectors do not.

**Unmeasured is not empty.** Depth slices report values only along measured
scan lines. Gaps in migrated images are dropped, not interpolated. A survey
showing a quiet band may mean the antenna was deaf there — check the recorded
RF chain before concluding otherwise.

**Resolution claims are bounded.** A stepped-frequency profile's resolution
follows the frequencies actually measured, and degrades with depth as
high-frequency content attenuates. There is no single resolution figure for a
whole profile.

**Warnings are data.** `capability_notes`, `coverage_note`,
`depth_focus_warning`, and gate failures are the instrument telling you the
limits of its own measurement. Relay them.

---

## Current state of the bench

- The platform runs against a **simulated radio** by default (`sim-pluto-0`),
  which models delays, leakage, noise, clipping, and PLL phase jitter.
- A real Pluto appears as `pluto-usb:` or `pluto-ip:...` when attached.
- **Nothing has ever transmitted on this bench.** The receive path is verified
  against real hardware; the transmit path is exercised only in simulation.
  Treat any transmit-derived result as unvalidated until that changes.

## If you are unsure

Prefer `GET /api/sage/...` over interpreting raw products yourself — SAGE
already applies the epistemic discipline described above, and its answers carry
evidence links you can follow. If a question cannot be answered from stored
measurements, saying so is the correct response.
