#!/usr/bin/env python3
"""T0.3/T0.4 - terminated noise floor, gain law, and noise-figure proxy.

Needs two 50 ohm SMA terminators (RX1 and RX2) and nothing else. No transmit.

Three things come out of it:

* **Gain law.** Noise floor versus commanded gain, per channel. Both curves
  should rise ~1 dB per commanded dB and sit within about a decibel of each
  other. Curves that are offset but parallel mean a fixed gain error you can
  calibrate; curves that are *not* parallel mean the two channels are using
  different gain tables, which you cannot.
* **Gain independence.** Gains swapped 20/60 -> 60/20; each channel's floor
  must follow its own commanded gain. This is the check that distinguishes two
  real receivers from one buffer presented twice.
* **Noise-figure proxy.** With the input terminated, the noise floor at fixed
  high gain versus frequency is a relative noise-figure curve. This is where an
  unbinned second channel is most likely to disappoint, and it costs one sweep.

A terminated input is not optional. An open SMA is a reflective, antenna-ish
load that picks up ambient signal, and the number you get back is the room.

    .venv/bin/python tools/chancal/t0_noise.py --confirm-terminated
"""

from __future__ import annotations

import argparse

import common


def floors(radio: common.Radio, samples: int) -> tuple[float, float]:
    rx1, rx2 = radio.capture(samples)
    return common.rms_dbfs(rx1), common.rms_dbfs(rx2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uri", default=common.DEFAULT_URI)
    ap.add_argument("--samples", type=int, default=1 << 16)
    ap.add_argument("--lo-hz", type=float, default=2.45e9)
    ap.add_argument("--nf-gain-db", type=float, default=60.0)
    ap.add_argument("--confirm-terminated", action="store_true",
                    help="assert both RX ports carry a 50 ohm load")
    args = ap.parse_args()

    if not args.confirm_terminated:
        print("Refusing to run: pass --confirm-terminated once both RX SMA ports\n"
              "have a 50 ohm terminator on them. An open port measures the room,\n"
              "not the receiver, and the result would be quietly wrong.")
        return 2

    radio = common.Radio(args.uri)
    radio.assert_2r2t()
    radio.assert_running()
    radio.set_rx_lo(args.lo_hz)
    radio.set_rx_bandwidth(18e6)
    common.banner("T0.3 terminated noise floor", radio)
    fs = radio.sample_rate
    report = {"provenance": common.provenance(radio), "samples": args.samples}

    # --- gain law -------------------------------------------------------
    print("\n  gain law (terminated, LO %.3f GHz)" % (args.lo_hz / 1e9))
    print(f"  {'cmd dB':>7} {'RX1 dBFS':>10} {'RX2 dBFS':>10} {'d(2-1)':>8}"
          f" {'RX1 clip':>9} {'RX2 clip':>9}")
    sweep = []
    for g in range(0, 72, 5):
        a1 = radio.set_rx_gain(0, float(g))
        a2 = radio.set_rx_gain(1, float(g))
        rx1, rx2 = radio.capture(args.samples)
        f1, f2 = common.rms_dbfs(rx1), common.rms_dbfs(rx2)
        c1, c2 = common.clipping_fraction(rx1), common.clipping_fraction(rx2)
        print(f"  {g:7d} {f1:10.2f} {f2:10.2f} {f2 - f1:+8.2f} {c1:9.4f} {c2:9.4f}")
        sweep.append({"commanded_db": g, "actual_db": [a1, a2],
                      "floor_dbfs": [f1, f2], "delta_db": f2 - f1,
                      "clip_fraction": [c1, c2]})
    report["gain_law"] = sweep

    lo = [s for s in sweep if 10 <= s["commanded_db"] <= 60]
    if len(lo) >= 2:
        span = lo[-1]["commanded_db"] - lo[0]["commanded_db"]
        s1 = (lo[-1]["floor_dbfs"][0] - lo[0]["floor_dbfs"][0]) / span
        s2 = (lo[-1]["floor_dbfs"][1] - lo[0]["floor_dbfs"][1]) / span
        deltas = [s["delta_db"] for s in lo]
        print(f"\n  slope 10-60 dB:  RX1 {s1:.3f} dB/dB   RX2 {s2:.3f} dB/dB")
        print(f"  RX2-RX1 offset:  mean {sum(deltas)/len(deltas):+.2f} dB, "
              f"spread {max(deltas) - min(deltas):.2f} dB")
        print("  Parallel curves (small spread) = a fixed, calibratable offset.")
        print("  Diverging curves = different gain tables; do not calibrate that away.")
        report["gain_law_summary"] = {
            "slope_db_per_db": [s1, s2],
            "mean_offset_db": sum(deltas) / len(deltas),
            "offset_spread_db": max(deltas) - min(deltas),
        }

    # --- gain independence ---------------------------------------------
    print("\n  gain independence (each floor must follow its own channel)")
    indep = []
    for g1, g2 in ((20.0, 60.0), (60.0, 20.0)):
        radio.set_rx_gain(0, g1)
        radio.set_rx_gain(1, g2)
        f1, f2 = floors(radio, args.samples)
        print(f"    RX1={g1:.0f} dB RX2={g2:.0f} dB  ->  "
              f"RX1 {f1:7.2f} dBFS   RX2 {f2:7.2f} dBFS")
        indep.append({"commanded": [g1, g2], "floor_dbfs": [f1, f2]})
    swing1 = indep[1]["floor_dbfs"][0] - indep[0]["floor_dbfs"][0]
    swing2 = indep[1]["floor_dbfs"][1] - indep[0]["floor_dbfs"][1]
    passed = swing1 > 20 and swing2 < -20
    print(f"    RX1 moved {swing1:+.2f} dB, RX2 moved {swing2:+.2f} dB  ->  "
          f"{'PASS - independent' if passed else 'FAIL - channels are not independent'}")
    report["gain_independence"] = {"trials": indep, "swing_db": [swing1, swing2],
                                   "pass": passed}

    # --- noise figure proxy versus frequency ----------------------------
    print(f"\n  relative noise figure (terminated, both gains {args.nf_gain_db:.0f} dB)")
    radio.manual_gain_both(args.nf_gain_db)
    print(f"  {'LO MHz':>9} {'RX1 dBFS':>10} {'RX2 dBFS':>10} {'d(2-1)':>8}")
    nf = []
    for mhz in (70, 100, 200, 433, 915, 1575, 2450, 3500, 4500, 5800, 6000):
        try:
            actual = radio.set_rx_lo(mhz * 1e6)
        except common.AttrMismatch as exc:
            print(f"  {mhz:9d}  refused: {exc}")
            nf.append({"lo_mhz": mhz, "refused": str(exc)})
            continue
        f1, f2 = floors(radio, args.samples)
        print(f"  {actual/1e6:9.2f} {f1:10.2f} {f2:10.2f} {f2 - f1:+8.2f}")
        nf.append({"lo_mhz": actual / 1e6, "floor_dbfs": [f1, f2],
                   "delta_db": f2 - f1})
    report["noise_figure_proxy"] = nf
    ok = [p for p in nf if "delta_db" in p]
    if ok:
        worst = max(ok, key=lambda p: abs(p["delta_db"]))
        print(f"\n  Largest RX2-RX1 gap: {worst['delta_db']:+.2f} dB at "
              f"{worst['lo_mhz']:.0f} MHz")
        print("  A gap that grows at the band edges is the expected signature of an")
        print("  unbinned second channel - note the frequency, do not average it out.")
        report["worst_nf_delta"] = worst

    common.save("t0-noise", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
