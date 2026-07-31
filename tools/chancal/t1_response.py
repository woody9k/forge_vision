#!/usr/bin/env python3
"""T1.3/T1.4 - gain, phase and image-rejection parity across the band,
with a cable swap so cable differences do not get charged to the channel.

The trap this exists to avoid: measure RX2 minus RX1 once and you have measured
`(channel difference) + (cable difference) + (splitter port difference)`. On a
bench with ordinary SMA cables the second and third terms are easily larger
than the first, especially above 3 GHz. A 1.4 dB "channel 2 is worse" result
that is really a tired cable will send you chasing silicon that is fine.

The fix is the standard swap. With the two splitter outputs feeding RX1 and
RX2, measure; then exchange those two cables at the *receiver* end and measure
again:

    orientation A:  M_A = (C2 - C1) + (Q - P)
    orientation B:  M_B = (C2 - C1) - (Q - P)

so the channel difference is (M_A + M_B)/2 and the cable+port difference, which
you get for free, is (M_A - M_B)/2. If that second term is comparable to the
first, your cabling - not channel 2 - is the limiting factor, and the honest
report says so.

    .venv/bin/python tools/chancal/t1_response.py            # external source
    .venv/bin/python tools/chancal/t1_response.py --source internal \\
        --tx-confirm conducted-into-attenuator
"""

from __future__ import annotations

import argparse

import common
import txtone

DEFAULT_FREQS_MHZ = (70, 100, 200, 433, 915, 1575, 2450, 3500, 4500, 5800, 6000)


def sweep(radio, args, tone) -> list[dict]:
    rows = []
    print(f"  {'LO MHz':>9} {'RX1 dBFS':>10} {'RX2 dBFS':>10} {'d(2-1)':>8} "
          f"{'phase':>9} {'IR1 dB':>8} {'IR2 dB':>8} {'coh':>7}")
    for mhz in args.freqs_mhz:
        try:
            lo = radio.set_rx_lo(mhz * 1e6)
            if tone is not None:
                radio.set_tx_lo(mhz * 1e6)
        except common.AttrMismatch as exc:
            print(f"  {mhz:9d}  refused: {exc}")
            rows.append({"lo_mhz": mhz, "refused": str(exc)})
            continue
        rx1, rx2 = radio.capture(args.samples)
        m = common.pair_phase(rx1, rx2, radio.sample_rate)
        ir1 = common.image_rejection_db(rx1, radio.sample_rate)
        ir2 = common.image_rejection_db(rx2, radio.sample_rate)
        clipped = max(m["rx1_clip"], m["rx2_clip"]) > 1e-4
        weak = min(m["rx1_level_dbfs"], m["rx2_level_dbfs"]) < -70
        flag = " CLIP" if clipped else (" WEAK" if weak else "")
        print(f"  {lo/1e6:9.2f} {m['rx1_level_dbfs']:10.2f} {m['rx2_level_dbfs']:10.2f} "
              f"{-m['amp_ratio_db']:+8.2f} {m['phase_deg']:+9.2f} {ir1:8.1f} {ir2:8.1f} "
              f"{m['coherence']:7.4f}{flag}")
        rows.append({"lo_mhz": lo / 1e6,
                     "rx1_dbfs": m["rx1_level_dbfs"], "rx2_dbfs": m["rx2_level_dbfs"],
                     "delta_db": -m["amp_ratio_db"],   # RX2 minus RX1
                     "phase_deg": m["phase_deg"],
                     "image_rejection_db": [ir1, ir2],
                     "coherence": m["coherence"],
                     "clipped": clipped, "weak": weak})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default=common.DEFAULT_URI)
    ap.add_argument("--offset-hz", type=float, default=2e6)
    ap.add_argument("--gain-db", type=float, default=30.0)
    ap.add_argument("--samples", type=int, default=1 << 16)
    ap.add_argument("--freqs-mhz", type=int, nargs="+", default=list(DEFAULT_FREQS_MHZ))
    ap.add_argument("--source", choices=("external", "internal"), default="external")
    ap.add_argument("--tx-confirm", default="")
    ap.add_argument("--no-swap", action="store_true",
                    help="single orientation only; channel and cable stay confounded")
    args = ap.parse_args()

    radio = common.Radio(args.uri)
    radio.assert_2r2t()
    radio.assert_running()
    radio.set_rx_bandwidth(18e6)
    radio.manual_gain_both(args.gain_db)
    common.banner("T1.3/T1.4 band response parity", radio)

    tone = None
    if args.source == "internal":
        tone = txtone.Tone(radio, chan=1, offset_hz=args.offset_hz,
                           confirm=args.tx_confirm)
    ctx = tone if tone is not None else _null()

    report = {"provenance": common.provenance(radio),
              "settings": {k: v for k, v in vars(args).items() if k != "tx_confirm"}}

    with ctx:
        print("\n  orientation A (splitter port P -> RX1, port Q -> RX2)")
        a = sweep(radio, args, tone)
        report["orientation_a"] = a

        if args.no_swap:
            print("\n  --no-swap: channel and cable differences remain confounded.")
            common.save("t1-response", report)
            return 0

        print("\n  Now EXCHANGE the two cables at the receiver end: the cable that")
        print("  was on RX1 goes to RX2 and vice versa. Leave everything else alone.")
        input("  Press Enter when swapped... ")

        print("\n  orientation B (port Q -> RX1, port P -> RX2)")
        b = sweep(radio, args, tone)
        report["orientation_b"] = b

    print("\n  --- swap-corrected ---")
    print(f"  {'LO MHz':>9} {'channel d':>11} {'cable d':>10} {'ch phase':>10} "
          f"{'cable phase':>12}")
    combined = []
    by_f = {r["lo_mhz"]: r for r in b if "delta_db" in r}
    for ra in a:
        rb = by_f.get(ra.get("lo_mhz"))
        if rb is None or "delta_db" not in ra:
            continue
        chan_db = (ra["delta_db"] + rb["delta_db"]) / 2
        cable_db = (ra["delta_db"] - rb["delta_db"]) / 2
        chan_ph = common.circ_mean([ra["phase_deg"], rb["phase_deg"]])
        cable_ph = common.wrap180((ra["phase_deg"] - rb["phase_deg"]) / 2)
        print(f"  {ra['lo_mhz']:9.2f} {chan_db:+11.2f} {cable_db:+10.2f} "
              f"{chan_ph:+10.2f} {cable_ph:+12.2f}")
        combined.append({"lo_mhz": ra["lo_mhz"], "channel_delta_db": chan_db,
                         "cable_delta_db": cable_db,
                         "channel_phase_deg": chan_ph, "cable_phase_deg": cable_ph})
    report["swap_corrected"] = combined

    if combined:
        worst = max(combined, key=lambda r: abs(r["channel_delta_db"]))
        cable = max(abs(r["cable_delta_db"]) for r in combined)
        print(f"\n  Worst channel difference: {worst['channel_delta_db']:+.2f} dB at "
              f"{worst['lo_mhz']:.0f} MHz")
        print(f"  Largest cable/port difference seen: {cable:.2f} dB")
        if cable >= abs(worst["channel_delta_db"]):
            print("  The cabling is at least as large as the effect being measured.")
            print("  Report the channel number with that caveat, or improve the cables")
            print("  before quoting it - do not present it as a clean channel result.")
        ir2 = [min(r["image_rejection_db"]) for r in a if "image_rejection_db" in r]
        if ir2:
            print(f"  Worst image rejection anywhere in the sweep: {min(ir2):.1f} dB")
            print("  Quadrature error is the most sensitive tell of a badly matched")
            print("  front end; if RX2's is much worse than RX1's, that is a real find.")
        report["summary"] = {"worst_channel_delta": worst,
                             "max_cable_delta_db": cable}

    common.save("t1-response", report)
    return 0


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
