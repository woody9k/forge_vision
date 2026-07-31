#!/usr/bin/env python3
"""T0.2 - prove the two-channel digital path with the AD9361 BIST tone.

Run this before anything involving a cable. It settles a question that poisons
every later measurement if you get it wrong: **is `voltage0/1` really RX1's I/Q
and `voltage2/3` really RX2's**, and are they two independent streams rather
than one buffer demuxed twice?

The AD9361 can replace its receive datapath with a synthetic complex tone. The
debugfs interface is `<mode> <freq_Hz> <level_dB> <mask>`; mode 2 injects at RX.
Measured on this board, the `mask` argument masks **channel 1's** I and Q
components individually - bit 0 zeroes one, bit 1 the other, and mask 3 zeroes
RX1 outright. That gives three independent checks for free:

    mask 0   both channels carry the tone at full scale
             -> single-sided spectrum = I/Q pairing is correct
    mask 1,2 RX1 drops by exactly 3 dB (one of I/Q zeroed), RX2 untouched
             -> voltage0 and voltage1 are the I and Q of the *same* receiver
    mask 3   RX1 goes to numerically zero, RX2 stays at full scale
             -> voltage2/3 is a genuinely separate stream, and the
                channel-to-buffer-index mapping is what we assumed

This validates the digital and DMA path only. It says nothing about the RF
front end - that is what the Tier 1 tests with a real signal are for.

    .venv/bin/python tools/chancal/t0_bist.py

Needs SSH to the board; debugfs is not exposed over IIO.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess

import common

DEBUGFS = "/sys/kernel/debug/iio/iio:device0"

# The tone generator is quantized to sample_rate/32 (measured: asking for
# 2.000 MHz at 30.72 MSPS yields 1.920 MHz, which is 2 x 960 kHz). Not a fault
# - but a naive "is it where I asked" check would call it one.
QUANT_DIVISOR = 32

# "numerically zero" - a masked channel reads as exact zeros, not a low level
ZERO_DBFS = -200.0


def ssh(host: str, password: str, cmd: str) -> str:
    base = ["ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
            f"root@{host}", cmd]
    if password:
        if not shutil.which("sshpass"):
            raise RuntimeError("sshpass not installed; use an SSH key and --password ''")
        base = ["sshpass", "-p", password] + base
    r = subprocess.run(base, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ssh failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def set_tone(host: str, password: str, mode: int, freq_hz: int = 0,
             level_db: int = 0, mask: int = 0) -> str:
    ssh(host, password, f"echo '{mode} {freq_hz} {level_db} {mask}' > {DEBUGFS}/bist_tone")
    return ssh(host, password, f"cat {DEBUGFS}/bist_tone")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default=common.DEFAULT_URI)
    ap.add_argument("--host", default="192.168.99.222")
    ap.add_argument("--password", default="analog", help="'' to use an SSH key")
    ap.add_argument("--tone-hz", type=float, default=2e6)
    ap.add_argument("--samples", type=int, default=1 << 16)
    args = ap.parse_args()

    radio = common.Radio(args.uri)
    radio.assert_2r2t()
    radio.assert_running()
    radio.manual_gain_both(30.0)
    common.banner("T0.2 BIST tone / channel mapping", radio)
    fs = radio.sample_rate

    step = fs / QUANT_DIVISOR
    expect = round(args.tone_hz / step) * step
    print(f"  tone quantization is fs/{QUANT_DIVISOR} = {step/1e3:.0f} kHz; "
          f"asking for {args.tone_hz/1e6:.3f} MHz expects {expect/1e6:.3f} MHz")

    report = {"provenance": common.provenance(radio), "tone_hz": args.tone_hz,
              "expected_tone_hz": expect, "quantization_hz": step, "steps": {}}
    ok = True
    try:
        # --- mask 0: both channels live, check placement and I/Q pairing ---
        set_tone(args.host, args.password, 2, int(args.tone_hz), 0, 0)
        rx1, rx2 = radio.capture(args.samples)
        print("\n  mask 0 - both channels carry the tone")
        for label, x in (("RX1", rx1), ("RX2", rx2)):
            t = common.tone(x, fs)
            ir = common.image_rejection_db(x, fs)
            err = abs(t["freq_hz"] - expect)
            good_freq = err < 4 * fs / args.samples
            good_pair = ir > 20.0
            ok &= good_freq and good_pair
            print(f"    {label}: {t['freq_hz']/1e6:+.4f} MHz "
                  f"(err {err:+.0f} Hz){'' if good_freq else '  FAIL: misplaced'}"
                  f"  {t['rms_dbfs']:+.2f} dBFS rms")
            print(f"         image rejection {ir:.1f} dB -> "
                  f"{'I/Q pairing correct' if good_pair else 'I/Q PAIRING SUSPECT'}")
            report["steps"][f"mask0_{label}"] = dict(t, image_rejection_db=ir,
                                                     freq_error_hz=err)

        # --- masks 1 and 2: half of RX1 zeroed, RX2 must not care ---
        print("\n  masks 1,2 - one of RX1's I/Q zeroed (expect RX1 -3 dB, RX2 flat)")
        base2 = common.rms_dbfs(rx2)
        for m in (1, 2):
            set_tone(args.host, args.password, 2, int(args.tone_hz), 0, m)
            a, b = radio.capture(args.samples)
            r1, r2 = common.rms_dbfs(a), common.rms_dbfs(b)
            half = abs(r1 - (-3.01)) < 0.5
            flat = abs(r2 - base2) < 0.5
            ok &= half and flat
            print(f"    mask {m}: RX1 {r1:+8.2f} dBFS {'ok' if half else 'FAIL (want -3.01)'}"
                  f"   RX2 {r2:+8.2f} dBFS {'ok' if flat else 'FAIL (should not move)'}")
            report["steps"][f"mask{m}"] = {"rx1_rms_dbfs": r1, "rx2_rms_dbfs": r2}

        # --- mask 3: RX1 fully zeroed. The decisive mapping check. ---
        print("\n  mask 3 - RX1 zeroed entirely (expect RX1 silent, RX2 full scale)")
        set_tone(args.host, args.password, 2, int(args.tone_hz), 0, 3)
        a, b = radio.capture(args.samples)
        r1, r2 = common.rms_dbfs(a), common.rms_dbfs(b)
        silent = r1 < ZERO_DBFS
        alive = abs(r2 - base2) < 0.5
        ok &= silent and alive
        print(f"    RX1 {r1:+9.2f} dBFS {'ok - exactly zero' if silent else 'FAIL - RX1 is not the channel being masked'}")
        print(f"    RX2 {r2:+9.2f} dBFS {'ok - unaffected' if alive else 'FAIL - RX2 followed RX1'}")
        report["steps"]["mask3"] = {"rx1_rms_dbfs": r1, "rx2_rms_dbfs": r2}
        if silent and alive:
            print("    -> voltage0/1 is RX1, voltage2/3 is RX2, and the two are")
            print("       independent streams. Not one buffer demuxed twice.")

    finally:
        # never leave the injector running: it replaces the ADC data outright,
        # so every later capture would be synthetic and look immaculate
        back = set_tone(args.host, args.password, 0)
        print(f"\n  bist_tone <- 0 (disabled; reads back {back!r})")
        report["disabled_after"] = back
        rx1, rx2 = radio.capture(1 << 14)
        live = [common.rms_dbfs(rx1), common.rms_dbfs(rx2)]
        print(f"  live-again check: RX1 {live[0]:.2f} dBFS  RX2 {live[1]:.2f} dBFS")
        if max(live) > -1.0:
            print("  !! still at full scale - the injector did NOT turn off")
            ok = False
        report["after_disable_dbfs"] = live

    print(f"\n  T0.2 {'PASS' if ok else 'FAIL'}")
    report["pass"] = ok
    common.save("t0-bist", report)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
