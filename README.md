# Forge Vision

Software-defined RF perception and subsurface imaging platform, implementing
the **Forge Vision Platform Requirements v0.1** (see
`Forge_Vision_Platform_Requirements_v0.1.docx`).

This build covers roadmap releases **0.1 RF Bench** through **0.5 SAGE
Perception**: reliable device control and raw capture, calibrated FMCW range
profiles with confidence-aware peak detection, position-indexed B-scan
imaging, migration and cross-scan fusion into world coordinates, and a
grounded assistant over the stored evidence — all on top of the
experiment/provenance system the spec requires.

Releases 0.6 (portable field system) and 1.0 (validated platform) are not
started. Nothing has ever transmitted: the receive path is verified against
real hardware, the transmit path is exercised only in simulation.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn forge_vision.server.app:app --host 127.0.0.1 --port 8347
```

Open http://127.0.0.1:8347/ and:

1. **Dashboard** → Connect `sim-pluto-0` (a physically-modeled virtual Pluto+;
   real hardware is discovered automatically when `pyadi-iio` is installed and
   a device is reachable).
2. **Safety** → enter your name, tick the acknowledgement, **Arm TX for this
   session**. Nothing transmits until you do (FR-SAF-001); the red **STOP**
   button in the header kills all transmission at any time (FR-SAF-003).
3. **Live RF** → Start stream: spectrum, waterfall, time-domain I/Q, clipping
   and sample-loss alerts, raw capture recording.
4. **Range Lab** → Run ranging: range profile with detected targets, each with
   measured delay (observation) separated from derived range (model), an
   uncertainty interval, confidence, and a "TX leakage?" flag for near-zero
   returns. Capture a background and re-run to see coherent background
   subtraction reveal targets buried under clutter.
5. **Scan Studio** → Load the buried-target scene, Start scan, Auto-run: a
   B-scan builds column by column with quality gating; toggle *remove mean
   trace* to watch the buried-target hyperbolas emerge from clutter. Scans
   resume cleanly after interruption or restart by ID.
6. **Antenna Lab** → inventory your antennas, cables, and attenuators
   (FR-RFC-001/002), import NanoVNA touchstone sweeps (`.s1p`/`.s2p`,
   FR-RFC-003), and get S11/VSWR plots with recommended / marginal /
   unsuitable band ratings (FR-RFC-004). Pin one component to overlay a
   second trace for comparison.
7. **World View** → create a site, register finalized scans into it with an
   origin and heading, and build the scene. Each B-scan is focused by
   diffraction-stack migration, focused responses are transformed into site
   coordinates, and responses that recur across scans fuse into findings with
   persistence and confidence. Click a finding to reach its contributing
   scans; export a Markdown site report following the spec's Appendix B.
8. **SAGE** → ask grounded questions about a site or experiment: *why is
   finding 1 highlighted?*, *show anomalies between 0.5 and 2 m deep*, *what
   should I measure next?* Every statement carries an epistemic label
   (observation / calculation / inference / hypothesis / unknown) and links to
   the artifacts behind it. It is read-only and cannot transmit.
   Optionally point it at any OpenAI-compatible local model (Ollama, LM
   Studio, vLLM, llama.cpp) for plain-prose narration *over* those findings —
   every figure in the prose is checked against the measurements, and
   narration that invents one is withheld. Findings never wait on the model.
9. **Experiments** → every run is a self-contained package: browse, search,
   verify checksums, export/import as zip, annotate, and **Replay** — reprocess
   the stored raw I/Q with new parameters, no hardware needed.

## Tests

```bash
.venv/bin/python -m pytest tests/
```

138 tests keyed to the spec's reference experiments and acceptance criteria:

| Spec item | Test |
|---|---|
| REF-02 plate at known distance | `test_dsp.py::test_ref02_plate_at_known_distance` |
| REF-03 background subtraction | `test_dsp.py::test_ref03_background_subtraction_highlights_change` |
| REF-05 spatially coherent B-scan | `test_scan.py::test_ref05_bscan_spatially_coherent` |
| REF-07 unknown-medium uncertainty | `test_dsp.py::test_ref07_unknown_medium_shows_interval_not_false_precision` |
| REF-08 interrupted experiment | `test_experiments.py::test_partial_experiment_readable_after_fault`, `test_scan.py::test_scan_resume_no_duplication` |
| AC-004 safe default (no auto-TX) | `test_safety.py::test_tx_disabled_at_startup` |
| AC-006 replay without hardware | `test_experiments.py::test_replay_without_hardware` |
| FR-SAF-* interlock, limits, e-stop, fault-safe | `test_safety.py` |
| FR-DAT-* immutability, integrity, export/import | `test_experiments.py` |
| FR-DSP-010 determinism | `test_dsp.py::test_pipeline_determinism` |
| Milestone D world-coordinate anomaly | `test_scene.py::test_end_to_end_perpendicular_scans_in_world_coordinates` |
| Milestone E evidence-linked answer | `test_sage.py::test_milestone_e_explains_anomaly_with_evidence_links` |
| FR-API-003 job submit/cancel/retry | `test_jobs.py` |
| FR-SAF-005/006 receive-path protection | `test_jobs.py::test_bare_tx_to_rx_cable_is_critical` |
| FR-RFC-006 connector chain | `test_jobs.py::test_experiment_records_the_connector_chain` |

## Architecture

```
forge_vision/
  config.py          physical constants, media presets, safety limits
  jobs.py            cancellable background jobs with progress (FR-API-003)
  positioning.py     manual / survey-wheel / replay position sources, pose
                     model separating measured angles from assumed ones
  safety.py          TX interlock, limit enforcement, audit log, e-stop
  waveforms.py       versioned waveform catalog (CW, FMCW, stepped, RX-only)
  devices/
    base.py          DeviceAdapter contract + capability model (FR-API-004)
    simulated.py     physically-modeled virtual Pluto+ (delays, leakage,
                     noise, clipping, buried-target scenes with hyperbolas)
    replay.py        stored experiments as virtual acquisition (FR-ACQ-008)
    pluto.py         real Pluto/Pluto+ via pyadi-iio (optional)
  dsp/
    pipeline.py      versioned, deterministic stage pipeline with provenance
    stages.py        dechirp/FFT range profile, coherent background
                     subtraction, peak detection, quality metrics
    peaks.py         peaks with measured-vs-derived separation + uncertainty
  imaging/bscan.py   B-scan assembly, quality layer, mean-trace clutter
                     removal, marked interpolation, resume state
  imaging/migration.py  diffraction-stack focusing with fold limiting and a
                     depth-focus warning when geometry cannot resolve depth
  sites.py           site model, scan registration, cross-scan fusion into
                     world coordinates, depth slices
  reports.py         Markdown site report (Appendix B outline)
  sage/
    facts.py         the grounded Fact: evidence and an epistemic label are
                     enforced at construction, not by convention
    analysis.py      quality assessment, experiment summary, finding
                     explanation, comparison, next-measurement recommendation
    query.py         deterministic grounded query parser (offline by design)
    narrate.py       optional OpenAI-compatible narration with groundedness
                     verification; degrades to the facts on any failure
  experiments/store.py  self-contained packages: raw npy + JSON manifests,
                     checksums, lineage, annotations, export/import
  server/
    runtime.py       UI-agnostic orchestration (used directly by tests)
    app.py           FastAPI HTTP + WebSocket live streaming
  ui/                browser UI (no build step): Dashboard, Live RF,
                     Range Lab, Scan Studio, Experiments, Safety
```

Design principles from the spec that shaped the code:

- **Hardware isolated from UI** (§4.1): a crashed stream or UI can never leave
  the transmitter on; every TX-on path is bracketed by `try/finally` and the
  safety controller.
- **Raw data is immutable** (FR-DAT-001): finalized packages refuse new
  segments; processed products are separate derived artifacts with full stage/
  version/parameter lineage (FR-DAT-002).
- **Never hide uncertainty**: ranges carry intervals from range resolution and
  permittivity uncertainty; interpolated B-scan columns are visually marked;
  clipping and sample loss are alerts and metadata, never silently dropped.
- **Deterministic DSP** (FR-DSP-010): same raw data + pipeline fingerprint →
  identical output; replay reproduces original results bit-for-bit.

## Position rig (survey wheel)

`firmware/esp32_position/esp32_position.ino` streams JSON position lines over
USB from an ESP32 with a rotary encoder on a wheel of known circumference:

```
{"t":12.345,"counts":1830,"x_m":1.372,"heading_deg":91.2}
```

Only distance is required; an absent field is recorded as *not measured*
rather than defaulted. Scan Studio can then capture at the reported position
instead of a typed one, snapping to the nearest planned grid point and
recording how far it had to move. A rig that has rolled off the end of the
line, or whose link has stalled, fails the capture gate rather than
contributing a confidently mislocated trace.

```bash
.venv/bin/pip install pyserial      # only needed for a serial position rig
```

Note on GNSS: consumer GPS is metres-accurate while scan steps are
centimetres, so GPS is not usable as the scan-line position. It is useful for
tagging the site and as a clock.

## Data location

Experiments, calibration assets, and the safety audit log live in
`~/.forge-vision/` (override with `FORGE_VISION_DATA`).

## Using real hardware

```bash
sudo apt install libiio-utils libiio-dev   # system libiio (C library + tools)
.venv/bin/pip install pyadi-iio            # Python bindings
```

Radios are auto-discovered at startup, and the Dashboard's **Rescan
hardware** button probes again without a restart — plug in over USB
(`ip:192.168.2.1` gadget address) or enter the LAN address of a Pluto+ on
its Ethernet port (e.g. `ip:192.168.1.87`) in the URI field. If the driver
stack is missing, the rescan status shows the exact install commands.

The Pluto adapter runs **burst-capture only**: each acquisition is a single
contiguous DMA buffer (max ~8.4 M samples), because no Pluto can stream
61.44 MSPS continuously over USB or Ethernet. Short reads are reported as
loss events, never concealed. Tuning limits are read from the device when
the driver exposes them — a stock Pluto reports 325 MHz–3.8 GHz, a Pluto+
or AD9364-hacked unit reports 70 MHz–6 GHz — with conservative stock limits
assumed otherwise.

Bench safety notes: start with the `bench_cabled` frequency profile, keep TX
gain at the -30 dB default, and run the attenuated loopback experiment
(REF-01, 30–40 dB inline attenuation TX→RX) before any antenna work.

## Not yet implemented (per roadmap)

Releases 0.4–1.0: world-view site mapping, multi-pass registration, SAGE AI
assistance, Jetson portable deployment, and the C-scan/volumetric imaging
extensions. The data model and adapter contracts were built so these bolt on
without reworking the experiment or device layers.
