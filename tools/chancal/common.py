"""Shared plumbing for the channel-2 characterization suite.

Deliberately raw `libiio` rather than pyadi-iio. These scripts are metrology:
we want every attribute write read back and verified, we want the exact bin the
phase came from, and we do not want a wrapper's version-dependent property
names between us and the driver. The installed pyadi is 0.0.21, which predates
the 2R2T support we would be relying on.

Nothing here transmits. TX helpers live in `t2_tx.py` behind an explicit gate.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import time

import iio
import numpy as np

DEFAULT_URI = os.environ.get("CHANCAL_URI", "ip:192.168.99.222")

# cf-ad9361-lpc reports `le:S12/16>>0`: a 12-bit sample sign-extended into an
# int16. Full scale is therefore 2048, not 32768. Every dBFS number in this
# suite is relative to that; get it wrong and the absolute levels shift by
# 24 dB while all the channel-to-channel *differences* stay correct.
FULL_SCALE = 2048.0

# Buffers to refill and discard after any reconfiguration, before keeping one.
#
# Measured on this board, not guessed: with the BIST injector switched from
# "tone on both channels" to "RX1 masked", refills 1-3 still returned the OLD
# data and only refill 4 reflected the change. That is the kernel's 4-block DMA
# queue draining. Anything less than 4 silently returns pre-change samples,
# which in this suite would mean attributing channel 1's behaviour to a setting
# it was never captured under. Every number here would be wrong and none of
# them would look wrong.
#
# This only bites a *persistent* buffer, which is what this suite keeps for
# speed. Destroying and recreating the buffer flushes the queue outright -
# verified, the very next capture is post-change - which is what the platform's
# own `PlutoDevice.receive()` already does, so it does not have this problem.
SETTLE_BUFFERS = 4

# Bins integrated by the wideband cross-spectrum estimator; see _select_bins.
WIDEBAND_BINS = 64

PHY = "ad9361-phy"
RXDEV = "cf-ad9361-lpc"
TXDEV = "cf-ad9361-dds-core-lpc"

# results land next to the scripts so a run is self-describing
RESULTS = pathlib.Path(__file__).resolve().parent / "results"


class AttrMismatch(RuntimeError):
    """A driver accepted a write and then reported a different value."""


class Radio:
    """A thin, verifying wrapper over one AD9361 context."""

    def __init__(self, uri: str = DEFAULT_URI):
        self.uri = uri
        self.ctx = iio.Context(uri)
        self.phy = self.ctx.find_device(PHY)
        self.rx = self.ctx.find_device(RXDEV)
        self.tx = self.ctx.find_device(TXDEV)
        if self.phy is None or self.rx is None:
            raise RuntimeError(f"{uri}: no {PHY}/{RXDEV} — is this an AD9361 context?")
        self._buf = None

    # -- identity ---------------------------------------------------------

    def context_attrs(self) -> dict:
        return {k: v for k, v in self.ctx.attrs.items()}

    def assert_running(self, fix: bool = True) -> None:
        """Refuse to measure a radio whose state machine is not running.

        Hard-won: writing `ensm_mode` can leave the AD9361 in `alert`, where
        the receivers are idle. Nothing announces it. Captures still succeed,
        gain writes are accepted and then ignored, `hardwaregain` reads back
        values that drift on their own, and the DMA returns a static pattern
        that scores a perfect 1.0000 coherence - a flawless-looking result
        produced by a radio that is not receiving. Cost an hour before the
        `ensm=alert` was spotted. The recovery is simply to set `fdd` back.
        """
        state = self.phy.attrs["ensm_mode"].value
        if state == "fdd":
            return
        if not fix:
            raise RuntimeError(
                f"ad9361 ensm_mode is {state!r}, not 'fdd' - the receivers are "
                "idle and every number taken now would be fiction")
        print(f"  [recover] ensm_mode was {state!r}, not 'fdd' - the receivers "
              "were idle. Restoring 'fdd'.")
        self.phy.attrs["ensm_mode"].value = "fdd"
        time.sleep(0.5)
        now = self.phy.attrs["ensm_mode"].value
        if now != "fdd":
            raise RuntimeError(f"could not return ensm_mode to 'fdd' (reads {now!r})")
        self.drop_buffer()

    def assert_2r2t(self) -> None:
        """Refuse to characterize a second channel that is not there.

        Four buffer channels on `cf-ad9361-lpc` is the tell: in 1R1T the same
        device exposes two, which are the I and Q of a single receiver.
        """
        n = len(self.rx.channels)
        model = self.ctx.attrs.get("ad9361-phy,model", "?")
        if n < 4:
            raise RuntimeError(
                f"{RXDEV} has {n} channels (model {model}) — the board is in 1R1T. "
                "Check the u-boot env: compatible=ad9361, mode=2r2t, "
                "attr_name/attr_val both unset.")

    # -- attributes -------------------------------------------------------

    def _chan(self, dev, name: str, output: bool):
        ch = dev.find_channel(name, output)
        if ch is None:
            raise RuntimeError(f"no channel {name} (output={output}) on {dev.name}")
        return ch

    def get(self, name: str, attr: str, output: bool = False, dev=None) -> str:
        return self._chan(dev or self.phy, name, output).attrs[attr].value

    def set(self, name: str, attr: str, value, output: bool = False, dev=None,
            verify: bool = True, tol: float = 0.0) -> str:
        """Write an attribute and read it back.

        The AD9361 driver silently clamps almost everything — sample rate, LO,
        gain — so an unverified write is a guess. `tol` allows the quantization
        the hardware genuinely has (gain is 0.25 dB, the LO is a fractional-N
        step) without hiding a clamp.
        """
        ch = self._chan(dev or self.phy, name, output)
        ch.attrs[attr].value = str(value)
        got = ch.attrs[attr].value
        if verify:
            try:
                # hardwaregain reads back as "30.000000 dB" but is written bare
                wanted, actual = float(value), float(str(got).split()[0])
            except (ValueError, IndexError):
                if str(got).strip() != str(value).strip():
                    raise AttrMismatch(f"{name}.{attr}: wrote {value!r}, reads {got!r}")
            else:
                if abs(actual - wanted) > tol:
                    raise AttrMismatch(
                        f"{name}.{attr}: wrote {wanted:g}, reads {actual:g} "
                        f"(clamped or quantized beyond tol={tol:g})")
        return got

    # -- convenience: the handful of knobs every test touches --------------

    @property
    def sample_rate(self) -> float:
        return float(self.get("voltage0", "sampling_frequency"))

    def set_sample_rate(self, hz: float) -> float:
        self.set("voltage0", "sampling_frequency", int(hz), tol=1.0)
        return self.sample_rate

    @property
    def rx_lo(self) -> float:
        return float(self.get("altvoltage0", "frequency", output=True))

    def set_rx_lo(self, hz: float) -> float:
        # fractional-N: a few Hz of settling error is the part, not a clamp
        self.set("altvoltage0", "frequency", int(hz), output=True, tol=100.0)
        return self.rx_lo

    @property
    def tx_lo(self) -> float:
        return float(self.get("altvoltage1", "frequency", output=True))

    def set_tx_lo(self, hz: float) -> float:
        self.set("altvoltage1", "frequency", int(hz), output=True, tol=100.0)
        return self.tx_lo

    def rx_gain(self, chan: int) -> float:
        return float(self.get(f"voltage{chan}", "hardwaregain").split()[0])

    def rx_gain_range(self, chan: int) -> tuple[float, float]:
        """The gain table available at the *current* LO.

        Not a constant: the AD9361 selects a different gain table per band, so
        the ceiling is 71 dB around 2.4 GHz but only 40 dB down at 98 MHz.
        Asking for more than the band allows is silently clamped by the driver.
        """
        lo, _step, hi = self.get(f"voltage{chan}", "hardwaregain_available").strip(
            "[]").split()
        return float(lo), float(hi)

    def set_rx_gain(self, chan: int, db: float) -> float:
        self.set(f"voltage{chan}", "gain_control_mode", "manual")
        lo, hi = self.rx_gain_range(chan)
        want = min(max(db, lo), hi)
        if want != db:
            # clamp rather than raise, so a band sweep is not derailed by one
            # edge - but never quietly: an unreported clamp would show up later
            # as an unexplained level step between bands
            print(f"    [clamp] RX{chan + 1} gain {db:.2f} dB is outside the "
                  f"[{lo:g}, {hi:g}] table at {self.rx_lo/1e6:.1f} MHz; using "
                  f"{want:.2f} dB")
        # the gain table is 1 dB steps and not aligned to round numbers - asking
        # for 0 dB lands on -1 dB. That is quantization, not a clamp, so the
        # tolerance has to be a full step or every sweep trips on it
        self.set(f"voltage{chan}", "hardwaregain", f"{want:.2f}", tol=1.01)
        return self.rx_gain(chan)

    def set_rx_bandwidth(self, hz: float) -> None:
        for ch in (0, 1):
            self.set(f"voltage{ch}", "rf_bandwidth", int(hz), tol=1.0)

    def manual_gain_both(self, db: float) -> tuple[float, float]:
        """Manual gain on both receivers.

        Non-negotiable for anything phase-related: two independent AGCs will
        step gain at different instants, and an AD9361 gain step carries a
        phase step with it. An AGC-on phase measurement is a measurement of
        the AGC.
        """
        return self.set_rx_gain(0, db), self.set_rx_gain(1, db)

    def calibphase(self, chan: int) -> float:
        return float(self._chan(self.rx, f"voltage{chan}", False).attrs["calibphase"].value)

    # -- capture ----------------------------------------------------------

    def capture(self, nsamples: int = 1 << 16, settle_buffers: int = SETTLE_BUFFERS):
        """One DMA burst; returns (rx1, rx2) as complex64.

        Both receivers come out of a single buffer, which is what makes the
        relative phase meaningful — they share the sample clock and are
        demuxed from one interleaved stream, not captured in sequence.

        `settle_buffers` refills are thrown away first. This is not a
        precaution, it is required: see SETTLE_BUFFERS.
        """
        chans = [self.rx.find_channel(f"voltage{i}") for i in range(4)]
        for ch in chans:
            ch.enabled = True
        if self._buf is None:
            self._buf = iio.Buffer(self.rx, nsamples, False)
        for _ in range(settle_buffers):
            self._buf.refill()
        self._buf.refill()
        raw = [np.frombuffer(ch.read(self._buf), dtype="<i2").astype(np.float32)
               for ch in chans]
        rx1 = (raw[0] + 1j * raw[1]).astype(np.complex64)
        rx2 = (raw[2] + 1j * raw[3]).astype(np.complex64)
        return rx1, rx2

    def drop_buffer(self) -> None:
        """Tear the DMA buffer down so the next capture rebuilds it."""
        self._buf = None

    def close(self) -> None:
        self._buf = None
        self.ctx = None


# -- measurement ----------------------------------------------------------


def dbfs(amplitude: float) -> float:
    return 20.0 * math.log10(max(amplitude, 1e-12) / FULL_SCALE)


def rms_dbfs(x: np.ndarray) -> float:
    return dbfs(float(np.sqrt(np.mean(np.abs(x) ** 2))))


def clipping_fraction(x: np.ndarray) -> float:
    """Share of samples at or past the 12-bit rail.

    Rule 3 of this codebase: a clipped capture is not a measurement, and it
    must be reported rather than quietly averaged in.
    """
    lim = FULL_SCALE - 2
    return float(np.mean((np.abs(x.real) >= lim) | (np.abs(x.imag) >= lim)))


def _spectrum(x: np.ndarray):
    n = len(x)
    w = np.hanning(n).astype(np.float32)
    X = np.fft.fftshift(np.fft.fft(x * w))
    return X, float(w.sum())


def tone(x: np.ndarray, fs: float, exclude_dc_hz: float = 50e3) -> dict:
    """Locate the strongest tone and report its level, plus the floor."""
    n = len(x)
    X, wsum = _spectrum(x)
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))
    mag = np.abs(X)
    # the AD9361 always has an LO-leakage spike at baseband DC; it is not the
    # signal, and picking it would make every later number nonsense
    mask = np.abs(freqs) > exclude_dc_hz
    k = int(np.argmax(np.where(mask, mag, 0.0)))
    peak = float(mag[k]) / wsum
    # noise floor from the median of everything well away from the tone
    away = mask & (np.abs(freqs - freqs[k]) > 20 * fs / n)
    floor = float(np.median(mag[away])) / wsum
    return {
        "bin": k,
        "freq_hz": float(freqs[k]),
        "level_dbfs": dbfs(peak),
        "floor_dbfs": dbfs(floor),
        "snr_db": dbfs(peak) - dbfs(floor),
        "rms_dbfs": rms_dbfs(x),
        "clip_fraction": clipping_fraction(x),
    }


def image_rejection_db(x: np.ndarray, fs: float, exclude_dc_hz: float = 50e3) -> float:
    """Tone level minus the level of its mirror across DC.

    Direct-conversion quadrature error puts an image at -f. This is the single
    most sensitive indicator of a badly matched receive path, which is exactly
    the risk with an unbinned second channel.
    """
    n = len(x)
    X, _ = _spectrum(x)
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))
    mag = np.abs(X)
    mask = np.abs(freqs) > exclude_dc_hz
    k = int(np.argmax(np.where(mask, mag, 0.0)))
    k_img = int(np.argmin(np.abs(freqs + freqs[k])))
    lo = max(0, k_img - 2)
    img = float(np.max(mag[lo:k_img + 3]))
    return 20.0 * math.log10(float(mag[k]) / max(img, 1e-12))


def _select_bins(X1, X2, freqs, exclude_dc_hz: float, wideband: bool):
    """Which FFT bins carry the common signal.

    Narrowband (default): the single strongest bin, chosen from the *summed*
    magnitude so both channels are read at the same bin. Letting each channel
    pick its own argmax would let a one-bin disagreement inject a phase error
    that has nothing to do with the hardware.

    Wideband: the strongest `WIDEBAND_BINS` bins. This exists so the ladder can
    run against an ambient broadcast carrier through two antennas when no
    splitter is available - a screening test, not a substitute for a conducted
    one. Summing cross-spectra across a band is only valid while any delay
    between the two paths is small enough that the phase does not rotate across
    it: at 20 MHz of occupied bandwidth and a 30 cm antenna separation the
    rotation is about 0.13 rad, which is negligible. Widen either a lot and it
    stops being negligible.
    """
    mag = np.abs(X1) + np.abs(X2)
    mask = np.abs(freqs) > exclude_dc_hz
    masked = np.where(mask, mag, 0.0)
    if not wideband:
        return np.array([int(np.argmax(masked))])
    n = min(WIDEBAND_BINS, int(mask.sum()))
    return np.argpartition(masked, -n)[-n:]


def pair_phase(rx1: np.ndarray, rx2: np.ndarray, fs: float, blocks: int = 16,
               exclude_dc_hz: float = 50e3, wideband: bool = False) -> dict:
    """Relative phase and coherence of two receivers on a common tone.

    Both channels are read at the *same* FFT bin, chosen from the summed
    magnitude. Using each channel's own argmax would let a one-bin
    disagreement inject a phase error that has nothing to do with the
    hardware.

    `coherence` is |mean(cross)| / mean(|cross|) over sub-blocks: 1.0 means
    the phase held perfectly still across the capture, and anything below
    ~0.99 means the number after it is not worth quoting.
    """
    n = min(len(rx1), len(rx2))
    rx1, rx2 = rx1[:n], rx2[:n]
    X1, wsum = _spectrum(rx1)
    X2, _ = _spectrum(rx2)
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))
    bins = _select_bins(X1, X2, freqs, exclude_dc_hz, wideband)
    k = int(bins[int(np.argmax(np.abs(X1[bins]) + np.abs(X2[bins])))])

    cross = complex(np.sum(X1[bins] * np.conj(X2[bins])))
    phase = math.degrees(math.atan2(cross.imag, cross.real))

    # per-block phase, for within-capture stability
    blk = n // blocks
    phases, crosses = [], []
    for b in range(blocks):
        s = rx1[b * blk:(b + 1) * blk]
        t = rx2[b * blk:(b + 1) * blk]
        B1, bw = _spectrum(s)
        B2, _ = _spectrum(t)
        bf = np.fft.fftshift(np.fft.fftfreq(blk, 1.0 / fs))
        bb = _select_bins(B1, B2, bf, exclude_dc_hz, wideband)
        c = complex(np.sum(B1[bb] * np.conj(B2[bb])))
        crosses.append(c)
        phases.append(math.degrees(math.atan2(c.imag, c.real)))
    crosses = np.array(crosses)
    coherence = float(np.abs(np.mean(crosses)) / max(np.mean(np.abs(crosses)), 1e-30))

    return {
        "bin": k,
        "tone_freq_hz": float(freqs[k]),
        "phase_deg": phase,
        "amp_ratio_db": 20.0 * math.log10(abs(X1[k]) / max(abs(X2[k]), 1e-12)),
        "rx1_level_dbfs": dbfs(abs(X1[k]) / wsum),
        "rx2_level_dbfs": dbfs(abs(X2[k]) / wsum),
        "block_phase_std_deg": circ_std(phases),
        "coherence": coherence,
        "rx1_clip": clipping_fraction(rx1),
        "rx2_clip": clipping_fraction(rx2),
    }


def circ_mean(degs) -> float:
    r = np.deg2rad(np.asarray(degs, dtype=float))
    return float(math.degrees(math.atan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


def circ_std(degs) -> float:
    """Circular standard deviation, in degrees.

    A plain std() would report ~180 deg for a rock-steady offset that happens
    to sit near the +/-180 wrap, which is the difference between "unusable"
    and "perfect".
    """
    r = np.deg2rad(np.asarray(degs, dtype=float))
    R = math.hypot(float(np.mean(np.sin(r))), float(np.mean(np.cos(r))))
    R = min(max(R, 1e-12), 1.0)
    return float(math.degrees(math.sqrt(-2.0 * math.log(R))))


def wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


# -- reporting ------------------------------------------------------------


def save(name: str, payload: dict) -> pathlib.Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = RESULTS / f"{stamp}-{name}.json"
    payload = dict(payload)
    payload.setdefault("script", name)
    payload.setdefault("timestamp", stamp)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\n  -> {path}")
    return path


def provenance(radio: Radio) -> dict:
    """What the radio was when the numbers were taken.

    A characterization result that does not say which firmware, model string
    and sample rate produced it cannot be compared against the next one.
    """
    return {
        "uri": radio.uri,
        "hw_model": radio.ctx.attrs.get("hw_model", "?"),
        "fw_version": radio.ctx.attrs.get("fw_version", "?"),
        "phy_model": radio.ctx.attrs.get("ad9361-phy,model", "?"),
        "kernel": radio.ctx.attrs.get("local,kernel", "?"),
        "sample_rate_hz": radio.sample_rate,
        "rx_lo_hz": radio.rx_lo,
        "rx_gain_db": [radio.rx_gain(0), radio.rx_gain(1)],
        "rx_calibphase": [radio.calibphase(0), radio.calibphase(2)],
    }


def banner(title: str, radio: Radio) -> None:
    print(f"\n=== {title} ===")
    p = provenance(radio)
    print(f"  {p['hw_model']}  {p['fw_version']}  model={p['phy_model']}")
    print(f"  {p['uri']}  fs={p['sample_rate_hz']/1e6:.3f} MSPS  "
          f"LO={p['rx_lo_hz']/1e6:.3f} MHz")
