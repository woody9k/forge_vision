"""RF safety controller (§11): transmit interlock, limits, audit, fault-safe.

All transmit-enable paths must pass through this controller. The controller
is deliberately independent of any device implementation so a crashed UI or
device adapter cannot leave transmit armed (§4.1).
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

from .config import SafetyLimits


class SafetyViolation(Exception):
    pass


@dataclass
class SafetyState:
    armed: bool = False                 # session interlock (FR-SAF-001)
    armed_by: str = ""
    armed_at: float = 0.0
    tx_active_devices: set = field(default_factory=set)

    @property
    def tx_active(self) -> bool:
        return bool(self.tx_active_devices)


class SafetyController:
    def __init__(self, limits: SafetyLimits, audit_path: str):
        self.limits = limits
        self.state = SafetyState()
        self._lock = threading.RLock()
        self._audit_path = audit_path
        os.makedirs(os.path.dirname(audit_path), exist_ok=True)

    # -- audit -------------------------------------------------------------
    def audit(self, event: str, **detail) -> None:
        """Append-only audit log of safety-relevant events (FR-SAF-010)."""
        record = {"t": time.time(), "event": event, **detail}
        with self._lock, open(self._audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def audit_tail(self, n: int = 100) -> list[dict]:
        try:
            with open(self._audit_path, encoding="utf-8") as f:
                lines = f.readlines()[-n:]
            return [json.loads(x) for x in lines]
        except FileNotFoundError:
            return []

    # -- interlock ---------------------------------------------------------
    def arm(self, operator: str, acknowledgement: str) -> None:
        """Explicit per-session operator action required before any TX (FR-SAF-001)."""
        if not operator or not acknowledgement:
            raise SafetyViolation("arming requires operator name and acknowledgement text")
        with self._lock:
            self.state.armed = True
            self.state.armed_by = operator
            self.state.armed_at = time.time()
        self.audit("tx_armed", operator=operator, acknowledgement=acknowledgement)

    def disarm(self, reason: str = "operator") -> None:
        with self._lock:
            self.state.armed = False
            self.state.tx_active_devices.clear()
        self.audit("tx_disarmed", reason=reason)

    # -- validation --------------------------------------------------------
    def validate_tx(self, center_frequency_hz: float, waveform, tx_gain_db: float) -> None:
        """Enforce limits before transmit is enabled (FR-SAF-004, FR-SAF-007)."""
        lims = self.limits
        if not self.state.armed:
            raise SafetyViolation("transmit interlock is not armed for this session")
        if waveform.amplitude > lims.max_amplitude:
            raise SafetyViolation(
                f"waveform amplitude {waveform.amplitude} exceeds limit {lims.max_amplitude}")
        if waveform.duty_cycle > lims.max_duty_cycle:
            raise SafetyViolation(
                f"duty cycle {waveform.duty_cycle} exceeds limit {lims.max_duty_cycle}")
        if tx_gain_db > lims.max_tx_gain_db:
            raise SafetyViolation(
                f"tx gain {tx_gain_db} dB exceeds limit {lims.max_tx_gain_db} dB")
        if not (lims.min_frequency_hz <= center_frequency_hz <= lims.max_frequency_hz):
            raise SafetyViolation(
                f"frequency {center_frequency_hz:.4g} Hz outside device policy "
                f"[{lims.min_frequency_hz:.4g}, {lims.max_frequency_hz:.4g}]")
        bands = self.limits.allowed_bands()
        if bands and not any(lo <= center_frequency_hz <= hi for lo, hi in bands):
            raise SafetyViolation(
                f"frequency {center_frequency_hz:.4g} Hz not inside active profile "
                f"'{self.limits.active_profile}'")

    # -- tx state tracking -------------------------------------------------
    def notify_tx_started(self, device_id: str, **detail) -> None:
        with self._lock:
            self.state.tx_active_devices.add(device_id)
        self.audit("tx_started", device=device_id, **detail)

    def notify_tx_stopped(self, device_id: str, reason: str = "normal") -> None:
        with self._lock:
            self.state.tx_active_devices.discard(device_id)
        self.audit("tx_stopped", device=device_id, reason=reason)

    # -- emergency / fault safe (FR-SAF-003, FR-SAF-008) --------------------
    def emergency_stop(self, devices, reason: str = "operator_stop") -> list[str]:
        """Disable TX everywhere. Never raises; reports per-device outcome."""
        results = []
        for dev in devices:
            try:
                dev.force_tx_off()
                results.append(f"{dev.device_id}: tx off")
            except Exception as exc:  # noqa: BLE001 - must not fail during e-stop
                results.append(f"{dev.device_id}: ERROR {exc}")
        self.disarm(reason=reason)
        self.audit("emergency_stop", reason=reason, results=results)
        return results

    def status(self) -> dict:
        with self._lock:
            return {
                "armed": self.state.armed,
                "armed_by": self.state.armed_by,
                "armed_at": self.state.armed_at,
                "tx_active": self.state.tx_active,
                "tx_active_devices": sorted(self.state.tx_active_devices),
                "limits": self.limits.to_dict(),
            }
