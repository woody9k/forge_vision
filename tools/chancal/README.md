# Characterizing the second RX/TX channel

On 2026-07-31 the bench Pluto+ was switched from 1R1T to 2R2T by correcting the
u-boot environment (`compatible=ad9361`, `mode=2r2t`, `attr_name`/`attr_val`
cleared). Both receivers and both transmitters now enumerate and the board
routes all four to SMAs.

That is an **out-of-spec unlock of an AD9363 die**. ADI neither specifies nor
bins channel 2 on this part, and the Pluto+ channel-2 RF front end is not ADI's
layout. Until it is measured, channel 2 is an untested claim — and this
codebase does not present inferred data as measured (rule 1). These scripts
turn the claim into numbers.

## What the numbers are for

Ranked by what would actually stop the GPR work:

1. **RX1/RX2 phase coherence.** The entire reason two receivers are worth
   having. If the relative phase is a fixed constant, subtract it and move on.
   If it reshuffles on every retune, you need a reference-injection path before
   any array or interferometric processing is honest.
2. **TX/RX isolation.** Bistatic GPR has a transmitter and a receiver live at
   the same instant. On-chip and on-board leakage sets the direct-coupling
   floor, which sets the minimum usable range.
3. **Gain and noise-figure parity.** Decides whether one calibration can cover
   both channels or each needs its own.
4. **Image rejection.** The most sensitive tell of a poorly matched front end,
   and it caps dynamic range for weak reflectors.

## Methodology, and one correction to the obvious plan

The natural instinct — put an identical antenna setup on channel 1, run a
suite, move the same physical antennas to channel 2, repeat — controls the
right variable but cannot do the job:

* **Sequential measurement cannot measure coherence at all.** Relative phase is
  a property of two receivers observing the same wavefront *at the same
  instant*. There is no way to recover it from two runs taken minutes apart.
  Phase work needs one source split into both ports simultaneously.
* **Over the air, the room is louder than the effect.** Antenna VSWR,
  multipath, and a few millimetres of position error swamp the 1–2 dB and
  few-degree differences under investigation. Conducted first; over the air only
  at the end, as confirmation.
* **A single swap confounds channel with cable.** Measuring RX2−RX1 once gives
  `(channel) + (cable) + (splitter port)`. Do it in both cable orientations and
  the average is the channel, the half-difference is the cabling —
  `t1_response.py` does this and reports both.
* **Set the measurement noise floor before quoting any difference.** Disconnect
  and reconnect the same cable and re-measure. Any channel-2-versus-1 delta
  smaller than that repeatability is not a finding.

Two hardware rules that make or break every phase number:

* **AGC off, manual gain, always.** Two independent AGCs step at different
  instants, and an AD9361 gain step carries a phase step with it. An AGC-on
  phase measurement measures the AGC.
* **Both receivers must come out of one DMA buffer.** They share the sample
  clock and are demuxed from a single interleaved stream. `common.Radio.capture`
  does this; two separate captures would not be comparable.

## The tests

### Tier 0 — no external hardware beyond two 50 Ω terminators

| script | what it settles |
|---|---|
| `t0_parity.py` | Every attribute of channel 1 versus channel 2. Rules the *driver* out as the cause of any later asymmetry. Needs nothing. |
| `t0_bist.py` | Injects the AD9361 internal BIST tone and confirms `voltage0/1` really is RX1's I/Q and `voltage2/3` really is RX2's. Wrong pairing collapses image rejection, and would make every later number meaningless. Needs SSH. |
| `t0_noise.py` | Terminated noise floor versus gain (the gain law), the gain-independence swap, and a relative noise-figure curve from 70 MHz to 6 GHz. Needs two 50 Ω terminators. |

Parallel gain curves that are offset = a calibratable constant. Curves that
*diverge* = different gain tables, which you must not calibrate away.

### What the bench actually has

No splitter, and a **NanoVNA-F V2** (50 kHz – 3 GHz). That changes the order of
work more than it blocks it.

**The VNA covers things the SDR cannot.** Point it at the Pluto's own SMA ports
and measure **S11 of RX1 versus RX2**, and of TX1 versus TX2. This is a direct,
passive comparison of the two front-end matches — precisely where a non-ADI
channel-2 layout is most likely to differ, and it needs no splitter, no
transmit, and no code in this directory. Do it with the Pluto powered and in a
defined state (`fdd`, manual gain, LO parked), because a receiver's input match
depends on what the front end is doing. Ceiling is 3 GHz, so this covers
70 MHz–3 GHz and says nothing about the top half of the band.

The platform already drives this instrument — `rfcomponents/nanovna.py` and
`POST /api/vna/sweep`, merged 2026-07-31 — so run it through the API and store
each sweep against a component rather than scripting the serial port again
here. Two things that work carries which matter for this measurement: a sweep's
calibration provenance stays `known: false` until `calibration_check`
establishes the calibration actually covers the span, and the instrument
happily interpolates a calibration onto a span it was never taken over. Do the
residual check before comparing RX1 against RX2, or the difference you find may
be the calibration rather than the board. Use the same mechanism to sweep every
cable and attenuator in the Tier 1 path (`measure_delay` handles the aliasing
trap) so the path is measured rather than assumed.

One caution from the platform's own guidance: a VNA sweep is **not** behind the
transmit interlock, because there is no waveform or gain for a TX fingerprint
to bind to. Into a load, a cable, or a Pluto's RX port it is a closed circuit.
Into an antenna it is emission. Know what is on the port before sweeping.

**The VNA can also be the CW source** for Tier 1 once a splitter exists, in
place of `--source internal`. Worth preferring: it means the whole RX
characterization completes with the Pluto never transmitting, which defers the
first-ever-TX milestone until it is actually needed. Its source is fixed-level
(about −9 dBm) and free-running whenever the instrument is powered, which is
exactly what a phase reference wants. Drive it to a single frequency (start =
stop) for a continuous tone. If it retunes or pulses between sweep points the
coherence number collapses and tells you immediately — that metric is precisely
the diagnostic for "is my source clean enough".

**What still needs a splitter: the phase ladder.** There is no way around it —
coherence is a property of two receivers fed the same signal at the same
instant. A resistive 2-way power divider is the right tool (~$20–40). A plain
SMA T-piece is also acceptable *for the ladder specifically*, and that is worth
understanding rather than taking on faith: a T is a bad splitter because its
mismatch makes the two paths unequal and frequency-dependent. But the ladder
does not ask "what is the offset", it asks "**does the offset move**". A T's
error is fixed, so it cancels out of that question entirely. Do not use a T for
`t1_response.py`, where the absolute balance is the measurement.

**Meanwhile, `--wideband` screens without any of it.** It integrates the
cross-spectrum over a band instead of a single bin, so an ambient broadcast
carrier reaching both receivers can drive the ladder. Two antennas is the
intended use; on this bench even the bare SMA connectors pick up enough FM at
98 MHz. Verified on 2026-07-31: coherence 1.0000, and every rung — including
retune, sample-rate change and full context reinit — held to about 0.1 degrees.
That is encouraging and is what the silicon should do, since both receivers
share one LO and one BBPLL. It is **not** a measurement: it is open-connector
pickup at one frequency in one session with no thermal soak, and a common
signal arriving by a shared path is not the same as a controlled split. Treat
it as "the conducted test is worth doing and will probably pass".

### Tier 1 — needs a 2-way splitter, two attenuators, two cables

Source: either an external signal generator, or the board's own TX1 through a
≥30 dB pad. The on-board DDS is a legitimate source here even though TX and RX
run on separate PLLs: they share the 40 MHz reference, so the TX→RX phase
wander is **common to both receivers** and cancels exactly in RX1−RX2, which is
the only quantity these scripts quote.

| script | what it settles |
|---|---|
| `t1_phase.py` | **The decisive test.** Walks a ladder of disturbances — repeat, rebuffer, gain change, one-channel gain change, bandwidth, LO retune, sample rate, context reinit — and measures how far the RX1−RX2 phase moved at each rung. The lowest rung that moves sets your calibration cadence. `--soak-minutes` adds a thermal-drift run. |
| `t1_response.py` | Level, phase and image rejection across 70 MHz–6 GHz, with the cable swap, so the channel difference and the cable difference are reported separately. |

The correction knob, once you know the offset: `cf-ad9361-lpc` exposes
`calibphase` and `calibscale` per channel, applied in the FPGA before the data
reaches the host.

Still to add at this tier: a 1 dB compression comparison (raise drive until
each channel compresses, compare thresholds). Straightforward once a calibrated
source level exists.

### Tier 2 — transmit

`t2_tx.py --mode conducted | isolation`.

`--mode monitor` would have answered "does TX2 emit anything at all" with no
cables, but the **TX monitor path is unavailable on this firmware**: the driver
returns `EINVAL` for `TX_MONITOR1`, `TX_MONITOR2` and `TX_MONITOR1_2` on every
RX channel, in every ENSM state the board accepts, despite the device tree
carrying the `adi,txmon-*` properties. Cause not established; a 2R2T
interaction is plausible since the monitor multiplexes onto RX inputs that are
now both live. The mode is kept because it costs nothing to retry after a
firmware change, and it reports the refusal instead of crashing.

## Gotchas that cost time here

* **Stop the deployment before running any of this.** `radios.json` registers
  the bench Pluto as `ip:pluto.boblab.net` and the Forge Vision server applies
  its own config when it connects — observed 2026-07-31, a server restart moved
  the LO to 915 MHz and put RX1 back in `manual` underneath a bench script.
  Two writers on one AD9361 means no readback can be trusted and no result is
  reproducible. `deploy/forge-vision stop`, or disable the radio in the UI, then
  re-connect afterwards so the platform is not left holding a cached config that
  no longer matches the hardware.
* **The radio can end up in `ensm_mode = alert`, receivers idle, silently.**
  Captures still succeed, gain writes are accepted and ignored, `hardwaregain`
  reads back values that drift on their own, and the DMA returns a static
  pattern scoring a *perfect* 1.0000 coherence. A flawless-looking result from
  a radio that is not receiving is the worst failure mode in this suite, which
  is why every script now calls `Radio.assert_running()` first. Recovery is
  `ensm_mode = fdd`. Seen once immediately after a rejected `ensm_mode` write —
  but there was a second writer on the bus at the time, so the cause is **not
  established**. Treat it as something to detect, not something you can avoid
  by being careful with your own writes.
* **The RX gain table is 1 dB steps and not aligned to round numbers** — ask
  for 0 dB and you get −1 dB. That is quantization, not a clamp.
* **The gain ceiling is per-band**: `hardwaregain_available` reads
  `[-1 1 73]` at 98 MHz, `[-3 1 71]` at 2.45 GHz and `[-10 1 62]` at 5.8 GHz.
  Read it after retuning rather than assuming 71.
* **`iio_attr -c ad9361-phy voltage0` is ambiguous** — it matches both the
  input and the output channel, so an RX gain write can silently land on the
  TX attenuator (range [−89.75, 0]) and fail with `EINVAL`. Specify the
  direction, or use these scripts, which always do.

### Tier 3 — over the air

Only after the conducted numbers exist, and only to confirm that the antenna
plus channel behaves as the conducted numbers predict. If it does not, the
conducted numbers are right and the antenna setup is the variable.

## Transmit safety

**Nothing in this project has ever transmitted.** Tier 2 changes that, so:

* TX goes into an **attenuator and a load or a receiver — never an antenna.**
* TX gain starts at −40 dB and `txtone.py` refuses anything above −20 dB from
  the command line.
* Every TX path is a `with` block; both transmitters are silenced and
  attenuated to −89.75 dB in a `finally`, and `t2_tx.py` repeats that at the
  outermost level in case something dies in between. The `with` block alone
  does **not** cover a failure while the tone is being brought up — Python
  calls `__exit__` only if `__enter__` returns — so `Tone.__enter__` shuts
  down for itself before re-raising. That path is reachable here rather than
  theoretical: `Radio.set` writes before it verifies, and in `ensm_mode =
  alert` this board accepts gain writes, ignores them, and lets `hardwaregain`
  readback drift, which is exactly what raises `AttrMismatch` *after* the gain
  has landed. Pinned by `tests/test_chancal_txgate.py`.
* The default LO is 2.45 GHz — inside an ISM band, so even a leak is benign.
  If you sweep the full 70 MHz–6 GHz range, keep the drive at minimum.
* These scripts talk to libiio directly and therefore **do not** pass through
  the platform's `SafetyController`. That is acceptable for a bench
  characterization into a load; it is not acceptable for anything the platform
  records. **Results from here are bench notes, not measured experiments** —
  do not enter them into Forge Vision as measurements until the platform's own
  TX path, with its interlock and TX-authorization fingerprint, is what keyed
  the radio (rule 5).

## Running

```bash
.venv/bin/python tools/chancal/t0_parity.py
```

`numpy` is the only dependency and the project venv has it. Results are written
as JSON to `tools/chancal/results/`, each stamped with the firmware version,
model string, sample rate and gains that produced them — a characterization
number that does not say what it was taken on cannot be compared with the next
one. Override the radio with `--uri` or `CHANCAL_URI`.
