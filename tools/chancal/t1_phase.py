#!/usr/bin/env python3
"""T1.1 - the RX1/RX2 phase-coherence ladder. This is the decisive test.

Two-channel GPR does not need RX2 to be *good*, it needs RX2 to be
*predictable relative to RX1*. A fixed 37.4 degree offset is harmless - you
subtract it. An offset that reshuffles every time you retune is fatal unless
you can inject a reference and re-solve it on every retune.

So the question is not "what is the phase offset" but **"what is the smallest
disturbance that changes it"**. This walks a ladder of increasingly violent
disturbances and measures, at each rung, how far the RX1-RX2 phase moved:

    repeat        nothing at all               <- the measurement noise floor
    rebuffer      DMA buffer torn down/rebuilt
    gain_both     both gains changed and put back
    gain_rx1      one gain changed and put back <- gain-dependent phase
    bandwidth     RF bandwidth changed and back
    retune        RX LO moved away and back     <- the one that usually bites
    rate          sample rate changed and back  <- BBPLL/divider re-lock
    reinit        context closed and reopened

The lowest rung that shows real movement sets your calibration cadence. If
`retune` moves and `gain_both` does not, you may retune freely within a scan
but must re-solve the offset whenever you change centre frequency.

Anything measured here is only as good as the source: **one tone, split, into
both RX ports at the same instant.** Sequential measurements cannot measure
coherence at all, no matter how carefully the antennas are matched.

    # external signal generator into a 2-way splitter
    .venv/bin/python tools/chancal/t1_phase.py

    # on-board TX1 through a >=30 dB pad into the splitter (keys the TX)
    .venv/bin/python tools/chancal/t1_phase.py --source internal \\
        --tx-confirm conducted-into-attenuator
"""

from __future__ import annotations

import argparse
import time

import common
import txtone

# what "moved" means, in degrees of RX1-RX2 phase
STEADY_DEG = 2.0        # below this: treat as fixed, calibrate once
MARGINAL_DEG = 10.0     # above this: must be re-solved after that operation


def apply_rx_config(radio: common.Radio, args) -> None:
    radio.set_sample_rate(args.rate_hz)
    radio.set_rx_bandwidth(args.bandwidth_hz)
    radio.set_rx_lo(args.lo_hz)
    radio.manual_gain_both(args.gain_db)


def measure(radio: common.Radio, args) -> dict:
    rx1, rx2 = radio.capture(args.samples)
    return common.pair_phase(rx1, rx2, radio.sample_rate, wideband=args.wideband)


# -- the rungs ------------------------------------------------------------


def p_repeat(radio, args):
    return "nothing"


def p_rebuffer(radio, args):
    radio.drop_buffer()
    return "DMA buffer destroyed and rebuilt"


def p_gain_both(radio, args):
    other = 20.0 if args.gain_db > 40.0 else 60.0
    radio.manual_gain_both(other)
    radio.manual_gain_both(args.gain_db)
    return f"both gains -> {other:.0f} dB -> {args.gain_db:.0f} dB"


def p_gain_rx1(radio, args):
    other = 20.0 if args.gain_db > 40.0 else 60.0
    radio.set_rx_gain(0, other)
    radio.set_rx_gain(0, args.gain_db)
    return f"RX1 gain only -> {other:.0f} dB -> {args.gain_db:.0f} dB"


def p_bandwidth(radio, args):
    other = 10e6 if args.bandwidth_hz > 12e6 else 30e6
    radio.set_rx_bandwidth(other)
    radio.set_rx_bandwidth(args.bandwidth_hz)
    return f"RF bandwidth -> {other/1e6:.0f} MHz -> {args.bandwidth_hz/1e6:.0f} MHz"


def p_retune(radio, args):
    away = args.lo_hz - 100e6 if args.lo_hz > 500e6 else args.lo_hz + 100e6
    radio.set_rx_lo(away)
    radio.set_rx_lo(args.lo_hz)
    return f"RX LO -> {away/1e6:.0f} MHz -> {args.lo_hz/1e6:.0f} MHz"


def p_rate(radio, args):
    other = 15.36e6 if args.rate_hz > 20e6 else 30.72e6
    radio.set_sample_rate(other)
    radio.drop_buffer()
    radio.set_sample_rate(args.rate_hz)
    radio.set_rx_bandwidth(args.bandwidth_hz)
    radio.drop_buffer()
    return f"sample rate -> {other/1e6:.2f} -> {args.rate_hz/1e6:.2f} MSPS"


RUNGS = [
    ("repeat", p_repeat),
    ("rebuffer", p_rebuffer),
    ("gain_both", p_gain_both),
    ("gain_rx1", p_gain_rx1),
    ("bandwidth", p_bandwidth),
    ("retune", p_retune),
    ("rate", p_rate),
    # reinit is handled separately: it replaces the context object
]


def run_rung(radio, args, name, fn, tone) -> dict:
    phases, coherences, amps, note = [], [], [], ""
    for _ in range(args.trials):
        note = fn(radio, args)
        m = measure(radio, args)
        if max(m["rx1_clip"], m["rx2_clip"]) > 1e-4:
            print(f"    !! clipping ({m['rx1_clip']:.4f}/{m['rx2_clip']:.4f}) - "
                  "lower RX gain or add attenuation; this reading is not a measurement")
        phases.append(m["phase_deg"])
        coherences.append(m["coherence"])
        amps.append(m["amp_ratio_db"])
    mean = common.circ_mean(phases)
    std = common.circ_std(phases)
    spread = max(abs(common.wrap180(p - mean)) for p in phases)
    verdict = ("steady" if std < STEADY_DEG else
               "marginal" if std < MARGINAL_DEG else "MOVES")
    print(f"  {name:<10} {mean:+8.2f} deg  std {std:6.2f}  worst {spread:6.2f}  "
          f"coh {min(coherences):.4f}  A1/A2 {sum(amps)/len(amps):+.2f} dB  [{verdict}]")
    print(f"             ({note})")
    return {"perturbation": note, "phases_deg": phases, "mean_deg": mean,
            "std_deg": std, "worst_deviation_deg": spread,
            "min_coherence": min(coherences),
            "mean_amp_ratio_db": sum(amps) / len(amps), "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default=common.DEFAULT_URI)
    ap.add_argument("--lo-hz", type=float, default=2.45e9)
    ap.add_argument("--offset-hz", type=float, default=2e6,
                    help="tone offset from LO; keep it off DC and off fs/2")
    ap.add_argument("--rate-hz", type=float, default=30.72e6)
    ap.add_argument("--bandwidth-hz", type=float, default=18e6)
    ap.add_argument("--gain-db", type=float, default=30.0)
    ap.add_argument("--samples", type=int, default=1 << 16)
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--source", choices=("external", "internal"), default="external",
                    help="external: your own generator into a splitter. "
                         "internal: on-board TX1 through a pad (keys the TX)")
    ap.add_argument("--tx-confirm", default="", help="required for --source internal")
    ap.add_argument("--soak-minutes", type=float, default=0.0,
                    help="after the ladder, hold still and watch thermal drift")
    ap.add_argument("--wideband", action="store_true",
                    help="integrate the cross-spectrum over a band instead of one "
                         "bin, so an ambient broadcast carrier through two antennas "
                         "can drive the ladder when no splitter is available. "
                         "A screening test - see the README")
    args = ap.parse_args()

    radio = common.Radio(args.uri)
    radio.assert_2r2t()
    radio.assert_running()
    apply_rx_config(radio, args)
    common.banner("T1.1 phase-coherence ladder", radio)

    report = {"provenance": common.provenance(radio),
              "settings": {k: v for k, v in vars(args).items() if k != "tx_confirm"},
              "rungs": {}}

    tone = None
    if args.source == "internal":
        radio.set_tx_lo(args.lo_hz)
        tone = txtone.Tone(radio, chan=1, offset_hz=args.offset_hz,
                           confirm=args.tx_confirm)

    ctx = tone if tone is not None else _null()
    with ctx:
        first = measure(radio, args)
        print(f"\n  baseline: tone at {first['tone_freq_hz']/1e6:+.4f} MHz, "
              f"RX1 {first['rx1_level_dbfs']:.1f} dBFS, "
              f"RX2 {first['rx2_level_dbfs']:.1f} dBFS, "
              f"coherence {first['coherence']:.4f}")
        if min(first["rx1_level_dbfs"], first["rx2_level_dbfs"]) < -70:
            print("  !! the tone is barely above the floor on at least one channel.")
            print("     Fix the source level before reading anything below.")
        bar = VALID_COHERENCE_WIDEBAND if args.wideband else VALID_COHERENCE
        if first["coherence"] < bar:
            print(f"  !! coherence below {bar} within a single capture - the phase")
            print("     is not even stable across one buffer. Investigate that first.")
        report["baseline"] = first

        print(f"\n  {'rung':<10} {'mean phase':>12}  {'std':>9}  {'worst':>10}  "
              f"{'min coh':>8}")
        for name, fn in RUNGS:
            report["rungs"][name] = run_rung(radio, args, name, fn, tone)

        # reinit replaces the context, so it cannot be a plain perturbation fn
        print()
        phases = []
        for _ in range(max(2, args.trials // 2)):
            radio.close()
            radio = common.Radio(args.uri)
            if tone is not None:
                tone.radio = radio          # DDS state lives in the device, not here
            apply_rx_config(radio, args)
            phases.append(measure(radio, args)["phase_deg"])
        mean, std = common.circ_mean(phases), common.circ_std(phases)
        verdict = ("steady" if std < STEADY_DEG else
                   "marginal" if std < MARGINAL_DEG else "MOVES")
        print(f"  {'reinit':<10} {mean:+8.2f} deg  std {std:6.2f}  [{verdict}]")
        print("             (libiio context closed and reopened, config reapplied)")
        report["rungs"]["reinit"] = {"phases_deg": phases, "mean_deg": mean,
                                     "std_deg": std, "verdict": verdict,
                                     "perturbation": "context close/reopen"}

        if args.soak_minutes > 0:
            print(f"\n  soak: holding still for {args.soak_minutes:.0f} min "
                  "(thermal drift)")
            apply_rx_config(radio, args)
            t0 = time.time()
            soak = []
            while time.time() - t0 < args.soak_minutes * 60:
                m = measure(radio, args)
                soak.append({"t_s": time.time() - t0, "phase_deg": m["phase_deg"],
                             "coherence": m["coherence"],
                             "rx1_dbfs": m["rx1_level_dbfs"],
                             "rx2_dbfs": m["rx2_level_dbfs"]})
                print(f"    t={soak[-1]['t_s']:6.0f}s  phase {m['phase_deg']:+8.2f} deg"
                      f"  coh {m['coherence']:.4f}")
                time.sleep(10)
            drift = common.wrap180(soak[-1]["phase_deg"] - soak[0]["phase_deg"])
            print(f"    drift over {args.soak_minutes:.0f} min: {drift:+.2f} deg")
            report["soak"] = {"samples": soak, "drift_deg": drift}

    _interpret(report)
    common.save("t1-phase", report)
    return 0


# Below this, the "phase" is the argument of a noise peak and means nothing.
#
# The wideband bar is lower on purpose, and it is not arbitrary. Measured with
# the estimator fed pure independent noise, wideband coherence floors at about
# 0.25 rather than 0 - averaging 64 bins leaves a residual, and each block
# picking its own strongest bins biases it upward. Real off-air pickup common
# to both channels reads 1.00. 0.5 sits clear of the noise floor while still
# accepting a source that is only somewhat coherent, which is all the ladder
# needs to answer "did the offset move".
VALID_COHERENCE = 0.9
VALID_COHERENCE_WIDEBAND = 0.5


def _interpret(report: dict) -> None:
    print("\n  --- what this means ---")
    order = ["repeat", "rebuffer", "gain_both", "gain_rx1", "bandwidth",
             "retune", "rate", "reinit"]
    wideband = report.get("settings", {}).get("wideband", False)
    limit = VALID_COHERENCE_WIDEBAND if wideband else VALID_COHERENCE
    if wideband:
        print("  Wideband screening run: an ambient source through two antennas")
        print("  carries the room's multipath as well as the radio's behaviour, so")
        print("  treat a moving rung as 'worth a conducted measurement', not proof.")

    # Refuse to draw a conclusion from an invalid measurement. Without a real
    # common tone every rung reads "MOVES", which looks like a damning result
    # and is actually just noise. Reporting it as a finding would be exactly
    # the failure this codebase forbids: inference presented as measurement.
    coh = [r["min_coherence"] for r in report["rungs"].values()
           if "min_coherence" in r]
    base = report.get("baseline", {}).get("coherence", 0.0)
    if not coh or min(coh + [base]) < limit:
        print(f"  NO CONCLUSION. Worst coherence {min(coh + [base]):.3f} is below "
              f"{limit}, so the")
        print("  phase readings above are the argument of a noise peak, not a")
        print("  measurement of the receivers. Every rung will read MOVES and that")
        print("  means nothing. Get a real tone into both ports - check the source is")
        print("  on, the splitter is connected, and both channels see it well above")
        print("  the floor - then run this again.")
        report["valid"] = False
        return
    report["valid"] = True

    floor = report["rungs"].get("repeat", {}).get("std_deg")
    if floor is not None:
        print(f"  Measurement noise floor (repeat): {floor:.2f} deg. Nothing below")
        print("  this is a finding, no matter how suggestive it looks.")
    moved = [n for n in order
             if report["rungs"].get(n, {}).get("verdict") in ("marginal", "MOVES")]
    steady = [n for n in order if n not in moved and n in report["rungs"]]
    if steady:
        print(f"  Survives unchanged: {', '.join(steady)}")
    if not moved:
        print("  Nothing on the ladder moved the offset. A single calibration")
        print("  constant, written to the RX calibphase attributes, is enough.")
    else:
        print(f"  Disturbed by: {', '.join(moved)}")
        print(f"  -> re-solve the RX1/RX2 offset after '{moved[0]}' and anything")
        print("     above it. For GPR that means a reference-injection path, not a")
        print("     constant baked into a config file.")
    print("  Correction knob: cf-ad9361-lpc voltage2/3 `calibphase` and `calibscale`")
    print("  apply the fix in the FPGA, before the data ever reaches the host.")


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
