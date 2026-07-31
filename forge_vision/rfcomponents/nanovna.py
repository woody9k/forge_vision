"""NanoVNA serial driver for automated antenna and cable measurement (FR-RFC-003/004).

Import is optional: the platform runs fully without a VNA attached. When
pyserial and an instrument are present, `discover()` returns the ones it can
actually talk to.

Verified against a **NanoVNA-F V2** (SYSJOINT), firmware 0.6.2, on
`/dev/nanovna` (udev rule `60-nanovna.rules`). It enumerates as an STM32
CDC-ACM port, USB `0483:5740`, at 12 Mbit/s full speed — that VID/PID is
STMicroelectronics' generic virtual COM port and is shared with a great many
unrelated boards, so a candidate port is *probed* (`info`) rather than trusted
on its descriptor, the same way `devices/discovery.py` measures a transport
instead of believing it.

Design notes:

* **`scan` is the acquisition primitive.** `scan {start} {stop} {points} 7`
  returns frequency, S11 and S21 in five columns from one command — 101 points
  in ~0.59 s — where the older `sweep` + `frequencies` + `data 0` + `data 1`
  sequence needs four round trips for the same result. It is *not* read-only
  though: it overwrites the instrument's stored start, stop and point count
  (measured — set 700-3000 MHz/101, scan 800-2000 MHz/201, and the front panel
  reads back 800-2000 MHz/201). `scan()` therefore saves the operator's sweep
  settings and restores them afterwards, so automation does not silently
  reconfigure a bench instrument somebody is also driving by hand.

* **No touchstone round trip.** A NanoVNA measures S11 and S21 only; it has no
  reverse path. Writing a `.s2p` therefore means inventing S12 and S22 —
  NanoVNA-Saver assumes reciprocity for S12 and zeroes S22 — which puts
  fabricated numbers in a file that reads as measured. That is rule 1, so
  `scan()` returns the same structure `parse_touchstone()` produces and skips
  the file format entirely. Operator-supplied `.s2p` files still import
  through `parse_touchstone`; we simply do not manufacture one.

* **Calibration provenance cannot be read back.** The firmware reports *which*
  standards are captured (`cal` -> `load open short thru cal'ed`) but never the
  frequency span they were captured over, and it will silently interpolate a
  calibration onto whatever span is set. A sweep pulled off the instrument
  consequently carries no proof its calibration applies to it. Measuring the
  residual against a known standard is the only honest way to establish that
  it does — see `analyze_thru_residual()`.
"""

from __future__ import annotations

import math
import threading
import time

try:
    import serial
    from serial.tools import list_ports
    HAVE_SERIAL = True
    _SERIAL_ERROR = ""
except Exception as _exc:  # noqa: BLE001
    serial = None
    list_ports = None
    HAVE_SERIAL = False
    _SERIAL_ERROR = str(_exc)


PROMPT = b"ch> "
DEFAULT_BAUD = 115200          # ignored by CDC-ACM, but pyserial wants a number
ST_VCP_VID = 0x0483
ST_VCP_PID = 0x5740

# `scan` output mask bits, verified against firmware 0.6.2.
OUT_FREQ, OUT_S11, OUT_S21 = 1, 2, 4
OUT_ALL = OUT_FREQ | OUT_S11 | OUT_S21

# Measured ceiling on a NanoVNA-F V2 / 0.6.2: 301 points sweep in 1.70 s
# (5.7 ms/point, linear from 101 up). Asking for 401 does not clamp to 301 —
# the firmware returns a single junk row, so the request has to be refused
# here rather than letting a caller receive one point labelled as four hundred.
MAX_SWEEP_POINTS = 301
SECONDS_PER_POINT = 0.0057


class NanoVNAError(RuntimeError):
    """The instrument was unreachable, or answered in a way we cannot trust."""


# -- discovery ---------------------------------------------------------------

def candidate_ports() -> list[dict]:
    """Serial ports whose USB descriptor makes them a possible NanoVNA."""
    if not HAVE_SERIAL:
        return []
    out = []
    for p in list_ports.comports():
        if (p.vid, p.pid) != (ST_VCP_VID, ST_VCP_PID):
            continue
        out.append({
            "port": p.device,
            "vid": p.vid,
            "pid": p.pid,
            "product": p.product or "",
            "serial_number": p.serial_number or "",
        })
    return out


def discover(timeout: float = 2.0) -> list[dict]:
    """Probe every candidate port and return the ones that answer as a VNA.

    The USB descriptor is a hint, not an identification: `0483:5740` is the
    stock ST virtual COM port and is shared with unrelated STM32 boards. Only
    a device that responds to `info` is reported.
    """
    found = []
    for cand in candidate_ports():
        try:
            with NanoVNA(cand["port"], timeout=timeout) as vna:
                ident = vna.identify()
        except Exception as exc:  # noqa: BLE001
            found.append({**cand, "reachable": False, "error": str(exc)})
            continue
        found.append({**cand, "reachable": True, **ident})
    return found


# -- instrument --------------------------------------------------------------

class NanoVNA:
    """A NanoVNA on a serial port. Not thread-safe across instances sharing a port."""

    def __init__(self, port: str = "/dev/nanovna", timeout: float = 2.0,
                 baud: int = DEFAULT_BAUD):
        if not HAVE_SERIAL:
            raise NanoVNAError(
                f"pyserial is not available ({_SERIAL_ERROR}); "
                "install pyserial>=3.5 to use a VNA")
        self.port = port
        self.timeout = timeout
        self.baud = baud
        self._ser = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> "NanoVNA":
        if self._ser is None:
            try:
                self._ser = serial.Serial(self.port, self.baud, timeout=0.3)
            except Exception as exc:  # noqa: BLE001
                raise NanoVNAError(f"cannot open {self.port}: {exc}") from exc
            time.sleep(0.25)          # CDC-ACM settle after DTR assert
            self._ser.reset_input_buffer()
        return self

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False

    # -- protocol ----------------------------------------------------------
    def command(self, cmd: str, wait: float | None = None) -> list[str]:
        """Send a shell command, return its response lines (echo/prompt stripped)."""
        if self._ser is None:
            raise NanoVNAError("instrument is not open")
        wait = self.timeout if wait is None else wait
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write(cmd.encode("ascii") + b"\r\n")
            self._ser.flush()
            buf, last = b"", time.time()
            while time.time() - last < wait:
                n = self._ser.in_waiting
                if n:
                    buf += self._ser.read(n)
                    last = time.time()
                else:
                    time.sleep(0.02)
                if buf.endswith(PROMPT):
                    break
            else:
                raise NanoVNAError(
                    f"no prompt after {wait:.1f}s for {cmd!r} "
                    f"(got {len(buf)} bytes)")
        text = buf.decode("utf-8", "replace")
        lines = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line == cmd or line.startswith("ch>"):
                continue
            lines.append(line)
        return lines

    # -- identity ----------------------------------------------------------
    def identify(self) -> dict:
        """Model, firmware and serial number, as the instrument reports them."""
        info = self.command("info")
        fields = {}
        for line in info:
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.strip().lower()] = v.strip()
        version = self.command("version")
        serial_no = self.command("SN")
        return {
            "model": fields.get("model", ""),
            "frequency_range": fields.get("frequency", ""),
            "build_time": fields.get("build time", ""),
            "firmware": version[0] if version else "",
            "serial_number": serial_no[0] if serial_no else "",
        }

    def battery_mv(self) -> int | None:
        """Battery voltage in mV, or None if the instrument does not report it."""
        out = self.command("vbat")
        if not out:
            return None
        token = out[0].split()[0]
        try:
            return int(token)
        except ValueError:
            return None

    def sweep_settings(self) -> dict:
        """The instrument's own sweep range: {start_hz, stop_hz, points}."""
        out = self.command("sweep")
        if not out:
            raise NanoVNAError("no response to 'sweep'")
        parts = out[0].split()
        if len(parts) < 3:
            raise NanoVNAError(f"unparseable sweep settings: {out[0]!r}")
        return {"start_hz": float(parts[0]), "stop_hz": float(parts[1]),
                "points": int(parts[2])}

    def cal_status(self) -> dict:
        """Which calibration standards are captured, and whether it is applied.

        The span the calibration was taken over is *not* available — the
        firmware does not expose it. `span_known` is therefore always False
        here and exists to keep callers from quietly assuming otherwise;
        establishing the span is `analyze_thru_residual()`'s job.
        """
        out = self.command("cal")
        raw = out[0] if out else ""
        tokens = raw.split()
        known = {"load", "open", "short", "thru", "isoln"}
        return {
            "raw": raw,
            "standards": [t for t in tokens if t in known],
            "applied": "cal'ed" in tokens or "on" in tokens,
            "span_known": False,
        }

    # -- acquisition -------------------------------------------------------
    def scan(self, start_hz: float, stop_hz: float, points: int,
             pause_display: bool = True, restore_sweep: bool = True) -> dict:
        """Sweep and return {freqs_hz, s11, s21, z0, format, ports}.

        The structure matches `parse_touchstone()`'s output so everything
        downstream — `analyze_s11`, `analyze_s21`, `ComponentStore` — consumes
        an instrument sweep and an imported file through one code path.

        A silently clamped sweep is an error, not a result: if the instrument
        returns a different number of points or a different span than was
        asked for, that is reported rather than relabelled (rule 3).

        `scan` overwrites the instrument's stored sweep, so unless
        `restore_sweep` is False the operator's own settings are read first and
        put back afterwards — an automated measurement should not leave the
        front panel showing a range nobody selected.
        """
        if points < 2:
            raise NanoVNAError("a sweep needs at least 2 points")
        if points > MAX_SWEEP_POINTS:
            raise NanoVNAError(
                f"{points} points exceeds the instrument's {MAX_SWEEP_POINTS}-point "
                "limit; it answers an oversized request with a single junk row "
                "rather than clamping, so this is refused here")
        if stop_hz <= start_hz:
            raise NanoVNAError(
                f"stop ({stop_hz:.4g} Hz) must be above start ({start_hz:.4g} Hz)")

        start_i, stop_i = int(round(start_hz)), int(round(stop_hz))
        cmd = f"scan {start_i} {stop_i} {int(points)} {OUT_ALL}"
        # Measured 5.7 ms/point; budget an order of magnitude of slack so a
        # slow instrument is waited for rather than declared unreachable.
        wait = max(self.timeout, 5.0 + points * SECONDS_PER_POINT * 10)

        previous = None
        if restore_sweep:
            try:
                previous = self.sweep_settings()
            except NanoVNAError:
                previous = None       # nothing to restore beats refusing to sweep

        if pause_display:
            self.command("pause")
        try:
            rows = self.command(cmd, wait=wait)
        finally:
            # Always put the instrument back, even if the sweep failed.
            if previous is not None:
                try:
                    self.command(f"sweep {int(previous['start_hz'])} "
                                 f"{int(previous['stop_hz'])} {previous['points']}")
                except Exception:  # noqa: BLE001
                    pass
            if pause_display:
                try:
                    self.command("resume")
                except Exception:  # noqa: BLE001
                    pass

        if len(rows) != points:
            raise NanoVNAError(
                f"asked for {points} points and got {len(rows)}; the "
                "instrument clamped the sweep, so the result is not the "
                "measurement that was requested")

        freqs, s11, s21 = [], [], []
        for i, line in enumerate(rows):
            parts = line.split()
            if len(parts) != 5:
                raise NanoVNAError(
                    f"row {i} has {len(parts)} columns, expected 5: {line!r}")
            try:
                f, a, b, c, d = (float(x) for x in parts)
            except ValueError as exc:
                raise NanoVNAError(f"unparseable row {i}: {line!r}") from exc
            freqs.append(f)
            s11.append(complex(a, b))
            s21.append(complex(c, d))

        if any(b <= a for a, b in zip(freqs, freqs[1:])):
            raise NanoVNAError("returned frequencies are not strictly increasing")
        # Tolerate the instrument's own rounding of the grid, not a different span.
        step = (stop_hz - start_hz) / (points - 1)
        if abs(freqs[0] - start_hz) > step or abs(freqs[-1] - stop_hz) > step:
            raise NanoVNAError(
                f"instrument swept {freqs[0]:.4g}-{freqs[-1]:.4g} Hz, not the "
                f"requested {start_hz:.4g}-{stop_hz:.4g} Hz")

        return {"freqs_hz": freqs, "s11": s11, "s21": s21,
                "z0": 50.0, "format": "RI", "ports": 2}


# -- calibration provenance --------------------------------------------------

def analyze_thru_residual(freqs_hz: list, s21: list,
                          edge_fraction: float = 0.1) -> dict:
    """Judge whether a calibration actually covers the span it is being used on.

    With a known thru connected, a calibration taken over this span drives S21
    to 0 dB across all of it. A calibration taken over a *narrower* span and
    interpolated outward leaves its residual error concentrated at the band
    edges — that asymmetry is the tell, and it is measurable without the
    operator having to remember what they set.

    The verdict is deliberately hedged. Edge residual larger than mid-band
    residual is *consistent with* interpolation, not proof of it: a marginal
    connector or a cable resonance can do the same thing. What this returns is
    evidence to record alongside a measurement, not a certificate.
    """
    n = len(s21)
    if n < 10 or len(freqs_hz) != n:
        raise ValueError("need at least 10 matched points to judge a calibration")

    dev = [abs(20 * math.log10(max(abs(x), 1e-12))) for x in s21]
    k = max(2, int(n * edge_fraction))
    edge = dev[:k] + dev[-k:]
    mid_lo = max(0, n // 2 - k)
    mid = dev[mid_lo:n // 2 + k]

    edge_mean = sum(edge) / len(edge)
    mid_mean = sum(mid) / len(mid)
    worst = max(dev)

    # Thresholds are heuristics chosen against a measured NanoVNA-F V2: a
    # calibration covering its span held every point inside 0.28 dB with the
    # edges *cleaner* than mid-band.
    edges_worse = edge_mean > max(1.5 * mid_mean, mid_mean + 0.05)
    if worst > 1.0:
        verdict = ("Residual exceeds 1 dB. This calibration does not describe "
                   "this span; recalibrate before trusting any sweep from it.")
        covers = False
    elif edges_worse:
        verdict = (f"Residual concentrates at the band edges "
                   f"({edge_mean:.3f} dB vs {mid_mean:.3f} dB mid-band), which "
                   "is consistent with a calibration taken over a narrower "
                   "span and interpolated onto this one.")
        covers = False
    else:
        verdict = (f"Residual is within {worst:.3f} dB and is not concentrated "
                   "at the band edges, consistent with a calibration covering "
                   "this span.")
        covers = True

    return {
        "max_deviation_db": round(worst, 3),
        "mean_deviation_db": round(sum(dev) / n, 3),
        "edge_mean_db": round(edge_mean, 3),
        "mid_mean_db": round(mid_mean, 3),
        "edge_points": k,
        "covers_span": covers,
        "verdict": verdict,
    }
