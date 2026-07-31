"""Real Pluto/Pluto+ adapter via pyadi-iio.

Import is optional: the platform runs fully without hardware. When pyadi-iio
and a device are present, `PlutoDevice.discover()` returns adapters for each
reachable radio (FR-DEV-001, FR-DEV-008).

Design notes for real hardware:

* **Burst capture only.** A Pluto cannot stream 61.44 MSPS continuously over
  USB (or even gigabit Ethernet on a Pluto+). Each `receive()` is a single
  contiguous DMA buffer captured at full rate in device memory; stitching
  multiple `rx()` buffers together would hide discontinuities, which the
  spec forbids (FR-ACQ-003). Requests larger than the DMA limit are refused
  with a clear error instead of silently degraded.

* **Capabilities are detected, not assumed.** A stock Pluto is 325 MHz -
  3.8 GHz; a Pluto+ (or a Pluto with the AD9364 firmware hack) reaches
  70 MHz - 6 GHz. We read the LO tuning bounds from the driver when the
  device exposes them and fall back to conservative stock limits otherwise
  (FR-DEV-002).
"""

from __future__ import annotations

import re
import time

import numpy as np

from . import discovery
from .base import (CaptureSegment, ConfigurationError, DeviceAdapter,
                   DeviceCapabilities)

try:
    import adi  # type: ignore
    HAVE_ADI = True
    _ADI_ERROR = ""
except Exception as _exc:  # noqa: BLE001 - missing libiio.so raises OSError
    adi = None
    HAVE_ADI = False
    _ADI_ERROR = str(_exc)

# largest single DMA burst we will request (samples); beyond this the kernel
# driver either refuses the buffer or allocation becomes unreliable
MAX_BURST_SAMPLES = 1 << 23   # ~8.4 M complex samples (~1.1 s of 8-chirp FMCW)

# usb: first — the direct backend avoids the RNDIS/iiod hop, and it is the
# same physical board as the 192.168.2.1 gadget address
DEFAULT_URIS = ("usb:", "ip:192.168.2.1")

# conservative stock-Pluto limits used when the device does not report bounds
STOCK_PLUTO_CAPS = DeviceCapabilities(
    min_frequency=325e6, max_frequency=3.8e9,
    min_sample_rate=0.65e6, max_sample_rate=61.44e6,
    max_bandwidth=20e6, rx_channels=1, tx_channels=1,
    max_rx_gain_db=71.0, min_tx_gain_db=-89.75, max_tx_gain_db=0.0,
    transports=("usb", "ethernet"),
)


def driver_status() -> dict:
    """Report whether the pyadi-iio/libiio stack is usable on this host."""
    return {
        "available": HAVE_ADI,
        "detail": "" if HAVE_ADI else
                  f"pyadi-iio/libiio not usable: {_ADI_ERROR or 'not installed'} "
                  "(install the system library with `sudo apt install "
                  "libiio-utils libiio-dev`, then `pip install pyadi-iio`)",
    }


class PlutoDevice(DeviceAdapter):
    def __init__(self, uri: str = "ip:192.168.2.1", device_id: str | None = None):
        super().__init__(device_id or f"pluto-{uri}")
        if not HAVE_ADI:
            raise RuntimeError(driver_status()["detail"])
        self.uri = uri
        self._sdr = None
        self._caps: DeviceCapabilities | None = None
        self._detection_notes: list[str] = []

    @classmethod
    def discover(cls, uris: tuple = (), prefer: str = "auto",
                 measure: bool = True, book: tuple = ()) -> list["PlutoDevice"]:
        """Probe every candidate transport and open one device per board.

        `usb:`, the 192.168.2.1 gadget and a physical Ethernet port are all
        ways into the *same* radio, so this groups them and opens exactly one
        — registering two would give two entries whose cached configuration
        drifts apart in silence. Which one it picks is measured rather than
        assumed, and `prefer` overrides it (see devices/discovery.py).
        """
        found: list[PlutoDevice] = []
        if not HAVE_ADI:
            return found
        probes = discovery.survey(
            discovery.candidate_uris(tuple(uris), book=tuple(book)),
            measure=measure)
        for board in discovery.group_boards(probes):
            uris_in_board = {t["uri"] for t in board["transports"]}
            pick = discovery.choose(
                [p for p in probes if p.uri in uris_in_board], prefer=prefer)
            if not pick.get("uri"):
                continue
            try:
                dev = cls(pick["uri"])
                dev.connect()
            except Exception:  # noqa: BLE001 - a transport can vanish between
                continue       # the probe and the open; try the next board
            dev.discovery = {**pick, "alternatives": board["transports"],
                             "identified_by": board["identified_by"]}
            if board.get("note"):
                dev.discovery["note"] = board["note"]
            found.append(dev)
        return found

    # -- capability detection (FR-DEV-002) ----------------------------------
    @staticmethod
    def _parse_range(raw: str) -> tuple[float, float] | None:
        """Parse an libiio '[min step max]' availability string."""
        nums = [float(x) for x in re.findall(r"-?[\d.]+", raw or "")]
        return (nums[0], nums[-1]) if len(nums) >= 3 else None

    def _chan_range(self, name: str, output: bool, attr: str):
        try:
            ch = self._sdr._ctrl.find_channel(name, output)
            return self._parse_range(ch.attrs[attr].value)
        except Exception:  # noqa: BLE001 - older firmware may lack the attr
            return None

    def _lo_settable(self, hz: float) -> bool:
        try:
            self._sdr.rx_lo = int(hz)
            return True
        except Exception:  # noqa: BLE001 - rejection is the signal we want
            return False

    def _verify_tuning_bounds(self, lo: float, hi: float) -> tuple[float, float, str]:
        """Trust, then verify: the advertised range can be wider than reality.

        An AD9364-unlocked Pluto advertises `frequency_available` from
        46.875 MHz but the synthesiser refuses anything under 70 MHz. Taking
        the advertised figure at face value produces a platform that offers a
        band and then fails mid-scan, so the edges are probed and narrowed to
        what the hardware actually accepts.
        """
        note = ""
        original = None
        try:
            original = int(self._sdr.rx_lo)
        except Exception:  # noqa: BLE001
            pass
        try:
            if not self._lo_settable(lo):
                good = hi if self._lo_settable(hi) else None
                if good is not None:
                    bad, ok = lo, good
                    for _ in range(24):          # ~1 kHz resolution, bounded
                        if ok - bad <= 1e3:
                            break
                        mid = (bad + ok) / 2
                        if self._lo_settable(mid):
                            ok = mid
                        else:
                            bad = mid
                    note = (f"driver advertised a {lo / 1e6:.3f} MHz lower "
                            f"limit but rejects anything below "
                            f"{ok / 1e6:.3f} MHz; using the measured value")
                    lo = ok
        finally:
            if original is not None:
                try:
                    self._sdr.rx_lo = original
                except Exception:  # noqa: BLE001
                    pass
        return lo, hi, note

    def _detect_capabilities(self) -> None:
        """Read real limits from the driver rather than assuming a board.

        The AD9363 in a stock Pluto is specified for 20 MHz of channel
        bandwidth, but the driver reports what the part will actually accept
        (56 MHz RX / 40 MHz TX on firmware v0.39). We trust the device and
        note where that exceeds the datasheet, instead of hard-coding limits
        that would refuse configurations the hardware supports.
        """
        base = STOCK_PLUTO_CAPS
        notes: list[str] = []

        lo_rx = self._chan_range("altvoltage0", True, "frequency_available")
        lo_tx = self._chan_range("altvoltage1", True, "frequency_available")
        rate = self._chan_range("voltage0", False, "sampling_frequency_available")
        rx_bw = self._chan_range("voltage0", False, "rf_bandwidth_available")
        tx_bw = self._chan_range("voltage0", True, "rf_bandwidth_available")
        rx_gain = self._chan_range("voltage0", False, "hardwaregain_available")
        tx_gain = self._chan_range("voltage0", True, "hardwaregain_available")

        if not lo_rx:
            self._caps = base
            self._detection_notes = [
                "driver did not report tuning bounds; assuming stock Pluto "
                "limits (325 MHz-3.8 GHz)"]
            return

        lo = min(lo_rx[0], (lo_tx or lo_rx)[0])
        hi = max(lo_rx[1], (lo_tx or lo_rx)[1])
        lo, hi, bounds_note = self._verify_tuning_bounds(lo, hi)
        model = ""
        try:
            model = self._sdr._ctx.attrs.get("ad9361-phy,model", "")
        except Exception:  # noqa: BLE001
            pass

        self._caps = DeviceCapabilities(
            min_frequency=lo, max_frequency=hi,
            min_sample_rate=(rate or (base.min_sample_rate,))[0],
            max_sample_rate=(rate or (0, base.max_sample_rate))[1],
            max_bandwidth=(rx_bw or (0, base.max_bandwidth))[1],
            max_tx_bandwidth=(tx_bw[1] if tx_bw else None),
            rx_channels=base.rx_channels, tx_channels=base.tx_channels,
            max_rx_gain_db=(rx_gain or (0, base.max_rx_gain_db))[1],
            min_tx_gain_db=(tx_gain or (base.min_tx_gain_db,))[0],
            max_tx_gain_db=(tx_gain or (0, base.max_tx_gain_db))[1],
            transports=base.transports)

        caps = self._caps
        notes.append(f"{model or 'transceiver'} reports tuning "
                     f"{lo / 1e6:.0f}-{hi / 1e6:.0f} MHz, "
                     f"RX bandwidth {caps.max_bandwidth / 1e6:.0f} MHz, "
                     f"TX bandwidth {caps.tx_bandwidth / 1e6:.0f} MHz, "
                     f"sample rate {caps.min_sample_rate / 1e6:.2f}-"
                     f"{caps.max_sample_rate / 1e6:.2f} MSPS")
        if bounds_note:
            notes.append(bounds_note)
        if model.startswith("ad9363") and caps.tx_bandwidth > 20e6:
            notes.append(
                "NOTE: the driver permits more bandwidth than the AD9363 is "
                "specified for (20 MHz). Wider sweeps are accepted but "
                "amplitude/phase flatness near the band edges is not "
                "guaranteed — calibrate before trusting range accuracy.")
        if hi <= 4e9:
            notes.append(
                "tuning stops at 3.8 GHz (AD9363 class). The documented "
                "AD9364 compatibility change unlocks 70 MHz-6 GHz.")
        self._detection_notes = notes

    @property
    def capabilities(self) -> DeviceCapabilities:
        return self._caps or STOCK_PLUTO_CAPS

    @property
    def kind(self) -> str:
        return "pluto"

    def describe(self) -> dict:
        d = super().describe()
        d["uri"] = self.uri
        d["capability_notes"] = self._detection_notes
        return d

    # -- lifecycle -----------------------------------------------------------
    def connect(self) -> None:
        if self.connected and self._sdr is not None:
            return          # idempotent: a radio's USB interface claims once
        try:
            self._sdr = adi.Pluto(uri=self.uri)
        except Exception as exc:  # noqa: BLE001 - translate a vague driver error
            raise RuntimeError(
                f"could not open {self.uri}: {exc}. A Pluto's USB interface "
                "can only be claimed by one handle at a time — check that no "
                "other process (or another entry in this device list) already "
                "holds this radio.") from exc
        self._detect_capabilities()
        # platform defaults target a wideband Pluto+; fit them to whatever this
        # device actually is before pushing values at the driver
        self.config, clamp_notes = self.clamp_config(self.config)
        self._detection_notes.extend(clamp_notes)
        self._apply()
        self._sdr.tx_destroy_buffer()   # never inherit a stale TX buffer
        self.connected = True

    def disconnect(self) -> None:
        super().disconnect()
        self._sdr = None

    def _apply(self) -> None:
        cfg = self.config
        sdr = self._sdr
        sdr.sample_rate = int(cfg.sample_rate_hz)
        sdr.rx_rf_bandwidth = int(cfg.rx_bandwidth_hz)
        sdr.rx_lo = int(cfg.center_frequency_hz)
        sdr.tx_lo = int(cfg.center_frequency_hz)
        sdr.gain_control_mode_chan0 = "manual"
        sdr.rx_hardwaregain_chan0 = cfg.rx_gain_db
        sdr.tx_hardwaregain_chan0 = cfg.tx_gain_db

    def configure(self, cfg) -> None:
        super().configure(cfg)
        if self._sdr is not None:
            self._apply()

    # -- transmit ------------------------------------------------------------
    def enable_tx(self) -> None:
        super().enable_tx()
        wf = self._tx_waveform
        samples = wf.generate() * (2 ** 14)   # Pluto expects ~2^14 full scale
        self._sdr.tx_cyclic_buffer = True
        self._sdr.tx(samples.astype(np.complex64))

    def disable_tx(self) -> None:
        super().disable_tx()
        if self._sdr is not None:
            try:
                self._sdr.tx_destroy_buffer()
            except Exception:  # noqa: BLE001
                pass

    # -- acquisition (burst mode) ---------------------------------------------
    def receive(self, num_samples: int, position: dict | None = None) -> CaptureSegment:
        if not self.connected:
            raise RuntimeError("device not connected")
        if num_samples > MAX_BURST_SAMPLES:
            raise ConfigurationError(
                f"capture of {num_samples} samples exceeds the single-burst DMA "
                f"limit of {MAX_BURST_SAMPLES}; a Pluto cannot stream at full "
                "rate, so long captures must be split into explicit segments")

        sdr = self._sdr
        sdr.rx_destroy_buffer()             # force a fresh buffer of our size
        sdr.rx_buffer_size = int(num_samples)
        buf = np.asarray(sdr.rx())          # one contiguous DMA burst

        loss_events = []
        if len(buf) != num_samples:
            # never conceal a short read (FR-ACQ-003)
            loss_events.append({"type": "short_read",
                                "expected": int(num_samples),
                                "received": int(len(buf))})
        iq = (buf[:num_samples] / (2 ** 11)).astype(np.complex64)  # 12-bit ADC
        clipped = bool(len(iq) and (np.max(np.abs(iq.real)) >= 0.99
                                    or np.max(np.abs(iq.imag)) >= 0.99))
        wf = self._tx_waveform
        return CaptureSegment(
            iq=iq, timestamp=time.time(),
            config=self.config.to_dict(),
            waveform=wf.preview() if wf else None,
            device_id=self.device_id,
            sample_rate_hz=self.config.sample_rate_hz,
            center_frequency_hz=self.config.center_frequency_hz,
            loss_events=loss_events,
            clipped=clipped, position=position,
            telemetry=self.health(), tx_active=self.tx_enabled,
        )

    def health(self) -> dict:
        h = {"time": time.time(), "connected": self.connected,
             "transport": self.uri, "capture_mode": "burst"}
        try:
            # ad9361-phy exposes an on-die temperature sensor channel
            phy = self._sdr._ctrl
            ch = phy.find_channel("temp0")
            h["temperature_c"] = int(ch.attrs["input"].value) / 1000.0
        except Exception:  # noqa: BLE001
            pass
        return h
