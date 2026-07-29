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


# Pre-transmit operator checklist (FR-SAF-009). These are the questions a
# bench operator should be forced to answer before any RF leaves the board.
# `required` items block arming; advisory items are recorded but do not gate.
DEFAULT_CHECKLIST = [
    {"id": "tx_port_loaded",
     "text": "The TX port is terminated, attenuated, or connected to a known "
             "load — not left open with a cable attached that could act as a "
             "radiator.",
     "required": True},
    {"id": "rx_protected",
     "text": "If TX is cabled to RX, at least 30 dB of attenuation is in the "
             "path. A direct TX->RX connection can damage the receiver.",
     "required": True},
    {"id": "frequency_authorised",
     "text": "The chosen frequency is one I am permitted to transmit on in "
             "this environment (see the active frequency profile).",
     "required": True},
    {"id": "power_reviewed",
     "text": "Transmit gain and amplitude limits have been reviewed for this "
             "session.",
     "required": True},
    {"id": "people_clear",
     "text": "No one is close to a radiating antenna.",
     "required": False},
    {"id": "connectors_checked",
     "text": "Connectors and adapters are seated and undamaged.",
     "required": False},
]


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
        self.checklist = [dict(item, confirmed=False)
                          for item in DEFAULT_CHECKLIST]

    # -- pre-transmit checklist (FR-SAF-009) --------------------------------
    def checklist_status(self) -> dict:
        outstanding = [i["id"] for i in self.checklist
                       if i["required"] and not i["confirmed"]]
        return {"items": self.checklist, "outstanding": outstanding,
                "complete": not outstanding}

    def confirm_checklist_item(self, item_id: str, confirmed: bool = True) -> dict:
        for item in self.checklist:
            if item["id"] == item_id:
                item["confirmed"] = bool(confirmed)
                self.audit("checklist_item", item=item_id, confirmed=confirmed)
                return self.checklist_status()
        raise KeyError(f"unknown checklist item: {item_id}")

    def reset_checklist(self) -> dict:
        for item in self.checklist:
            item["confirmed"] = False
        self.audit("checklist_reset")
        return self.checklist_status()

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
        """Explicit per-session operator action required before any TX (FR-SAF-001).

        The required pre-transmit checks must be confirmed first (FR-SAF-009).
        """
        if not operator or not acknowledgement:
            raise SafetyViolation("arming requires operator name and acknowledgement text")
        status = self.checklist_status()
        if not status["complete"]:
            texts = [i["text"] for i in self.checklist
                     if i["id"] in status["outstanding"]]
            raise SafetyViolation(
                "pre-transmit checklist incomplete; confirm: "
                + " | ".join(texts))
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
                "checklist": self.checklist_status(),
            }
