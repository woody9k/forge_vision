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
.venv/bin/python -m pytest tests/          # 342 tests, ~3 min
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
devices/        adapter contract, simulated radio, real Pluto, replay source
  discovery.py  probe every transport, measure it, group one board, pick one
  book.py       radio addresses the operator saved, edited from the UI
dsp/            versioned pipeline, FMCW stages, peaks, stepped-frequency synthesis
imaging/        B-scan assembly, diffraction-stack migration
rfcomponents/   component inventory, touchstone/VNA import
  chains.py     the working signal path plus named, reusable configurations
  nanovna.py    NanoVNA serial driver: discovery, sweep, calibration residual
safety.py       interlock, TX authorization fingerprints, audit log
sites.py        site model, cross-scan fusion into world coordinates
sage/           grounded facts, analysis, query parser, optional LLM narration
jobs.py         cancellable background jobs
positioning.py  manual / survey-wheel / replay position sources
ui/             single-page UI: Dashboard is read-only, Hardware holds plumbing
```

The UI splits along one line worth preserving: **Hardware** owns anything
physical (radios, transports, signal path, components), **Dashboard** is
read-only status, and operating controls that change while you work — centre
frequency, gain — stay on the working pages beside the plots they affect.

## Hardware notes that cost time to learn

- The bench radio is a **Pluto+**. Since the 2026-07-30 reflash it reports as
  **PlutoSDR Rev.C**, `fw_version v0.33-3-gd382-dirty`, kernel 5.4.0, on-device
  libiio 0.21. It previously reported Rev.B with kernel 6.1 and libiio 0.26 —
  if you see Rev.B, the older firmware is back.
- **It runs as AD9361, 2R2T, since 2026-07-31** (`Z7010-AD9361`,
  `ad9361-phy,model: ad9361`). `cf-ad9361-lpc` has **four** channels — two I/Q
  pairs, two real receivers — and `cf-ad9361-dds-core-lpc` carries `TX1_*` and
  `TX2_*` tone generators. It was 1R1T before, when those four were two.
  The board routes all four to SMAs.
  The missing channel was never the firmware or the hardware: it was the u-boot
  environment. `compatible=ad9364` + `mode=1r1t` (plus `attr_name=compatible` /
  `attr_val=ad9364`) made `adi_loadvals` `fdt rm` the
  `adi,2rx-2tx-mode-enable` property and downgrade the TX core on every boot.
  AD9364 is a 1R1T part, so asking for it is what cost the second channel.
  Fixed over SSH without reflashing:

  ```
  fw_setenv attr_name; fw_setenv attr_val      # deleting BOTH is required
  fw_setenv compatible ad9361
  fw_setenv mode 2r2t
  reboot
  ```

  Clearing `attr_name`/`attr_val` is not optional: `adi_loadvals` contains
  `test -n ${attr_val} = ad9364`, a malformed 4-argument u-boot `test` that can
  re-trigger the downgrade *and* `saveenv` `mode=1r1t` back to flash even with
  `compatible=ad9361`. Revert with `compatible ad9364` / `mode 1r1t`. The old
  trap where `ad9361` was silently downgraded to `ad9363a` applied to non-Rev.C
  model strings, so it does not bite here.
  **Channel 2 is an out-of-spec unlock of an AD9363 die and is unproven** — see
  "Where things stand".
- Tuning is **70 MHz – 6 GHz**, RX bandwidth 56 MHz, TX 40 MHz. Under
  `compatible=ad9361` the LO again advertises a **46.875 MHz floor** it does not
  honour, exactly as the pre-2026-07-30 firmware did; the honest
  `[70000000 1 6000000000]` belonged to the AD9364 configuration. Capability
  detection probes the edge rather than trusting the advertised value — cheap,
  and it is what caught the lie both times.
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
### NanoVNA-F V2 (SYSJOINT) — bench VNA, added 2026-07-31

Used to characterise antennas and cables from the Hardware page. Enumerates as
USB `0483:5740` (STM32 CDC-ACM), product string `NanoVnaPro Virtual ComPort`,
**12 Mbit/s full speed** — USB 3 or Type-C buys nothing, and a C-to-A cable
avoids the missing-CC-resistor failure mode that stops some USB-C devices
enumerating on a C-to-C cable. Model reports `NanoVNA-F_V2`, firmware 0.6.2,
50 kHz – 3 GHz.

- `/dev/ttyACM0` is `root:dialout 0660` and the deployment user is **not** in
  dialout. `/etc/udev/rules.d/60-nanovna.rules` grants **plugdev** — which the
  running uvicorn already carries as a supplementary group, so the rule takes
  effect with no restart and no re-login — plus a stable `/dev/nanovna`.
- `0483:5740` is ST's generic virtual COM port, shared with many unrelated
  boards, so `discover()` **probes** with `info` rather than trusting the
  descriptor.
- The acquisition primitive is `scan {start} {stop} {points} 7`: five columns
  (freq, S11 re/im, S21 re/im) in one command, measured 5.7 ms/point. **It
  overwrites the instrument's stored start, stop and point count**, so the
  driver saves and restores them — automation must not silently reconfigure a
  front panel someone is also using by hand.
- **301 points is the ceiling.** Asking for 401 does not clamp; the firmware
  returns a single junk row, which would otherwise arrive as one point wearing
  the label of four hundred. Refused in the driver.
- **The firmware will not tell you what span a calibration was taken over.**
  Bare `cal` reports only which standards are captured
  (`load open short thru cal'ed`), and it silently interpolates a calibration
  onto whatever span is swept. A sweep therefore carries no proof its
  calibration applies. `analyze_thru_residual()` measures the residual against
  a known thru instead: a calibration covering the span holds S21 at 0 dB
  across all of it, and an interpolated one leaves its error concentrated at
  the **band edges**. That asymmetry is the tell.
- The instrument always returns an S21 column even with port 2 open, so a
  one-port antenna sweep must declare `ports=1` or it stores a column of noise
  labelled as insertion loss.
- **Phase is retained** (`s11_ri` / `s21_ri`, complex as `[re, im]`). The first
  cut stored only magnitudes, which meant electrical delay — a phase quantity —
  could not be computed from a saved sweep and the cable had to be re-measured.
- **Electrical delay aliases, and one sweep cannot tell.** Unwrapping assumes
  under π of phase per step, capping the measurable delay at `1/(2·Δf)`. Past
  that, a long cable reports a *short* delay with a **perfectly clean linear
  fit** — 40 ns sampled every 23 MHz reads 3.5 ns at zero residual, with no
  symptom distinguishing it from a genuinely short cable. `analyze_delay()`
  therefore sets `alias_checked: False` and never claims otherwise;
  `delays_agree()` settles it by comparing two sweeps at different point
  counts, which fold differently. `vna_measure_delay()` does that pair and
  writes `nominal_delay_ns` **only** when they agree — a disagreement means
  the true delay is longer than both, so storing the smaller number would be
  inventing one. An early version of this check tested the *measured* delay
  against the limit, which can never fire, because aliasing is precisely what
  makes the measured value small. Do not reintroduce that.
- The instrument reports **conjugate phase** (it rises with frequency), so a
  naive group-delay fit comes out negative. A passive cable cannot advance a
  signal, so the magnitude is the physical answer and the direction is
  recorded as the convention observation it is.
- A thru calibration zeroes whatever was connected during it, so calibrating
  through a jumper subtracts that jumper from every later measurement — about
  1 ns for an 8 in cable, which is ~0.15 m of two-way range. The correction is
  an operator declaration (`reference_plane_ns`), recorded in the component
  notes as an assumption rather than folded in silently.
- The VNA is **not** gated by `SafetyController`. Its source is fixed-level
  (~ −9 dBm), is not operator-controllable, and free-runs whenever the
  instrument is powered, so the TX fingerprint has nothing to bind to. Every
  sweep is written to the safety audit log and its span checked against the
  active frequency profile; an out-of-profile sweep warns and is recorded but
  is not blocked, because a broadband sweep into a load radiates essentially
  nothing. Decided 2026-07-31 — do not quietly upgrade this to a block or
  downgrade it to silence.

- Reflashing re-enumerates the board, which invalidates any open USB handle.
  The adapter keeps reporting `connected: true`; the tell is that `health`
  loses its `temperature_c` field. Disconnect and reconnect to recover.

## Where things stand

**Nothing has ever transmitted.** The receive path is validated on hardware;
the transmit path exists only in simulation. Treat every transmit-derived
figure — including the stepped-frequency resolution results — as a simulation
result. README has the full maturity table; do not let code existing be read
as a physical claim.

The one exception is the **NanoVNA**, whose own source is an instrument
stimulus rather than the platform's transmit path. RF component measurement
(`rfcomponents/nanovna.py`, `/api/vna/*`) is validated end to end on real
hardware: driver, sweep, analysis, and storage were exercised against a
NanoVNA-F V2 measuring real cables on 2026-07-31.

The bench right now: a Pluto+ on Ethernet at `pluto.boblab.net`
(192.168.99.222), reachable and healthy, TX off, disarmed, at 915 MHz /
30.72 MSPS / RX 40 dB / TX −30 dB. The deployment is bound to `0.0.0.0`
deliberately (single-operator lab network, no auth — see docs/DEPLOY.md) and
the UI is at http://192.168.99.124:8347.

**The reported configuration is now reconciled against the radio.** A
watchdog polls every connected device every 15 s and the UI says whether the
settings shown are the ones the radio holds. Read that before trusting a
number off the Dashboard, and see "Keeping the reported configuration honest"
below for what it can and cannot catch.

**The UI is verified in a browser, not only by tests.** Doing that on
2026-08-01 found three defects that the test suite, a JS syntax check and an
element-reference audit had all passed — including a notes field that
silently flattened multi-line text and destroyed measurement provenance on
save. Open the page. The connected browsers are on Windows, so use the LAN
address (`192.168.99.124`), not `127.0.0.1`, which from there is a different
machine entirely.

**In flight:** a passive antenna-directivity measurement. A bare-RX-port
baseline is recorded (experiment `20260731-102906-1e7c3f`: flat −104.7 dBFS
across 2400–2500 MHz, occupancy 0.00 everywhere, which is the reference the
antenna gets compared against). The next steps need the operator to attach the
blue-triangle log-periodic through the 10 ft cable to RX, sweep 2.4 GHz
pointed at a WiFi AP, then rotate 180° and sweep again — the difference is
front-to-back ratio. Receive-only throughout; nothing keys the transmitter.
Watch for clipping at 40 dB RX gain and drop to 30 dB if it appears, because a
clipped sweep is not a measurement.

The feedline (`long skinny cable` in the inventory) was measured on the
NanoVNA on 2026-07-31: **5.7 dB insertion loss at 2.45 GHz** (3.0 dB at
700 MHz, 6.9 dB at 3 GHz) and **14.7 ns electrical delay**. The delay is what
identifies it — 14.7 ns at a velocity factor around 0.70 is 3.09 m, or
**10.1 ft**, and the 8.9–10.5 ft spread across plausible velocity factors
brackets 10 ft. So this is the 10 ft feedline, and the loss figure applies to
the directivity work.

That cuts two ways. Front-to-back ratio is **unaffected**: it is a difference
of two sweeps, so a fixed loss cancels exactly. Any *absolute* comparison
against the −104.7 dBFS bare-port baseline needs **5.7 dB added back**, since
that baseline was recorded with no cable — and the same 5.7 dB raises the
effective noise figure, so confirm the AP still clears the floor before
committing to a rotation run.

The 14.7 ns includes 0.97 ns the operator declared for the calibration
reference plane (the 8 in jumper the thru cal was performed through), which is
an assumption rather than a measurement. The measured figure alone is
13.75 ns, cross-checked at 101 and 301 points.

**The engineering queue**, in the order I would take it. An external review
produced a longer list; these are the parts that survived verification:

1. **Raw retention for stepped sweeps.** `stepped_run` stores only the
   stitched `stepped_profile` derived product — the per-step captures are
   built in a local list and discarded. A stepped run therefore cannot be
   reprocessed or independently checked, which is rule 4 unmet in spirit. Save
   each step as a segment, create the experiment before the sweep, and
   finalize as partial on cancellation.
2. **Persistent safety and calibration state.** Two things live only in memory
   and are silently lost on restart. The **active frequency profile** is the
   urgent one — it reverts to `bench_cabled` (70 MHz – 6 GHz) every start, so
   a profile narrowed for antenna work quietly widens again; that is a safety
   gate reverting without telling anyone. Then `runtime.calibration`, an
   in-memory dict that is simply lost — and which also needs to record what it
   was taken with (waveform, gains, chain, frequency span) and refuse to apply
   against an incompatible configuration, the same way chain configurations
   already do.
   The VNA path now solves the same problem and is the model to copy: a stored
   measurement carries a `calibration` record that stays `known: false` until
   evidence says otherwise, and `analyze_thru_residual()` produces that
   evidence by measurement rather than assertion. Build one abstraction for
   both rather than a second parallel one.
3. **Bistatic geometry.** `imaging/migration.py` uses
   `r = sqrt(z² + (x−x₀)²)` — a one-way slant range from a single point. With
   separate TX and RX antennas the path is TX→target plus target→RX. Only
   bites once two antennas sit at a known separation, which is why it is third.
4. **FMCW timing validation.** The simulator assumes the receive buffer starts
   at a known point in the chirp. Real hardware gives no such guarantee, so
   absolute ranging is unproven. Needs an attenuated loopback and repeated
   captures across TX restarts and retunes.

**RX2 exists but is unproven.** The second receive and transmit channel was
unlocked on 2026-07-31 (see the hardware notes for the u-boot fix). Enumeration
and independence are confirmed — masking RX1 in the AD9361 BIST generator drives
it to numerically zero while RX2 stays at full scale, so `voltage0/1` is RX1,
`voltage2/3` is RX2, and they are genuinely separate streams rather than one
buffer demuxed twice. What is *not* established is RF performance. This is an
out-of-spec unlock of an **AD9363** die: ADI neither specifies nor bins channel
2 on this part, and the Pluto+ channel-2 front end is not ADI's layout.

So do not plan work that assumes RX2 is as good as RX1, and above all do not
assume the two are phase-coherent — a second receive channel only buys a
coherent phase reference if the RX1/RX2 offset is either fixed or re-solvable.
Whether it survives a retune, a gain change or a sample-rate change is exactly
what is unmeasured. The characterization suite is `tools/chancal/`; read its
README before running anything, and note that its results are bench notes, not
platform measurements, because it drives libiio directly and so does not pass
through `SafetyController`.

Two things learned while building it, both worth knowing before you write
capture code against this board:

- **The TX monitor path is unavailable.** `rf_port_select` returns `EINVAL` for
  `TX_MONITOR1`, `TX_MONITOR2` and `TX_MONITOR1_2` on every RX channel, in
  every ENSM state the board accepts, even though the device tree carries the
  `adi,txmon-*` properties. Cause not established; a 2R2T interaction is
  plausible. There is therefore no cable-free way to observe the transmitters.
- **The running deployment configures the hardware radio, so bench tooling and
  the server will fight over it.** `radios.json` registers the Pluto as
  `ip:pluto.boblab.net` and the server applies its own config on connect —
  observed 2026-07-31, a server restart moved the LO to 915 MHz and put RX1
  back in `manual` under a bench script that had just set something else. Two
  writers on one AD9361 means neither can trust a readback. **Stop the server,
  or disable the radio in the UI, before running anything in `tools/chancal/`**,
  and re-connect afterwards so the platform's cached config is not stale
  against hardware someone else has moved.
- **The radio can end up in `ensm_mode = alert`, with its receivers idle, and
  nothing announces it.** Captures still succeed, gain writes are accepted and
  silently ignored, `hardwaregain` reads back values that drift on their own,
  and the DMA returns a static pattern that scores a *perfect* coherence — a
  flawless-looking result from a radio that is not receiving. The tell is a
  `hardwaregain` readback that does not match what you wrote; recovery is
  `ensm_mode = fdd`. Seen once, right after a rejected `ensm_mode` write, but
  with a second writer on the bus at the time the cause is **not established** —
  do not assume your own last write caused it.
- **A persistent RX buffer returns three stale buffers after any attribute
  change.** Measured by switching the BIST mask: refills 1–3 returned the old
  data and only refill 4 reflected the change. `PlutoDevice.receive()` is safe
  because it destroys the buffer before each capture, which flushes the queue
  outright — but anything that holds a buffer across a reconfiguration must
  discard four, or it will record samples the RF config never applied to.

## Keeping the reported configuration honest about the radio

`dev.config` is what was **asked for**. The radio is the authority on what it
**has**, and the two come apart easily: the AD9361 driver clamps and quantizes
silently, AGC overrides a gain the moment you write it, and a bench script or
a second handle moves the board with no notification. `_apply()` writes and
never reads back, so before this existed the UI could only ever show the
request.

- `read_hardware_config()` reads the radio; `sync_status()` compares. Measured
  **~4 ms** for a full read-back over Ethernet, which is why a capture can
  afford to verify rather than assume.
- **Tolerance decides a boolean, never what is displayed.** Quantization is
  not drift — a fractional-N LO landing 2 Hz off 921 MHz is the setting, as
  the hardware can express it. `SYNC_TOLERANCES` in `devices/base.py` holds
  the allowances; the actual value is always reported.
- **`in_sync: None` means "not checked", which is not "fine".** An unreadable
  radio and a healthy one must never render the same, and `status()` carries
  `sync: null` until something has actually looked.
- Only **transitions** are audited. A radio adrift for an hour writing a line
  per poll buries the moment it happened, which is the part worth finding.
- The watchdog starts with the service (`FORGE_VISION_SYNC_INTERVAL`, default
  15 s) and **skips any device whose lock is held**, so it never adds latency
  to a capture.
- `resync_device()` adopts the radio's values rather than re-applying ours —
  re-applying would fight whatever made the change and hide the conflict — and
  then **re-reads**, because adopting cannot fix a TX LO that has stopped
  tracking RX or an AGC mode that reverted. It withdraws TX authorization,
  since permission was granted against a configuration that has moved.
- **Captures record what the radio had, not what it was asked for.** Storing
  the request meant a clamped or externally-changed setting was written into
  the experiment as though it were in force — rule 1 inside stored data, the
  worst place for it. `telemetry.config_verified` says whether the read
  succeeded; `telemetry.config_note` says what disagreed.

**Two settings are accepted, displayed, and silently ignored.**
`rx_channel` and `tx_channel` are validated against `capabilities.rx_channels`
(the adapter still reports 1, so only 0 passes) but `_apply()` hardcodes
`chan0` for gain and AGC mode, and `receive()` never consults them. Harmless
today. **The moment the adapter exposes the 2R2T channels, setting
`rx_channel = 1` will validate, display as RX2, and change nothing** — the
exact shape of bug the sync work above exists to prevent, but invisible to it
because the hardware never moves. Wire those through `_apply()` and
`receive()` in the same change that raises the channel count.

## Gotchas

- **The active frequency profile does not survive a restart.** `SafetyLimits()`
  is constructed with its defaults in `Runtime.__init__` and nothing persists
  `active_profile`, so every start silently returns to **`bench_cabled`
  (70 MHz – 6 GHz)** — the closed-circuit profile. Observed on 2026-07-31:
  the profile was deliberately narrowed to `ism_conservative` because antennas
  had gone on the bench, and a later service restart widened it back with no
  notice. Check `safety.limits.active_profile` in `/api/status` after any
  restart, and re-set it before radiating. This is the same in-memory-state
  defect as engineering-queue item 2 (`runtime.calibration`) and wants the
  same fix; the profile is arguably the more urgent of the two, because it is
  a safety gate rather than a correction.

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
