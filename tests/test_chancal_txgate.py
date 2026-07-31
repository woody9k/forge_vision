"""The transmit gate in tools/chancal/txtone.py.

`tools/chancal/` is a bench suite that drives libiio directly and deliberately
bypasses `SafetyController`, so nothing else in the test tree covers it. This
file covers the one class that keys a transmitter, because "nothing in this
project has ever transmitted" is a claim worth protecting with more than a
docstring.

The suite imports `iio` at module scope. The gate logic under test never
reaches it, so a stub keeps this runnable on a host without the bindings —
the same reason the platform treats pyadi-iio as optional.

The stub is removed from `sys.modules` again immediately after the import.
Leaving it there is not a harmless convenience: a resident fake `iio` makes
`runtime.rescan_hardware()` report no driver, which silently turned
`test_capabilities.py::test_rescan_reports_already_present` into a skip and
cut the whole suite from ~175 s to ~49 s by disabling real hardware probing
everywhere. A test file must not change what other tests exercise.
"""

from __future__ import annotations

import pathlib
import sys
import types

import pytest

_stubbed_iio = "iio" not in sys.modules
if _stubbed_iio:
    sys.modules["iio"] = types.ModuleType("iio")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "tools" / "chancal"))
try:
    import txtone  # noqa: E402
finally:
    # `common` keeps its own reference to whatever it imported, so dropping the
    # stub here leaves this module working while any later `import iio`
    # resolves to the real bindings.
    if _stubbed_iio:
        del sys.modules["iio"]


class FakeAttr:
    def __init__(self):
        self.value = ""


class FakeChannel:
    def __init__(self):
        self.attrs = {k: FakeAttr() for k in
                      ("scale", "frequency", "phase", "raw")}


class FakeTx:
    def __init__(self):
        self.channels = {}

    def find_channel(self, name, output=False):
        return self.channels.setdefault(name, FakeChannel())


class FakeRadio:
    """Enough of common.Radio for the gate, recording every gain write.

    `fail_on_gain` reproduces the real failure: `Radio.set` writes the
    attribute and *then* verifies, so a mismatch raises with the new gain
    already applied to the hardware.
    """

    tx_lo = 915e6

    def __init__(self, fail_on_gain=None):
        self.tx = FakeTx()
        self.fail_on_gain = fail_on_gain
        self.gain_writes = []          # (channel_name, value) in order

    def set(self, name, attr, value, output=False, tol=0.0, verify=True):
        if attr == "hardwaregain":
            self.gain_writes.append((name, float(value)))
            if (self.fail_on_gain is not None
                    and float(value) == self.fail_on_gain):
                # the write landed; the readback check is what fails
                raise RuntimeError(
                    f"{name}.hardwaregain: wrote {value}, reads -37 "
                    "(clamped or quantized beyond tol=0.5)")
        return str(value)

    def get(self, name, attr, output=False):
        return "-40.000000 dB"

    # -- assertions ---------------------------------------------------------
    def both_transmitters_floored(self) -> bool:
        """Did each TX channel's *last* gain write put it at the floor?"""
        last = {}
        for name, val in self.gain_writes:
            last[name] = val
        return (len(last) == 2
                and all(v == txtone.OFF_TX_GAIN_DB for v in last.values()))

    def live_tones(self) -> list:
        return [n for n, ch in self.tx.channels.items()
                if ch.attrs["scale"].value not in ("", "0")]


# -- the gate ----------------------------------------------------------------

def test_tx_refused_without_the_confirmation_string():
    with pytest.raises(txtone.TxGateError, match="TX refused"):
        txtone.Tone(FakeRadio(), chan=1, offset_hz=1e6)


def test_tx_refused_above_the_bench_gain_ceiling():
    with pytest.raises(txtone.TxGateError, match="bench ceiling"):
        txtone.Tone(FakeRadio(), chan=1, offset_hz=1e6, gain_db=-5.0,
                    confirm=txtone.CONFIRM)


def test_the_confirmation_string_is_not_guessable_by_accident():
    for wrong in ("yes", "y", "true", "confirm", ""):
        with pytest.raises(txtone.TxGateError):
            txtone.Tone(FakeRadio(), chan=1, offset_hz=1e6, confirm=wrong)


# -- shutdown on every path --------------------------------------------------

def test_normal_exit_floors_both_transmitters():
    r = FakeRadio()
    with txtone.Tone(r, chan=1, offset_hz=1e6, confirm=txtone.CONFIRM):
        assert r.live_tones(), "the tone should be live inside the block"
    assert r.both_transmitters_floored()
    assert r.live_tones() == []


def test_exception_inside_the_block_still_floors_both_transmitters():
    r = FakeRadio()
    with pytest.raises(ValueError):
        with txtone.Tone(r, chan=1, offset_hz=1e6, confirm=txtone.CONFIRM):
            raise ValueError("measurement blew up mid-tone")
    assert r.both_transmitters_floored()
    assert r.live_tones() == []


def test_failure_while_raising_the_gain_does_not_leave_tx_keyed():
    """__exit__ never runs if __enter__ raises, so __enter__ must clean up.

    `Radio.set` writes before it verifies, so an AttrMismatch on the gain
    write means the gain reached the hardware and *then* the exception fired.
    This is reachable on the bench board: in `ensm_mode = alert` the driver
    accepts gain writes, ignores them, and lets readback drift — which is
    precisely what trips the tolerance check.
    """
    r = FakeRadio(fail_on_gain=-40.0)          # the "bring our channel up" write
    with pytest.raises(RuntimeError, match="hardwaregain"):
        with txtone.Tone(r, chan=1, offset_hz=1e6,
                         gain_db=-40.0, confirm=txtone.CONFIRM):
            pytest.fail("the block must not be entered")
    assert r.gain_writes, "the failing write should have been attempted"
    assert r.both_transmitters_floored(), (
        "TX was left keyed after __enter__ raised: " + repr(r.gain_writes))
    assert r.live_tones() == []


def test_shutdown_survives_a_failing_attenuation():
    """One transmitter refusing must not stop the attempt on the other."""
    r = FakeRadio()
    calls = []
    original = r.set

    def flaky(name, attr, value, **kw):
        if attr == "hardwaregain" and name == "voltage0":
            calls.append(name)
            if len(calls) > 1:            # fail only during shutdown
                r.gain_writes.append((name, float(value)))
                raise RuntimeError("voltage0 refused")
        return original(name, attr, value, **kw)

    r.set = flaky
    with txtone.Tone(r, chan=2, offset_hz=1e6, confirm=txtone.CONFIRM):
        pass
    # voltage1 must still have been floored despite voltage0 raising
    last = {}
    for name, val in r.gain_writes:
        last[name] = val
    assert last.get("voltage1") == txtone.OFF_TX_GAIN_DB
