"""Gated CW source using the AD9361's on-chip DDS.

This is the only code in the suite that keys a transmitter, and as of this
writing **nothing in this project has ever transmitted**. Read the TX section
of tools/chancal/README.md before using it.

Why the on-chip DDS rather than streaming a waveform from the host: it is a
hardware numerically-controlled oscillator, so the tone is spectrally clean,
needs no DMA, and cannot be disturbed by a host-side buffer underrun. For phase
metrology that matters - a glitching source is indistinguishable from a
glitching receiver.

A note on why an on-board TX is a legitimate source for a *relative* phase
measurement: the AD9361 has separate TX and RX PLLs. They share the 40 MHz
reference, so they are frequency-locked but not phase-locked, and the TX-to-RX
phase wanders slowly. That wander is **common to both receivers**, so it
cancels exactly in the RX1-minus-RX2 difference, which is the only quantity
this suite quotes.
"""

from __future__ import annotations

import common

# Deliberately far below anything useful. The conducted tests want a tone that
# lands around -30 dBFS at the receiver through a 30 dB pad, not a hot signal.
SAFE_TX_GAIN_DB = -40.0
OFF_TX_GAIN_DB = -89.75          # the part's minimum, i.e. as off as it gets

CONFIRM = "conducted-into-attenuator"


class TxGateError(RuntimeError):
    pass


class Tone:
    """Context manager: a single-sideband CW tone on TX1 or TX2.

    Always used as `with Tone(...)` so the transmitter is shut down on the way
    out of *any* path, including an exception - the same try/finally discipline
    the platform's SafetyController enforces (rule 5).
    """

    def __init__(self, radio: common.Radio, chan: int, offset_hz: float,
                 gain_db: float = SAFE_TX_GAIN_DB, confirm: str = ""):
        if confirm != CONFIRM:
            raise TxGateError(
                "TX refused. This keys a real transmitter on a board that has "
                "never transmitted.\n"
                "Pass --tx-confirm " + CONFIRM + " only when ALL of these hold:\n"
                "  * the TX port goes into an attenuator (>=30 dB) and from there "
                "into the receiver or a 50 ohm load\n"
                "  * NO antenna is connected to any TX port\n"
                "  * you have read the TX section of tools/chancal/README.md")
        if chan not in (1, 2):
            raise ValueError("chan must be 1 or 2")
        if gain_db > -20.0:
            raise TxGateError(
                f"TX gain {gain_db} dB is above the -20 dB bench ceiling. "
                "Raise it deliberately in the script, not from the command line.")
        self.radio = radio
        self.chan = chan
        self.offset_hz = offset_hz
        self.gain_db = gain_db

    # -- DDS plumbing -----------------------------------------------------

    def _tones(self):
        """The I and Q generators of tone 1 for this channel."""
        return (f"TX{self.chan}_I_F1", f"TX{self.chan}_Q_F1")

    def _all_tone_names(self):
        for c in (1, 2):
            for iq in ("I", "Q"):
                for f in (1, 2):
                    yield f"TX{c}_{iq}_F{f}"

    def _silence_all(self):
        for name in self._all_tone_names():
            ch = self.radio.tx.find_channel(name, True)
            if ch is not None:
                ch.attrs["scale"].value = "0"

    def __enter__(self):
        r = self.radio
        # every other generator off first, so the spectrum contains exactly
        # what we think it contains
        self._silence_all()

        # attenuate both transmitters to the floor, then bring up only ours
        for ch in (0, 1):
            r.set(f"voltage{ch}", "hardwaregain", f"{OFF_TX_GAIN_DB:.2f}",
                  output=True, tol=0.5)

        i_name, q_name = self._tones()
        # 90 degrees between I and Q selects one sideband; equal phase would
        # put half the power in the image
        for name, phase_mdeg in ((i_name, 90000), (q_name, 0)):
            ch = r.tx.find_channel(name, True)
            ch.attrs["frequency"].value = str(int(self.offset_hz))
            ch.attrs["phase"].value = str(phase_mdeg)
            ch.attrs["raw"].value = "1"
            ch.attrs["scale"].value = "0.25"

        r.set(f"voltage{self.chan - 1}", "hardwaregain", f"{self.gain_db:.2f}",
              output=True, tol=0.5)
        actual = r.get(f"voltage{self.chan - 1}", "hardwaregain", output=True)
        print(f"  [TX{self.chan} ON] offset {self.offset_hz/1e6:+.3f} MHz, "
              f"gain {actual}, LO {r.tx_lo/1e6:.3f} MHz")
        return self

    def __exit__(self, *exc):
        r = self.radio
        try:
            self._silence_all()
        finally:
            for ch in (0, 1):
                try:
                    r.set(f"voltage{ch}", "hardwaregain", f"{OFF_TX_GAIN_DB:.2f}",
                          output=True, tol=0.5)
                except Exception as err:            # noqa: BLE001
                    print(f"  !! could not attenuate TX{ch + 1}: {err}")
            print(f"  [TX{self.chan} OFF] tones silenced, both TX at "
                  f"{OFF_TX_GAIN_DB} dB")
        return False
