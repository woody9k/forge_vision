"""Position and pose sources (FR-POS-001..008, UX-SCN-002).

Where the antenna was, and which way it pointed, is half of every image this
platform produces: the RF measurement says how far away a reflector is along
the beam, and the position says where the beam was. Position error therefore
becomes image error directly, which is why FR-POS-008 requires uncertainty to
travel with the measurement rather than being assumed away.

Three sources, all presenting the same `PositionSample`:

* `ManualSource`   — the operator types the position. Exact if the tape
                     measure is honest, and the default.
* `SerialSource`   — a microcontroller streams JSON lines over USB. This is
                     the survey-wheel path: a wheel of known circumference
                     turns an encoder, the counts become distance.
* `ReplaySource`   — positions recorded earlier, for reprocessing a scan
                     without the rig.

The line protocol is deliberately plain text so it can be read with any
terminal and written by any board:

    {"t": 12.345, "x_m": 1.372, "counts": 1830, "heading_deg": 91.2,
     "pitch_deg": -1.1, "roll_deg": 0.4, "fix": "rtk", "quality": 1.0}

Only `x_m` (or `counts`) is required. Anything absent is reported as unknown
rather than defaulted, because a fabricated heading is worse than no heading.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field

try:
    import serial            # pyserial, optional
    HAVE_SERIAL = True
except ImportError:          # pragma: no cover - exercised by absence
    serial = None
    HAVE_SERIAL = False


@dataclass
class PositionSample:
    """One position/pose observation. Unknown fields stay None (FR-POS-003)."""

    x_m: float
    timestamp: float
    source: str
    uncertainty_m: float = 0.05
    heading_deg: float | None = None
    pitch_deg: float | None = None
    roll_deg: float | None = None
    height_m: float | None = None
    antenna_separation_m: float | None = None
    counts: int | None = None
    fix: str = ""                       # gnss fix type if any
    stale_s: float = 0.0                # age when it was consumed
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class PositionSource:
    name = "base"

    def read(self) -> PositionSample | None:
        """Take the next position. May consume, for sources that are a
        sequence rather than a live reading."""
        raise NotImplementedError

    def latest(self) -> PositionSample | None:
        """Observe without consuming — status displays must never advance a
        source and steal a sample the next capture needed."""
        return None

    def close(self) -> None:
        pass

    def status(self) -> dict:
        return {"name": self.name, "connected": True}


class ManualSource(PositionSource):
    """The operator supplies the position for each capture (FR-POS-001)."""

    name = "manual"

    def __init__(self, uncertainty_m: float = 0.01):
        self.uncertainty_m = uncertainty_m
        self._last: PositionSample | None = None

    def set(self, x_m: float, **pose) -> PositionSample:
        self._last = PositionSample(
            x_m=float(x_m), timestamp=time.time(), source=self.name,
            uncertainty_m=self.uncertainty_m,
            **{k: v for k, v in pose.items()
               if k in ("heading_deg", "pitch_deg", "roll_deg", "height_m",
                        "antenna_separation_m")})
        return self._last

    def read(self) -> PositionSample | None:
        return self._last

    def latest(self) -> PositionSample | None:
        return self._last


class ReplaySource(PositionSource):
    """Positions recorded earlier, consumed in order (FR-POS-002 import)."""

    name = "replay"

    def __init__(self, samples: list):
        self._samples = [s if isinstance(s, PositionSample)
                         else PositionSample(**s) for s in samples]
        self._i = 0
        self._last: PositionSample | None = None

    def read(self) -> PositionSample | None:
        if self._i >= len(self._samples):
            return None
        s = self._samples[self._i]
        self._i += 1
        self._last = s
        return s

    def latest(self) -> PositionSample | None:
        # the next one to be consumed, so status shows what a capture would get
        if self._i < len(self._samples):
            return self._samples[self._i]
        return self._last

    def status(self) -> dict:
        return {"name": self.name, "connected": True,
                "remaining": len(self._samples) - self._i}


class SerialSource(PositionSource):
    """A microcontroller streaming JSON position lines over USB (FR-POS-002).

    A reader thread keeps only the most recent sample: a scan point wants
    "where is the antenna now", not a backlog. Samples carry their age so a
    stalled link shows up as stale data rather than a confidently wrong
    position.
    """

    name = "serial"
    MAX_AGE_S = 1.0

    def __init__(self, port: str, baud: int = 115200,
                 wheel_circumference_m: float = 0.0,
                 counts_per_revolution: int = 0,
                 uncertainty_m: float = 0.01, open_fn=None):
        if not HAVE_SERIAL and open_fn is None:
            raise RuntimeError(
                "pyserial is not installed; run `pip install pyserial` to read "
                "positions from a microcontroller")
        self.port = port
        self.baud = baud
        self.wheel_circumference_m = wheel_circumference_m
        self.counts_per_revolution = counts_per_revolution
        self.uncertainty_m = uncertainty_m
        self._latest: PositionSample | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._lines = 0
        self._bad_lines = 0
        self._last_error = ""
        self._serial = (open_fn() if open_fn else
                        serial.Serial(port, baud, timeout=0.5))
        self._thread = threading.Thread(target=self._reader, daemon=True,
                                        name="fv-position")
        self._thread.start()

    def _counts_to_metres(self, counts: float) -> float | None:
        if self.wheel_circumference_m > 0 and self.counts_per_revolution > 0:
            return counts * self.wheel_circumference_m / self.counts_per_revolution
        return None

    def _parse(self, line: str) -> PositionSample | None:
        data = json.loads(line)
        warnings = []
        x = data.get("x_m")
        counts = data.get("counts")
        if x is None and counts is not None:
            x = self._counts_to_metres(float(counts))
            if x is None:
                warnings.append(
                    "encoder counts received but the wheel circumference and "
                    "counts per revolution are not configured, so distance "
                    "cannot be derived")
                return None
        if x is None:
            return None
        return PositionSample(
            x_m=float(x),
            timestamp=float(data.get("t") or time.time()),
            source=self.name,
            uncertainty_m=float(data.get("uncertainty_m", self.uncertainty_m)),
            heading_deg=data.get("heading_deg"),
            pitch_deg=data.get("pitch_deg"),
            roll_deg=data.get("roll_deg"),
            height_m=data.get("height_m"),
            antenna_separation_m=data.get("antenna_separation_m"),
            counts=int(counts) if counts is not None else None,
            fix=data.get("fix", ""), warnings=warnings)

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._serial.readline()
            except Exception as exc:  # noqa: BLE001 - a yanked cable is normal
                self._last_error = str(exc)
                time.sleep(0.2)
                continue
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or line.startswith("#"):
                continue
            self._lines += 1
            try:
                sample = self._parse(line)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                self._bad_lines += 1
                self._last_error = f"{exc}: {line[:80]}"
                continue
            if sample is not None:
                with self._lock:
                    self._latest = sample

    def latest(self) -> PositionSample | None:
        return self.read()          # a live reading is never consumed

    def read(self) -> PositionSample | None:
        with self._lock:
            sample = self._latest
        if sample is None:
            return None
        age = time.time() - sample.timestamp
        sample.stale_s = round(max(0.0, age), 3)
        if age > self.MAX_AGE_S:
            sample.warnings = list(sample.warnings) + [
                f"position is {age:.1f} s old — the link may have stalled; "
                "the antenna may have moved since this was reported"]
        return sample

    def status(self) -> dict:
        with self._lock:
            latest = self._latest
        return {
            "name": self.name, "port": self.port, "baud": self.baud,
            "connected": self._thread.is_alive(),
            "lines_received": self._lines, "bad_lines": self._bad_lines,
            "last_error": self._last_error,
            "wheel_circumference_m": self.wheel_circumference_m,
            "counts_per_revolution": self.counts_per_revolution,
            "latest": latest.to_dict() if latest else None,
        }

    def close(self) -> None:
        self._stop.set()
        try:
            self._serial.close()
        except Exception:  # noqa: BLE001
            pass


def pose_from_sample(sample: PositionSample, plan: dict) -> dict:
    """Build the pose recorded with a capture (FR-POS-003, FR-POS-008).

    Plan values are the fallback; a measured value always wins, and which one
    was used is recorded so a reader can tell an assumption from a reading.
    """
    measured = [k for k in ("heading_deg", "pitch_deg", "roll_deg", "height_m")
                if getattr(sample, k) is not None]
    return {
        "x_m": sample.x_m,
        "uncertainty_m": sample.uncertainty_m,
        "height_m": (sample.height_m if sample.height_m is not None
                     else plan.get("antenna_height_m", 0.0)),
        "heading_deg": sample.heading_deg,
        "pitch_deg": sample.pitch_deg,
        "roll_deg": sample.roll_deg,
        "antenna_separation_m": sample.antenna_separation_m,
        "source": sample.source,
        "measured_fields": measured,
        "assumed_fields": [k for k in ("heading_deg", "pitch_deg", "roll_deg")
                           if getattr(sample, k) is None],
        "counts": sample.counts,
        "fix": sample.fix,
        "stale_s": sample.stale_s,
        "warnings": list(sample.warnings),
        "timestamp": sample.timestamp,
    }
