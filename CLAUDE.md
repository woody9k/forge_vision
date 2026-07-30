# Forge Vision — notes for an AI working on this codebase

Built from `Forge_Vision_Platform_Requirements_v0.1.docx` (in the repo root).
Requirement IDs like `FR-SAF-005` and `UX-SCN-002` in comments and tests refer
to that document — keep citing them, it is how coverage is tracked.

**If you want to *use* the API rather than change the code, read
[docs/AGENTS.md](docs/AGENTS.md) and [docs/API.md](docs/API.md) instead.**

## Run and test

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn forge_vision.server.app:app --host 127.0.0.1 --port 8347
.venv/bin/python -m pytest tests/          # ~190 tests, ~75 s
.venv/bin/python tools/gen_api_docs.py     # regenerate docs/API.md after API changes
```

Data lives in `~/.forge-vision` (override with `FORGE_VISION_DATA`). Tests use
a temp dir via the `runtime` / `armed_runtime` fixtures in `tests/conftest.py`.

To run it as a deployment rather than by hand, use `deploy/forge-vision`
(`preflight`, `start`, `stop`, `status`, `health`, `logs`, `backup`) and read
[docs/DEPLOY.md](docs/DEPLOY.md). **Do not bind to `0.0.0.0` casually** — the
API has no authentication and can key a transmitter; `SafetyController` gates
mistakes, not strangers.

## The rules this codebase holds itself to

These are not style preferences — they come from the spec and several are
enforced by tests. Breaking one is a defect even if everything still passes.

1. **Never present inferred data as measured.** Interpolated B-scan columns are
   marked; migrated cells without enough fold are dropped, not zero-filled;
   depth slices cover only measured lines. "Unmeasured" and "empty" are
   different claims.
2. **Never report precision you do not have.** Ranges carry intervals from
   resolution and permittivity uncertainty. Confidence for lateral position and
   depth is rated separately.
3. **Never hide a problem.** Clipping, sample loss, stale positions, calibration
   mismatch, and thin migration support are surfaced, not smoothed over.
4. **Raw data is immutable.** Finalized experiments refuse new segments;
   derived products are separate artifacts carrying stage versions and
   parameters.
5. **Transmit is gated.** Every TX path runs through `SafetyController`, is
   bracketed by `try/finally`, and is enforced for physical radios only —
   a simulated receiver cannot be damaged, so the interlock records rather than
   refuses there. Do not add a TX path that bypasses this.
6. **SAGE may not assert without evidence.** `Fact` raises
   `UngroundedStatement` at construction if a non-`unknown` statement has no
   evidence links. LLM narration is checked for invented numbers and withheld
   if any are found. Keep it that way.

## Layout

`server/runtime.py` is the UI-agnostic orchestration layer and is what the
tests drive; `server/app.py` is a thin routing shell over it. Put logic in the
runtime, not the routes.

```
devices/     adapter contract, simulated radio, real Pluto, replay source
dsp/         versioned pipeline, FMCW stages, peaks, stepped-frequency synthesis
imaging/     B-scan assembly, diffraction-stack migration
sites.py     site model, cross-scan fusion into world coordinates
sage/        grounded facts, analysis, query parser, optional LLM narration
jobs.py      cancellable background jobs
positioning.py  manual / survey-wheel / replay position sources
```

## Hardware notes that cost time to learn

- The bench radio reports as a stock **PlutoSDR Rev.B**, has an SD slot, and is
  almost certainly a Pluto+ running stock ADI firmware.
- It was unlocked to **AD9364** (70 MHz – 6 GHz) via `fw_setenv compatible
  ad9364`. Setting `compatible=ad9361` on a non-Rev.C model string is silently
  downgraded to `ad9363a` by the boot script — that trap cost an evening.
- The driver **advertises** a 46.875 MHz tuning floor but rejects anything below
  70 MHz. Capability detection probes the edge rather than trusting the
  advertised value.
- A Pluto cannot stream at full rate; captures are single DMA bursts.
- Only one handle may claim the USB interface. `connect()` is idempotent, and
  `usb:` and `ip:192.168.2.1` are the same board.

## Gotchas

- `pkill -f 'uvicorn forge_vision'` matches the killing shell's own command
  line. Put the kill in its own step and use a bracket pattern:
  `pkill -f 'uvicorn forge_[v]ision'` — or just use `deploy/forge-vision stop`,
  which signals a pidfile it has verified against `/proc/<pid>/cmdline`.
- Serve the UI with no-cache headers (already done) — a stale `app.js` against
  a fresh `index.html` produces controls that silently do nothing.
- A merged PR is not proof the code is on `main`. Verify with
  `git log main..origin/<branch>`, and check that the artifacts you expect
  actually exist in the tree. This has bitten twice: two releases landed on a
  stacking branch instead of `main`, and a commit pushed to a branch *after*
  its PR merged sat unmerged while the PR still read `MERGED`.
  `deploy/forge-vision preflight` now reports this before you deploy.
