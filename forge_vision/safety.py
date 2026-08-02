"""RF safety controller (§11): transmit interlock, limits, audit, fault-safe.

All transmit-enable paths must pass through this controller. The controller
is deliberately independent of any device implementation so a crashed UI or
device adapter cannot leave transmit armed (§4.1).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field

from .config import SafetyLimits


class SafetyViolation(Exception):
    pass


# Receive-path protection (FR-SAF-005). A Pluto's RX input is damaged above
# roughly +2.5 dBm and compresses well before that; the ADC saturates when
# the input plus RX gain exceeds full scale. Transmit output is about
# +7 dBm at 0 dB gain, and tx_hardwaregain is attenuation from there.
#
# The dangerous case is a direct TX->RX cable with no attenuator, which puts
# the full transmit power into the receiver. These numbers are deliberately
# conservative: they are meant to stop an expensive mistake, not to model the
# front end precisely.
PLUTO_TX_MAX_OUTPUT_DBM = 7.0
RX_DAMAGE_DBM = -10.0        # stay well below the absolute maximum
RX_COMPRESSION_DBM = -25.0   # above this the front end is no longer linear
RX_FULL_SCALE_DBM = -30.0    # input + gain beyond this saturates the ADC


def rx_protection_check(tx_gain_db: float, rx_gain_db: float,
                        path_attenuation_db: float,
                        tx_max_output_dbm: float = PLUTO_TX_MAX_OUTPUT_DBM) -> dict:
    """Assess whether a configuration would overdrive the receive path.

    `path_attenuation_db` is what the operator has declared sits between the
    transmit and receive ports: inline attenuators plus antenna isolation.
    Zero means a bare cable, which is the case worth shouting about.
    """
    tx_output_dbm = tx_max_output_dbm + min(0.0, tx_gain_db)
    rx_input_dbm = tx_output_dbm - max(0.0, path_attenuation_db)
    after_gain_dbm = rx_input_dbm + rx_gain_db

    warnings, severity = [], "ok"
    if rx_input_dbm >= RX_DAMAGE_DBM:
        severity = "critical"
        warnings.append(
            f"Estimated {rx_input_dbm:.1f} dBm at the receive port — at or "
            f"above the {RX_DAMAGE_DBM:.0f} dBm damage threshold. Add at "
            f"least {rx_input_dbm - RX_DAMAGE_DBM + 10:.0f} dB of attenuation "
            "before transmitting.")
    elif rx_input_dbm >= RX_COMPRESSION_DBM:
        severity = "warn"
        warnings.append(
            f"Estimated {rx_input_dbm:.1f} dBm at the receive port. The front "
            "end will compress, so amplitudes and ranges derived from this "
            "capture will not be trustworthy.")
    if after_gain_dbm >= RX_FULL_SCALE_DBM and severity != "critical":
        severity = "warn" if severity == "ok" else severity
        over = after_gain_dbm - RX_FULL_SCALE_DBM
        if rx_input_dbm >= RX_FULL_SCALE_DBM:
            # Reducing RX gain cannot help: the signal arriving at the port is
            # already past full scale before any gain is applied. Telling the
            # operator to turn the gain down here sends them to 0 dB, watching
            # the warning refuse to clear, with nothing explaining why.
            past = rx_input_dbm - RX_FULL_SCALE_DBM
            # Mirror of the `ceil() - 1` below. Quoting `past` itself lands
            # the operator *on* full scale, which the `>=` test still trips —
            # so following the advice exactly reproduced this very warning,
            # and at `past` below 1 dB it read "add at least 0 dB".
            needed = math.floor(past) + 1
            # `past` is printed to one decimal and `needed` as a whole number,
            # so 6.96 dB reads as "7.0 dB past ... at least 7 dB more". That
            # looks like the boundary bug this replaced, and is not: 7 clears
            # 6.96. Two reviewers have now nearly re-flagged it.
            warnings.append(
                f"Estimated {rx_input_dbm:.1f} dBm arriving at the receive "
                f"port is {past:.1f} dB past ADC full scale before any "
                "receive gain. Reducing RX gain cannot fix this — even at "
                f"0 dB it would still clip. This needs at least "
                f"{needed:.0f} dB more isolation or attenuation between "
                "transmit and receive.")
        else:
            # The saturation test is `>=`, so the gain that lands *exactly* on
            # full scale still trips it. Quote the largest whole dB strictly
            # below, or the advice reproduces the very defect above: an
            # operator who follows it precisely still sees the warning.
            max_gain = math.ceil(RX_FULL_SCALE_DBM - rx_input_dbm) - 1
            warnings.append(
                f"Receive gain of {rx_gain_db:.0f} dB puts the signal "
                f"{over:.0f} dB past ADC full scale; expect clipping. Reduce "
                f"RX gain to {max_gain:.0f} dB or below.")
    if path_attenuation_db <= 0:
        warnings.append(
            "No path attenuation is declared, so this estimate assumes "
            "transmit is cabled straight into receive with nothing "
            "between them — the worst case, and the number above rests on it. "
            "If the ports are on separate antennas the real isolation is far "
            "higher and these figures are pessimistic; declare it under "
            "Hardware so the estimate describes your bench. If transmit "
            "really is cabled to receive, this will damage the receiver.")
        severity = "critical" if severity != "critical" else severity

    return {
        "severity": severity,
        "safe": severity == "ok",
        "tx_output_dbm": round(tx_output_dbm, 1),
        "rx_input_dbm": round(rx_input_dbm, 1),
        "rx_after_gain_dbm": round(after_gain_dbm, 1),
        "path_attenuation_db": path_attenuation_db,
        "warnings": warnings,
        "thresholds": {"damage_dbm": RX_DAMAGE_DBM,
                       "compression_dbm": RX_COMPRESSION_DBM,
                       "adc_full_scale_dbm": RX_FULL_SCALE_DBM},
    }


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
        # what the operator says is in the TX->RX path (FR-SAF-006)
        self.path_attenuation_db = 0.0
        # device_id -> fingerprint of the configuration TX was approved for.
        # Transmit permission belongs to a configuration, not to a device.
        self._tx_authorizations: dict[str, str] = {}

    def declare_path_attenuation(self, db: float) -> dict:
        """Record the attenuation/isolation the operator has in the path."""
        if db < 0:
            raise SafetyViolation("path attenuation cannot be negative")
        self.path_attenuation_db = float(db)
        self.audit("path_attenuation_declared", attenuation_db=db)
        return {"path_attenuation_db": self.path_attenuation_db}

    def rx_protection(self, tx_gain_db: float, rx_gain_db: float) -> dict:
        """Assess the receive path for the given radio settings (FR-SAF-005)."""
        return rx_protection_check(tx_gain_db, rx_gain_db,
                                   self.path_attenuation_db)

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
    def tx_fingerprint(self, center_frequency_hz: float, waveform,
                       tx_gain_db: float, rx_gain_db: float | None = None,
                       **extra) -> str:
        """A stable digest of everything that was approved for transmit.

        Authorization is granted against a *configuration*, not a device. If
        any of these change, the approval no longer describes what the radio is
        doing and must be withdrawn (FR-SAF-004).
        """
        occupied = waveform.occupied_range(center_frequency_hz)
        payload = {
            "center_frequency_hz": round(float(center_frequency_hz), 3),
            "occupied": [round(x, 3) for x in occupied] if occupied else None,
            "waveform": f"{waveform.name}@{waveform.version}",
            "waveform_bandwidth_hz": float(waveform.bandwidth_hz),
            "sample_rate": float(waveform.sample_rate),
            "amplitude": float(waveform.amplitude),
            "duty_cycle": float(waveform.duty_cycle),
            "tx_gain_db": round(float(tx_gain_db), 3),
            "rx_gain_db": (round(float(rx_gain_db), 3)
                           if rx_gain_db is not None else None),
            "profile": self.limits.active_profile,
            "path_attenuation_db": float(self.path_attenuation_db),
            **extra,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def authorization_for(self, device_id: str) -> str:
        """The fingerprint TX was last authorized against, or ""."""
        with self._lock:
            return self._tx_authorizations.get(device_id, "")

    def revoke_authorization(self, device_id: str) -> None:
        with self._lock:
            self._tx_authorizations.pop(device_id, None)

    def validate_tx(self, center_frequency_hz: float, waveform, tx_gain_db: float,
                    rx_gain_db: float | None = None,
                    enforce_rx_protection: bool = True) -> None:
        """Enforce limits before transmit is enabled (FR-SAF-004, FR-SAF-007).

        Also refuses a configuration that would put damaging power into the
        receiver (FR-SAF-005/006) — the failure mode that costs hardware.
        """
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
        # The whole occupied span must be legal, not just its midpoint. A
        # 56 MHz sweep centred in a 26 MHz allocation passes a centre-only
        # check and transmits outside the allocation for most of every chirp.
        occupied = waveform.occupied_range(center_frequency_hz)
        lo_hz, hi_hz = occupied if occupied else (center_frequency_hz,
                                                  center_frequency_hz)
        span = "" if lo_hz == hi_hz else (
            f" (occupies {lo_hz:.4g}-{hi_hz:.4g} Hz)")
        if not (lims.min_frequency_hz <= lo_hz
                and hi_hz <= lims.max_frequency_hz):
            raise SafetyViolation(
                f"frequency {center_frequency_hz:.4g} Hz outside device policy "
                f"[{lims.min_frequency_hz:.4g}, {lims.max_frequency_hz:.4g}]{span}")
        bands = self.limits.allowed_bands()
        if bands and not any(lo <= lo_hz and hi_hz <= hi for lo, hi in bands):
            raise SafetyViolation(
                f"frequency {center_frequency_hz:.4g} Hz not inside active profile "
                f"'{self.limits.active_profile}'{span}")
        if rx_gain_db is not None:
            check = self.rx_protection(tx_gain_db, rx_gain_db)
            if check["severity"] == "critical":
                if enforce_rx_protection:
                    self.audit("tx_blocked_rx_protection",
                               rx_input_dbm=check["rx_input_dbm"],
                               path_attenuation_db=check["path_attenuation_db"])
                    raise SafetyViolation(
                        "receive path protection: " + " ".join(check["warnings"]))
                # a simulated receiver cannot be damaged, so the same
                # configuration is recorded rather than refused — the operator
                # still sees what it would have meant on real hardware
                self.audit("rx_protection_warning_not_enforced",
                           rx_input_dbm=check["rx_input_dbm"],
                           path_attenuation_db=check["path_attenuation_db"],
                           warnings=check["warnings"])

    # -- tx state tracking -------------------------------------------------
    def authorize_tx(self, device_id: str, fingerprint: str) -> None:
        """Record what this device's transmit permission was granted against."""
        with self._lock:
            self._tx_authorizations[device_id] = fingerprint

    def notify_tx_started(self, device_id: str, **detail) -> None:
        with self._lock:
            self.state.tx_active_devices.add(device_id)
        self.audit("tx_started", device=device_id, **detail)

    def notify_tx_stopped(self, device_id: str, reason: str = "normal") -> None:
        with self._lock:
            self.state.tx_active_devices.discard(device_id)
            self._tx_authorizations.pop(device_id, None)
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
                "path_attenuation_db": self.path_attenuation_db,
            }
