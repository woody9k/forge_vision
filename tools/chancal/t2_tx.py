#!/usr/bin/env python3
"""T2 - TX2 against TX1, and TX-to-RX isolation.

**This keys a transmitter. Nothing in this project has ever transmitted.**
Read the TX section of tools/chancal/README.md first. Every mode here runs the
transmitter into an attenuator, never an antenna, and shuts it down in a
finally block on every exit path.

Modes:

  monitor    Observe each transmitter through the AD9361's internal TX monitor
             path - no cables, no attenuator, nothing leaving the package.

             **Known not to work on this firmware.** Measured 2026-07-31: the
             driver returns EINVAL for TX_MONITOR1, TX_MONITOR2 and
             TX_MONITOR1_2 on every RX channel, in both `fdd` and the ENSM
             states this board will accept, even though the device tree does
             carry the `adi,txmon-*` properties. Cause not established; a 2R2T
             interaction is plausible, since the monitor multiplexes onto the
             RX inputs that are now both live. Kept because it costs nothing to
             retry if the firmware changes, and it reports the refusal rather
             than crashing. Use `conducted` for real TX numbers.

  conducted  TX_n -> >=30 dB pad -> RX1. The receiver is held constant while
             the operator moves the cable between TX ports, so the receiver is
             a controlled variable rather than a second unknown. Measures
             output level versus commanded attenuation (the gain law), carrier
             leakage and sideband suppression.

  isolation  One transmitter driving into a load with the *other* channel's
             receiver terminated, measuring how much gets across anyway. This
             sets the direct-coupling floor for bistatic GPR, where TX and RX
             are live at the same instant. It is the number that decides
             whether a target reflection is visible at all near zero range.

    .venv/bin/python tools/chancal/t2_tx.py --mode monitor \\
        --tx-confirm conducted-into-attenuator
"""

from __future__ import annotations

import argparse

import common
import txtone

MONITOR_PORT = {1: "TX_MONITOR1", 2: "TX_MONITOR2"}


def restore_rx_port(radio: common.Radio, chan: int, port: str) -> None:
    radio.set(f"voltage{chan}", "rf_port_select", port, verify=False)


def measure_tx(radio, args, rx_chan: int) -> dict:
    """One capture on `rx_chan`, reported around the expected tone."""
    rx1, rx2 = radio.capture(args.samples)
    x = rx1 if rx_chan == 0 else rx2
    fs = radio.sample_rate
    t = common.tone(x, fs)
    return dict(t, sideband_suppression_db=common.image_rejection_db(x, fs))


def mode_monitor(radio, args, report) -> None:
    """Both transmitters through the same internal monitor path."""
    print("\n  internal TX monitor (uncalibrated coupling - relative numbers only)")
    original = radio.get("voltage0", "rf_port_select")
    print(f"  RX1 rf_port_select was {original!r}")
    try:
        for tx in (1, 2):
            port = MONITOR_PORT[tx]
            try:
                radio.set("voltage0", "rf_port_select", port, verify=False)
                got = radio.get("voltage0", "rf_port_select")
            except OSError as exc:
                # expected on this firmware - see the module docstring
                print(f"  [skip] TX{tx}: driver refused {port} ({exc}). The TX")
                print("         monitor path is unavailable here; use --mode conducted.")
                report["monitor"][f"tx{tx}"] = {"error": f"{port} refused: {exc}"}
                continue
            if got != port:
                print(f"  [skip] TX{tx}: driver would not select {port} (reads {got!r})")
                report["monitor"][f"tx{tx}"] = {"error": f"port select refused: {got}"}
                continue
            radio.set_tx_lo(args.lo_hz)
            radio.set_rx_lo(args.lo_hz)
            rows = []
            with txtone.Tone(radio, chan=tx, offset_hz=args.offset_hz,
                             gain_db=args.tx_gain_db, confirm=args.tx_confirm):
                for att in (-40.0, -30.0, -20.0):
                    radio.set(f"voltage{tx - 1}", "hardwaregain", f"{att:.2f}",
                              output=True, tol=0.5)
                    m = measure_tx(radio, args, rx_chan=0)
                    print(f"    TX{tx} @ {att:+6.1f} dB -> tone "
                          f"{m['level_dbfs']:7.2f} dBFS  SNR {m['snr_db']:5.1f} dB  "
                          f"sideband {m['sideband_suppression_db']:5.1f} dB")
                    rows.append(dict(m, commanded_tx_gain_db=att))
            report["monitor"][f"tx{tx}"] = rows
    finally:
        restore_rx_port(radio, 0, original)
        print(f"  RX1 rf_port_select restored to {original!r}")

    a, b = report["monitor"].get("tx1"), report["monitor"].get("tx2")
    if isinstance(a, list) and isinstance(b, list) and a and b:
        deltas = [y["level_dbfs"] - x["level_dbfs"] for x, y in zip(a, b)]
        print(f"\n  TX2 minus TX1 through the same monitor: "
              f"{', '.join(f'{d:+.2f}' for d in deltas)} dB")
        print("  Consistent across drive levels = a fixed offset. Diverging = the")
        print("  two transmitters are on different gain laws.")
        report["monitor"]["tx2_minus_tx1_db"] = deltas


def mode_conducted(radio, args, report) -> None:
    print("\n  conducted TX -> pad -> RX1")
    for tx in (1, 2):
        print(f"\n  Connect TX{tx} through a >=30 dB attenuator to RX1.")
        print("  No antenna anywhere. Confirm the pad is in line, not bypassed.")
        input("  Press Enter when cabled... ")
        radio.set_tx_lo(args.lo_hz)
        radio.set_rx_lo(args.lo_hz)
        radio.manual_gain_both(args.rx_gain_db)
        rows = []
        with txtone.Tone(radio, chan=tx, offset_hz=args.offset_hz,
                         gain_db=args.tx_gain_db, confirm=args.tx_confirm):
            print(f"    {'cmd dB':>7} {'tone dBFS':>11} {'floor':>9} {'SNR':>7} "
                  f"{'sideband':>9} {'clip':>8}")
            for att in (-50.0, -40.0, -30.0, -20.0):
                radio.set(f"voltage{tx - 1}", "hardwaregain", f"{att:.2f}",
                          output=True, tol=0.5)
                m = measure_tx(radio, args, rx_chan=0)
                print(f"    {att:7.1f} {m['level_dbfs']:11.2f} {m['floor_dbfs']:9.2f} "
                      f"{m['snr_db']:7.1f} {m['sideband_suppression_db']:9.1f} "
                      f"{m['clip_fraction']:8.4f}")
                if m["clip_fraction"] > 1e-4:
                    print("      !! clipping - drop RX gain, this row is not a measurement")
                rows.append(dict(m, commanded_tx_gain_db=att))
        report["conducted"][f"tx{tx}"] = rows
        if len(rows) >= 2:
            span = rows[-1]["commanded_tx_gain_db"] - rows[0]["commanded_tx_gain_db"]
            slope = (rows[-1]["level_dbfs"] - rows[0]["level_dbfs"]) / span
            print(f"    TX{tx} gain law: {slope:.3f} dB out per commanded dB "
                  "(1.000 is ideal)")
            report["conducted"][f"tx{tx}_slope"] = slope


def mode_isolation(radio, args, report) -> None:
    print("\n  TX/RX isolation - the bistatic direct-coupling floor")
    print("  Put a 50 ohm load on BOTH TX ports and a 50 ohm terminator on both")
    print("  RX ports. Anything reaching a receiver now got there through the")
    print("  package or the board, which is exactly what we want to quantify.")
    input("  Press Enter when everything is loaded/terminated... ")

    radio.set_tx_lo(args.lo_hz)
    radio.set_rx_lo(args.lo_hz)
    radio.manual_gain_both(args.rx_gain_db)

    baseline = radio.capture(args.samples)
    b1, b2 = (common.rms_dbfs(baseline[0]), common.rms_dbfs(baseline[1]))
    print(f"  quiet floor: RX1 {b1:.2f} dBFS   RX2 {b2:.2f} dBFS")
    report["isolation"]["quiet_floor_dbfs"] = [b1, b2]

    for tx in (1, 2):
        with txtone.Tone(radio, chan=tx, offset_hz=args.offset_hz,
                         gain_db=args.tx_gain_db, confirm=args.tx_confirm):
            rx1, rx2 = radio.capture(args.samples)
            fs = radio.sample_rate
            t1 = common.tone(rx1, fs)
            t2 = common.tone(rx2, fs)
            print(f"    TX{tx} on: leakage into RX1 {t1['level_dbfs']:7.2f} dBFS "
                  f"(SNR {t1['snr_db']:5.1f} dB), "
                  f"into RX2 {t2['level_dbfs']:7.2f} dBFS "
                  f"(SNR {t2['snr_db']:5.1f} dB)")
            report["isolation"][f"tx{tx}"] = {"rx1": t1, "rx2": t2}
    print("\n  Only lines that stand clear of the quiet floor are leakage; a tone")
    print("  at the floor means the isolation is better than this setup can see,")
    print("  which is a bound, not a measurement.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default=common.DEFAULT_URI)
    ap.add_argument("--mode", choices=("monitor", "conducted", "isolation"),
                    required=True)
    ap.add_argument("--lo-hz", type=float, default=2.45e9,
                    help="default sits in an ISM band, so even a leak is benign")
    ap.add_argument("--offset-hz", type=float, default=2e6)
    ap.add_argument("--tx-gain-db", type=float, default=txtone.SAFE_TX_GAIN_DB)
    ap.add_argument("--rx-gain-db", type=float, default=30.0)
    ap.add_argument("--samples", type=int, default=1 << 16)
    ap.add_argument("--tx-confirm", default="", help="required; see --help")
    args = ap.parse_args()

    radio = common.Radio(args.uri)
    radio.assert_2r2t()
    radio.assert_running()
    radio.set_rx_bandwidth(18e6)
    radio.manual_gain_both(args.rx_gain_db)
    common.banner(f"T2 transmit characterization ({args.mode})", radio)

    report = {"provenance": common.provenance(radio),
              "settings": {k: v for k, v in vars(args).items() if k != "tx_confirm"},
              "monitor": {}, "conducted": {}, "isolation": {}}
    try:
        {"monitor": mode_monitor, "conducted": mode_conducted,
         "isolation": mode_isolation}[args.mode](radio, args, report)
    finally:
        # belt and braces: the Tone context manager already does this, but a
        # transmitter left keyed by a crash in between is not acceptable
        for name in ("TX1_I_F1", "TX1_Q_F1", "TX1_I_F2", "TX1_Q_F2",
                     "TX2_I_F1", "TX2_Q_F1", "TX2_I_F2", "TX2_Q_F2"):
            ch = radio.tx.find_channel(name, True)
            if ch is not None:
                ch.attrs["scale"].value = "0"
        for ch in (0, 1):
            radio.set(f"voltage{ch}", "hardwaregain",
                      f"{txtone.OFF_TX_GAIN_DB:.2f}", output=True, verify=False)
        print("\n  [safe] both transmitters silenced and attenuated to minimum")

    common.save(f"t2-tx-{args.mode}", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
