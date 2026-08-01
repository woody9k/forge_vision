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
| **Component** | A physical part in the signal path — antenna, cable, adapter, attenuator. Carries an optional `vna` measurement with derived S11/VSWR bands and, for two-port sweeps, insertion loss. |
| **VNA measurement** | A sweep stored against a component. Records its `source` (a file import or a named instrument) and its `calibration` provenance, which stays `known: false` until a residual check establishes the calibration covers the span. |

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

**A VNA sweep emits RF, and is deliberately not gated.** `POST /api/vna/sweep`
and `/api/vna/calibration_check` drive the instrument's own source (~ −9 dBm,
fixed level, free-running whenever it is powered). It is not behind the
transmit interlock — there is no waveform, gain or profile for a TX
fingerprint to bind to, and requiring the bench to be armed to measure a cable
would be theatre. That is not permission to sweep casually: into a load or a
cable it is a closed circuit, but **into an antenna it is emission**, and you
generally cannot tell which from the API. Every sweep is written to the safety
audit log with its span. Ask before sweeping when you do not know what is on
the port.

### Transmit permission belongs to a configuration, not to a device

Even if an operator has armed the platform and started a transmission, that
permission covers *the exact setup it was granted for*: centre frequency,
occupied span, waveform, both gains, sample rate, RF bandwidth, active
frequency profile, and declared path attenuation.

Change any of those and the permission is withdrawn — the radio stops
transmitting, and `tx_authorization_revoked` is written to the safety audit.
That includes calls you might think of as harmless:

```
POST /api/devices/{id}/configure   {"rx_gain_db": 50}   → TX stops
POST /api/safety/profile           {"profile": "..."}   → TX stops
POST /api/safety/path_attenuation  {"attenuation_db":…} → TX stops
```

So do not reconfigure a device to "tidy up" while an operator is mid-run: you
will silently end their transmission. Read `GET /api/status` first, and leave
the device alone if `safety.tx_active` is true.

This exists because `configure()` used to bypass the interlock entirely — TX
gain could be raised a thousandfold, or the radio walked outside its allowed
band, while transmitting.

### Frequency limits apply to the whole occupied band

A waveform is refused if any part of its sweep falls outside the active
profile, not merely its centre frequency. A 56 MHz sweep centred inside a
26 MHz allocation is rejected, and the error says which span it occupies.

### An emergency stop latches

`POST /api/safety/stop` disables transmit, cancels running jobs, annotates
unfinished scans as interrupted, and then **refuses further acquisition** until
someone lifts it. `GET /api/status` reports `acquisition_stopped`. Lifting it
is an operator decision (`POST /api/safety/resume`, or re-arming) — an
automated client should surface the state, not clear it.

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
GET /api/status                     → devices, capabilities, safety, storage,
                                      acquisition_stopped
GET /api/experiments                → index; add ?kind=scan|range|survey|stepped
GET /api/experiments/{id}           → full manifest with annotations
GET /api/experiments/{id}/derived/{name}
GET /api/devices/transports         → every way into every radio, measured
GET /api/radios                     → saved radio addresses
GET /api/rf_chain                   → the antenna/cable path readings pass through
GET /api/chains                     → saved chain configurations, one active
GET /api/chains/{id}                → a configuration and its measurements
```

**How a radio is attached** — each device in `/api/status` carries a `link`:

```json
{"kind": "network|usb|usb-gadget|simulated",
 "address": "pluto.boblab.net (192.168.99.222)",
 "throughput_mb_s": 22.6,
 "chosen_because": "fastest measured transport at 22.6 MB/s",
 "alternatives": [{"uri": "usb:", "kind": "usb", "throughput_mb_s": 17.5}]}
```

The same radio is usually reachable several ways and they are **not**
equivalent — Ethernet measured roughly twice the USB throughput on this bench.
`POST /api/devices/{id}/switch_transport` moves to another link;
`POST /api/devices/{id}/forget` drops the entry (the next scan finds it again).
Both change what the operator is working with, so ask first.

**The signal path is part of every measurement.** `GET /api/rf_chain` resolves
the declared antenna and cables, totals their loss and delay, and reports what
is *not* characterised rather than treating unmeasured parts as lossless. If it
says `config_modified`, the patching no longer matches the saved configuration
it is named after — do not describe a capture as coming from that
configuration.

**Measure an antenna or cable with the VNA**
```
GET  /api/vna/discover              → serial ports that answered as a VNA
GET  /api/vna/status                → model, firmware, battery, sweep, calibration
POST /api/vna/sweep                 → sweep; attaches to a component if comp_id given
POST /api/vna/sweep_job             → same, as a cancellable job
POST /api/vna/measure_delay         → electrical delay, cross-checked for aliasing
POST /api/vna/calibration_check     → measure a known thru, judge the calibration
```

**Electrical delay needs two sweeps, and the API enforces that.** Phase
unwrapping caps the measurable delay at `1/(2*step)`; past that a long cable
reports a *short* delay with a perfectly clean linear fit, and no single sweep
can tell the difference. `/api/vna/measure_delay` sweeps at two point counts
(they fold differently) and writes `nominal_delay_ns` only when they agree. If
`cross_check.agree` is false, **the true delay is longer than both figures** —
report that, do not quote the smaller one.

`ports` is **required judgement about the physical world, not a formatting
choice**: `2` for a thru (a cable between both ports), `1` for reflection only
(an antenna on port 1). The instrument returns an S21 column either way, so
declaring `2` when nothing is on port 2 stores a column of noise as insertion
loss. If you do not know what is physically connected, ask — do not guess.

Every sweep result carries a `band_check`. When `inside_profile` is false the
swept span leaves the active frequency profile; the sweep still runs and is
recorded, and the `warning` explains why that may or may not matter. Relay it.

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

**A device's `config` is what it was asked for; `sync` says whether it held.**
Every device in `/api/status` carries a `sync` block. `in_sync: true` means
the radio was read and matches. `false` means it does not, and `drift` lists
each field with its `requested` and `actual` value — quote the actual one.
**`sync: null` or `in_sync: null` means nobody has checked**, which is not the
same claim as agreement; say "unverified", not "fine". A capture's
`telemetry.config_verified` says whether that segment's configuration was read
back from the hardware rather than assumed.

**A VNA sweep is not evidence it was calibrated.** The instrument reports
which standards are captured but never the span they cover, and it silently
interpolates a calibration onto whatever span is swept. Every stored
measurement carries `vna.calibration`; while `known` is false the numbers are
readings, not verified measurements, and must be described that way.
`POST /api/vna/calibration_check` is what turns that into an answer — it
measures the residual against a known thru, and reports edge-weighted error as
the signature of an interpolated calibration. Its verdict is deliberately
hedged ("consistent with"), because a marginal connector produces the same
pattern. Do not upgrade it to a certificate.

**Loss flatters a match.** For a two-port component, S11 is measured through
the part's own loss, which attenuates any reflection twice. A 6 dB pad shows a
near-perfect VSWR and is a useless feedline. The `recommended` /
`marginal` / `unsuitable` band ratings describe an **antenna**; for a cable or
attenuator, quote insertion loss instead. Measured on this bench: a 5.8 dB
cable read −24 dB mean S11 while a low-loss thru read −16 dB on the same
instrument — the lossier part looked better matched.

**A delay figure is conditional on not having aliased.** `delay_analysis` on a
stored sweep carries `alias_checked: false` and an `unambiguous_max_ns`
ceiling: the number is valid only if the true delay is under it, and the sweep
itself cannot establish that. Say so when quoting one, or use the
cross-checked `/api/vna/measure_delay` figure instead.

**A calibration reference plane is a declaration, not a measurement.** Where a
delay or loss includes a `reference_plane_ns` correction, that part came from
the operator saying what was connected during calibration. It is recorded in
the component notes as an assumption; keep it labelled that way.

**Cable loss is frequency-dependent, so a bare figure is not a measurement.**
Insertion loss is always reported with the frequency it was taken at. Keep
them together; "1.4 dB" alone is a claim nobody can check.

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
- A **NanoVNA-F V2** (50 kHz–3 GHz) measures antennas and cables. It is a
  separate instrument from the radio and is not part of the device registry —
  reach it through `/api/vna/*`, not `/api/devices/*`.
- **Nothing has ever transmitted on this bench.** The receive path is verified
  against real hardware; the transmit path is exercised only in simulation.
  Treat any transmit-derived result as unvalidated until that changes. The
  VNA's own source is the sole exception, and it is an instrument stimulus,
  not the platform's transmit path.

### Maturity — do not confuse "implemented" with "validated"

Code existing is not evidence that a physical claim holds. As of this writing:

| Area | Status |
|---|---|
| Receive path, capture, band survey | **bench validated** on a real Pluto+ |
| Transport selection, chain provenance, safety interlocks | **bench validated** |
| VNA component measurement | **bench validated** on a NanoVNA-F V2 — but read
  `vna.calibration` before quoting any of it |
| Radio state reconciliation | **bench validated** — drift from an out-of-band
  change is detected on a real Pluto+; check `sync` before quoting `config` |
| Range profiles, stepped-frequency synthesis | **simulator validated** — the
  headline resolution figures come from simulation |
| Migration, site fusion, Scene Builder, SAGE | **implemented**, exercised in
  simulation only |
| Bistatic geometry, coherent phase reference | **not implemented** — the
  imaging model currently assumes co-located TX and RX |
| Second RX/TX channel | **hardware only, uncharacterized** — the bench board
  runs 2R2T since 2026-07-31, but the API still reports one RX and one TX
  channel and no measurement of channel 2 exists yet |

When reporting a result, say which of these it rests on. A migrated image from
simulated data is a demonstration of the code, not a measurement of the ground.

## If you are unsure

Prefer `GET /api/sage/...` over interpreting raw products yourself — SAGE
already applies the epistemic discipline described above, and its answers carry
evidence links you can follow. If a question cannot be answered from stored
measurements, saying so is the correct response.
