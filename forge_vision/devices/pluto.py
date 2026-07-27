"""Real Pluto/Pluto+ adapter via pyadi-iio.

Import is optional: the platform runs fully without hardware. When pyadi-iio
and a device are present, `PlutoDevice.discover()` returns adapters for each
reachable radio (FR-DEV-001, FR-DEV-008).
"""

from __future__ import annotations

import time

import numpy as np

from .base import CaptureSegment, DeviceAdapter, DeviceCapabilities
from .simulated import PLUTO_PLUS_CAPS

try:
    import adi  # type: ignore
    HAVE_ADI = True
except ImportError:
    adi = None
    HAVE_ADI = False


class PlutoDevice(DeviceAdapter):
    def __init__(self, uri: str = "ip:192.168.2.1", device_id: str | None = None):
        super().__init__(device_id or f"pluto-{uri}")
        if not HAVE_ADI:
            raise RuntimeError(
                "pyadi-iio is not installed; run `pip install pyadi-iio pylibiio` "
                "to control physical Pluto hardware")
        self.uri = uri
        self._sdr = None

    @classmethod
    def discover(cls, uris: tuple = ("ip:192.168.2.1", "usb:")) -> list["PlutoDevice"]:
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

    @property
    def capabilities(self) -> DeviceCapabilities:
        return PLUTO_PLUS_CAPS

    @property
    def kind(self) -> str:
        return "pluto_plus"

    def connect(self) -> None:
        self._sdr = adi.Pluto(uri=self.uri)
        self._apply()
        self._sdr.tx_destroy_buffer()
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
        sdr.rx_buffer_size = cfg.buffer_size

    def configure(self, cfg) -> None:
        super().configure(cfg)
        if self._sdr is not None:
            self._apply()

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

    def receive(self, num_samples: int, position: dict | None = None) -> CaptureSegment:
        if not self.connected:
            raise RuntimeError("device not connected")
        chunks, collected = [], 0
        while collected < num_samples:
            buf = np.asarray(self._sdr.rx()) / (2 ** 11)   # 12-bit ADC scaling
            chunks.append(buf)
            collected += len(buf)
        iq = np.concatenate(chunks)[:num_samples].astype(np.complex64)
        clipped = bool(np.max(np.abs(iq.real)) >= 0.99 or np.max(np.abs(iq.imag)) >= 0.99)
        wf = self._tx_waveform
        return CaptureSegment(
            iq=iq, timestamp=time.time(),
            config=self.config.to_dict(),
            waveform=wf.preview() if wf else None,
            device_id=self.device_id,
            sample_rate_hz=self.config.sample_rate_hz,
            center_frequency_hz=self.config.center_frequency_hz,
            clipped=clipped, position=position,
            telemetry=self.health(), tx_active=self.tx_enabled,
        )

    def health(self) -> dict:
        h = {"time": time.time(), "connected": self.connected, "transport": self.uri}
        try:
            h["temperature_c"] = self._sdr._ctrl.channels[0].attrs  # best effort
        except Exception:  # noqa: BLE001
            pass
        return h
