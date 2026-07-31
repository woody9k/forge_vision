#!/usr/bin/env python3
"""T0.1 - attribute parity between channel 1 and channel 2.

Cheapest possible test and the right one to run first: if the driver itself
offers channel 2 a different gain range, a different bandwidth ceiling or a
different set of RF ports, every later measurement is explained by that rather
than by the silicon. Needs no cables and no transmit.

    .venv/bin/python tools/chancal/t0_parity.py
"""

from __future__ import annotations

import argparse

import common


# (device, channel-1 name, channel-2 name, output?, label)
PAIRS = [
    (common.PHY, "voltage0", "voltage1", False, "phy RX1 vs RX2"),
    (common.PHY, "voltage0", "voltage1", True, "phy TX1 vs TX2"),
    (common.RXDEV, "voltage0", "voltage2", False, "RX buffer I (ch1 vs ch2)"),
    (common.RXDEV, "voltage1", "voltage3", False, "RX buffer Q (ch1 vs ch2)"),
    (common.TXDEV, "voltage0", "voltage2", True, "TX buffer I (ch1 vs ch2)"),
    (common.TXDEV, "voltage1", "voltage3", True, "TX buffer Q (ch1 vs ch2)"),
]

# tone generators, compared by name suffix
TONE_PAIRS = [("TX1_I_F1", "TX2_I_F1"), ("TX1_Q_F1", "TX2_Q_F1"),
              ("TX1_I_F2", "TX2_I_F2"), ("TX1_Q_F2", "TX2_Q_F2")]

# attributes whose value legitimately differs between two live channels; a
# difference here is state, not asymmetry
VOLATILE = {"rssi", "hardwaregain", "gain_control_mode", "raw", "scale",
            "phase", "frequency", "calibbias", "calibphase", "calibscale"}


def read_all(dev, name: str, output: bool) -> dict:
    ch = dev.find_channel(name, output)
    if ch is None:
        return {}
    out = {}
    for attr, obj in ch.attrs.items():
        try:
            out[attr] = obj.value
        except OSError as exc:            # samples_pps and friends error on read
            out[attr] = f"<error: {exc.strerror or exc}>"
    return out


def compare(dev, a: str, b: str, output: bool, label: str, report: dict) -> int:
    ga, gb = read_all(dev, a, output), read_all(dev, b, output)
    only_a = sorted(set(ga) - set(gb))
    only_b = sorted(set(gb) - set(ga))
    differ = {k: (ga[k], gb[k]) for k in sorted(set(ga) & set(gb))
              if ga[k] != gb[k] and k not in VOLATILE}
    volatile = {k: (ga[k], gb[k]) for k in sorted(set(ga) & set(gb))
                if ga[k] != gb[k] and k in VOLATILE}

    problems = len(only_a) + len(only_b) + len(differ)
    mark = "FAIL" if problems else "ok  "
    print(f"  [{mark}] {label}  ({len(ga)} attrs)")
    for k in only_a:
        print(f"           only on {a}: {k} = {ga[k]}")
    for k in only_b:
        print(f"           only on {b}: {k} = {gb[k]}")
    for k, (va, vb) in differ.items():
        print(f"           MISMATCH {k}: {a}={va!r}  {b}={vb!r}")
    for k, (va, vb) in volatile.items():
        print(f"           (state)  {k}: {a}={va!r}  {b}={vb!r}")

    report[label] = {
        "channels": [a, b], "output": output, "device": dev.name,
        "attr_count": [len(ga), len(gb)],
        "only_on_ch1": only_a, "only_on_ch2": only_b,
        "mismatches": differ, "state_differences": volatile,
    }
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uri", default=common.DEFAULT_URI)
    args = ap.parse_args()

    radio = common.Radio(args.uri)
    radio.assert_2r2t()
    radio.assert_running()
    common.banner("T0.1 attribute parity", radio)

    report: dict = {"provenance": common.provenance(radio),
                    "context_attrs": radio.context_attrs(), "pairs": {}}
    problems = 0

    for devname, a, b, output, label in PAIRS:
        dev = radio.ctx.find_device(devname)
        if dev is None:
            print(f"  [skip] {label}: no device {devname}")
            continue
        problems += compare(dev, a, b, output, label, report["pairs"])

    print()
    for a, b in TONE_PAIRS:
        ga = read_all(radio.tx, a, True)
        gb = read_all(radio.tx, b, True)
        if not ga or not gb:
            print(f"  [FAIL] tone pair {a}/{b}: one of them does not exist")
            problems += 1
            continue
        keys = "ok" if set(ga) == set(gb) else "DIFFERENT ATTRS"
        print(f"  [{'ok  ' if keys == 'ok' else 'FAIL'}] tone pair {a} / {b}: {keys}")
        report["pairs"][f"tone {a}/{b}"] = {"ch1": ga, "ch2": gb}
        problems += 0 if keys == "ok" else 1

    print(f"\n  {problems} structural difference(s) between channel 1 and channel 2")
    if not problems:
        print("  Channel 2 is offered to us on identical terms. That says nothing")
        print("  about its RF performance - it only rules out the driver as a cause.")
    report["problem_count"] = problems
    common.save("t0-parity", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
