# Forge Vision

Software-defined RF perception and subsurface imaging platform, implementing
the **Forge Vision Platform Requirements v0.1** (see
`Forge_Vision_Platform_Requirements_v0.1.docx`).

This build covers roadmap releases **0.1 RF Bench**, **0.2 Range Lab**, and
**0.3 Scan Studio**: reliable device control and raw capture, calibrated FMCW
range profiles with confidence-aware peak detection, and position-indexed
B-scan imaging — all on top of the experiment/provenance system the spec
requires.

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
7. **Experiments** → every run is a self-contained package: browse, search,
   verify checksums, export/import as zip, annotate, and **Replay** — reprocess
   the stored raw I/Q with new parameters, no hardware needed.

## Tests

```bash
.venv/bin/python -m pytest tests/
```

40 tests keyed to the spec's reference experiments and acceptance criteria:

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

## Architecture

```
forge_vision/
  config.py          physical constants, media presets, safety limits
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
