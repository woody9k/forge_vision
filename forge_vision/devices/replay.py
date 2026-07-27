"""Replay device: a finalized experiment acts as a virtual acquisition source
(FR-ACQ-008, AC-006). Reprocessing recorded data never requires hardware."""

from __future__ import annotations

import numpy as np

from .base import CaptureSegment, DeviceAdapter, DeviceCapabilities


class ReplayDevice(DeviceAdapter):
    def __init__(self, experiment, store):
        super().__init__(f"replay-{experiment['identity']['experiment_id']}")
        self._experiment = experiment
        self._store = store
        self._segment_ids = [s["segment_id"] for s in experiment.get("segments", [])]
        self._cursor = 0

    @property
    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            min_frequency=0, max_frequency=1e12,
            min_sample_rate=1, max_sample_rate=1e12, max_bandwidth=1e12)

    @property
    def kind(self) -> str:
        return "replay"

    @property
    def remaining(self) -> int:
        return len(self._segment_ids) - self._cursor

    def rewind(self) -> None:
        self._cursor = 0

    def receive(self, num_samples: int = 0, position: dict | None = None) -> CaptureSegment:
        if self._cursor >= len(self._segment_ids):
            raise EOFError("replay exhausted: no more recorded segments")
        seg_id = self._segment_ids[self._cursor]
        self._cursor += 1
        iq, meta = self._store.load_segment(
            self._experiment["identity"]["experiment_id"], seg_id)
        return CaptureSegment(
            iq=iq.astype(np.complex64),
            timestamp=meta["timestamp"],
            config=meta["config"],
            waveform=meta["waveform"],
            device_id=meta["device_id"],
            sample_rate_hz=meta["sample_rate_hz"],
            center_frequency_hz=meta["center_frequency_hz"],
            loss_events=meta.get("loss_events", []),
            clipped=meta.get("clipped", False),
            position=meta.get("position"),
            telemetry={"replay": True},
            tx_active=meta.get("tx_active", False),
        )
