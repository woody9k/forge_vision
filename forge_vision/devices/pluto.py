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

DEFAULT_URIS = ("ip:192.168.2.1", "usb:")

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
    def discover(cls, uris: tuple = DEFAULT_URIS) -> list["PlutoDevice"]:
        found = []
        if not HAVE_ADI:
            return found
        for uri in uris:
            try:
                dev = cls(uri)
                dev.connect()
                found.append(dev)
            except Exception:  # noqa: BLE001 - absent hardware is expected
                continue
        return found

    # -- capability detection (FR-DEV-002) ----------------------------------
    def _read_lo_bounds(self, output: bool) -> tuple[float, float] | None:
        """Best-effort read of LO tuning bounds from the ad9361-phy driver."""
        try:
            phy = self._sdr._ctrl
            # RX LO is altvoltage0, TX LO is altvoltage1 on ad9361-phy
            ch = phy.find_channel("altvoltage1" if output else "altvoltage0", True)
            raw = ch.attrs["frequency_available"].value
            # formats seen in the wild: "[70000000 1 6000000000]"
            nums = [float(x) for x in re.findall(r"[\d.]+", raw)]
            if len(nums) >= 3:
                return nums[0], nums[2]
        except Exception:  # noqa: BLE001 - older firmware lacks the attr
            return None
        return None

    def _detect_capabilities(self) -> None:
        base = STOCK_PLUTO_CAPS
        notes = []
        rx_bounds = self._read_lo_bounds(output=False)
        tx_bounds = self._read_lo_bounds(output=True)
        if rx_bounds:
            lo = max(70e6, min(rx_bounds[0], (tx_bounds or rx_bounds)[0]))
            hi = min(6e9, max(rx_bounds[1], (tx_bounds or rx_bounds)[1]))
            notes.append(f"LO bounds reported by driver: {lo:.4g}-{hi:.4g} Hz")
            wide = hi > 4e9
            self._caps = DeviceCapabilities(
                min_frequency=lo, max_frequency=hi,
                min_sample_rate=0.65e6, max_sample_rate=61.44e6,
                max_bandwidth=56e6 if wide else 20e6,
                rx_channels=base.rx_channels, tx_channels=base.tx_channels,
                max_rx_gain_db=base.max_rx_gain_db,
                min_tx_gain_db=base.min_tx_gain_db,
                max_tx_gain_db=base.max_tx_gain_db,
                transports=base.transports)
            if wide:
                notes.append("wide tuning range: AD9364-class (Pluto+ or "
                             "expanded-range firmware)")
        else:
            self._caps = base
            notes.append("driver did not report LO bounds; assuming stock "
                         "Pluto limits (325 MHz-3.8 GHz, 20 MHz bandwidth)")
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
        self._sdr = adi.Pluto(uri=self.uri)
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
