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
5. **Transmit is gated, and the gate belongs to a configuration.** Every TX
   path runs through `SafetyController`, is bracketed by `try/finally`, and
   RX-protection is enforced for physical radios only — a simulated receiver
   cannot be damaged, so the interlock records rather than refuses there.
   Permission is granted against a *fingerprint* of the approved setup
   (frequency, occupied span, waveform, gains, rate, profile, declared path),
   so changing any of those withdraws it and forces TX off. The whole occupied
   band is checked, not the centre frequency: a 56 MHz sweep centred in a
   26 MHz allocation is legal at its midpoint and illegal either side of it.
   This paragraph used to claim every TX path was gated while `configure()`
   bypassed the interlock entirely — TX gain could go from −30 dB to 0 dB, or
   the radio be walked outside the active profile, mid-transmission. Do not
   add a TX path that bypasses this, and do not weaken the fingerprint.
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

- The bench radio is a **Pluto+**. Since the 2026-07-30 reflash it reports as
  **PlutoSDR Rev.C (Z7010-AD9364)**, `fw_version v0.33-3-gd382-dirty`, kernel
  5.4.0, on-device libiio 0.21. It previously reported Rev.B with kernel 6.1
  and libiio 0.26 — if you see Rev.B, the older firmware is back.
- It runs as **AD9364** (`ad9361-phy,model: ad9364`), 1R1T: `cf-ad9361-lpc`
  has two channels, which is I/Q of a single receiver, not two receivers.
  Earlier firmware needed `fw_setenv compatible ad9364`; setting
  `compatible=ad9361` on a non-Rev.C model string was silently downgraded to
  `ad9363a` by the boot script, which cost an evening. The model string is now
  Rev.C, so that particular trap no longer applies.
- Tuning is **70 MHz – 6 GHz**. The current firmware advertises
  `[70000000 1 6000000000]` honestly; the older one advertised a 46.875 MHz
  floor and then rejected anything below 70 MHz. Capability detection still
  probes the edge rather than trusting the advertised value — cheap, and it
  is what caught the old lie.
- A Pluto cannot stream at full rate; captures are single DMA bursts. Measured
  sustained buffer throughput, 16 MB reads:

  | transport | throughput | sustained | live frames |
  |---|---|---|---|
  | `ip:<LAN address>` (Ethernet) | 52.6 MB/s | 13.2 MSPS | **11.8 fps** |
  | `usb:` | 28.3 MB/s | 7.1 MSPS | 5.9 fps |
  | `ip:192.168.2.1` (USB gadget) | 21.3 MB/s | 5.3 MSPS | — |

  Only one handle may claim the **USB** interface (`connect()` is idempotent);
  the network backends are not exclusive, so diagnostics can run alongside a
  deployment. Discovery measures each transport and picks the fastest rather
  than assuming — see `devices/discovery.py`. Site-specific addresses go in
  `FORGE_VISION_PLUTO_URIS`; `prefer` on `/api/devices/rescan` overrides the
  choice, and an override that cannot be met is reported, not silently
  swapped.
- The Ethernet port has a **static address, 192.168.99.222** (`ipaddr_eth` in
  the `[USB_ETHERNET]` section of `config.txt` on the board's FAT16 partition).
  To change it: mount `/dev/sda1`, edit `config.txt` — it is **CRLF**, so a
  `sed` pattern anchored with `$` silently matches nothing — set
  `[ACTIONS] reset = 1`, then send a real **SCSI eject** (`eject /dev/sda`).
  A plain `umount` is not enough; the firmware only re-reads the file on a
  medium-removal event, and clears the reset flag itself after rebooting.
- **`usb:`, `ip:192.168.99.222` and `ip:192.168.2.1` are all the same board.**
  Verified by writing the LO on one and reading it on another. Registering the
  radio twice gives two device entries with independent cached configs that
  silently diverge — observed one entry reporting 923 MHz while the other and
  the hardware were at 1090 MHz. A capture taken through the stale entry would
  record an RF config the radio never had. Register one URI, not two.
- Reflashing re-enumerates the board, which invalidates any open USB handle.
  The adapter keeps reporting `connected: true`; the tell is that `health`
  loses its `temperature_c` field. Disconnect and reconnect to recover.

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
