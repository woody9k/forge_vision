"""NanoVNA driver and VNA measurement path (FR-RFC-003/004).

These run without an instrument attached. The fake below speaks the same
serial protocol as a NanoVNA-F V2 on firmware 0.6.2, including the behaviours
that cost real time to discover on the bench:

  * `scan` overwrites the stored sweep (start, stop *and* points), so the
    driver has to put it back;
  * an oversized point request is not clamped — the firmware answers with a
    single junk row, which would otherwise arrive as one point wearing the
    label of four hundred.
"""

from __future__ import annotations

import cmath
import math

import pytest

from forge_vision.rfcomponents import nanovna
from forge_vision.rfcomponents.nanovna import (MAX_SWEEP_POINTS, NanoVNA,
                                               NanoVNAError,
                                               analyze_thru_residual)


class FakeSerial:
    """A NanoVNA-F V2 on the other end of a CDC-ACM port."""

    def __init__(self, port="/dev/nanovna", baud=115200, timeout=0.3,
                 s11=0.01 + 0j, s21=1.0 + 0j, clamp_to=None, rows_override=None):
        self.port = port
        self.start, self.stop, self.points = 700e6, 3e9, 101
        self.s11, self.s21 = s11, s21
        self.clamp_to = clamp_to          # emulate a firmware that clamps
        self.rows_override = rows_override
        self.commands = []
        self.paused = False
        self._out = b""
        self.closed = False

    # -- pyserial surface --------------------------------------------------
    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def read(self, n: int) -> bytes:
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk

    def reset_input_buffer(self) -> None:
        self._out = b""

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def write(self, data: bytes) -> int:
        cmd = data.decode().strip()
        self.commands.append(cmd)
        self._out += cmd.encode() + b"\r\n" + self._respond(cmd) + b"ch> "
        return len(data)

    # -- instrument behaviour ---------------------------------------------
    def _respond(self, cmd: str) -> bytes:
        head = cmd.split()[0] if cmd else ""
        if head == "info":
            return (b"Model:        NanoVNA-F_V2\r\n"
                    b"Frequency:    50k ~ 3GHz\r\n"
                    b"Build time:   Apr  8 2026 - 13:38:33 CST\r\n")
        if head == "version":
            return b"0.6.2\r\n"
        if head == "SN":
            return b"543435314501538A\r\n"
        if head == "vbat":
            return b"3976 mV\r\n"
        if head == "cal":
            return b"load open short thru cal'ed\r\n"
        if head == "pause":
            self.paused = True
            return b""
        if head == "resume":
            self.paused = False
            return b""
        if head == "sweep":
            parts = cmd.split()
            if len(parts) >= 4:
                self.start, self.stop = float(parts[1]), float(parts[2])
                self.points = int(parts[3])
                return b""
            return f"{int(self.start)} {int(self.stop)} {self.points}\r\n".encode()
        if head == "scan":
            return self._scan(cmd)
        return b""

    def _scan(self, cmd: str) -> bytes:
        parts = cmd.split()
        start, stop, points = float(parts[1]), float(parts[2]), int(parts[3])
        # scan overwrites the stored sweep -- the behaviour the driver restores
        self.start, self.stop, self.points = start, stop, points
        if points > MAX_SWEEP_POINTS:
            return b"1 0 0 0 0\r\n"       # the single junk row, not a clamp
        n = self.clamp_to or points
        if self.rows_override is not None:
            return self.rows_override
        step = (stop - start) / (points - 1)
        out = []
        for i in range(n):
            f = start + i * step
            out.append(f"{f:.0f} {self.s11.real:.6f} {self.s11.imag:.6f} "
                       f"{self.s21.real:.6f} {self.s21.imag:.6f}")
        return ("\r\n".join(out) + "\r\n").encode()


@pytest.fixture
def fake(monkeypatch):
    """Patch pyserial so NanoVNA() talks to the fake instrument."""
    holder = {}

    class _Factory:
        def __call__(self, port, baud, timeout=0.3, **kw):
            holder["dev"] = FakeSerial(port, baud, timeout, **holder.get("kw", {}))
            return holder["dev"]

    monkeypatch.setattr(nanovna, "serial", type("S", (), {"Serial": _Factory()}))
    monkeypatch.setattr(nanovna, "HAVE_SERIAL", True)
    return holder


# -- identity and state ------------------------------------------------------

def test_identify_reads_model_firmware_and_serial(fake):
    with NanoVNA() as v:
        ident = v.identify()
    assert ident["model"] == "NanoVNA-F_V2"
    assert ident["firmware"] == "0.6.2"
    assert ident["serial_number"] == "543435314501538A"
    assert ident["frequency_range"] == "50k ~ 3GHz"


def test_battery_and_sweep_settings(fake):
    with NanoVNA() as v:
        assert v.battery_mv() == 3976
        assert v.sweep_settings() == {"start_hz": 700e6, "stop_hz": 3e9,
                                      "points": 101}


def test_cal_status_never_claims_to_know_the_span(fake):
    """The firmware reports standards but not the span; we must not imply it."""
    with NanoVNA() as v:
        cal = v.cal_status()
    assert cal["applied"] is True
    assert set(cal["standards"]) == {"load", "open", "short", "thru"}
    assert cal["span_known"] is False


# -- acquisition -------------------------------------------------------------

def test_scan_returns_the_touchstone_structure(fake):
    """One code path for a file import and a live sweep (FR-RFC-003)."""
    with NanoVNA() as v:
        data = v.scan(700e6, 3e9, 101)
    assert set(data) == {"freqs_hz", "s11", "s21", "z0", "format", "ports"}
    assert len(data["freqs_hz"]) == 101
    assert data["freqs_hz"][0] == pytest.approx(700e6)
    assert data["freqs_hz"][-1] == pytest.approx(3e9)
    assert data["z0"] == 50.0 and data["ports"] == 2
    assert all(isinstance(x, complex) for x in data["s11"])


def test_scan_restores_the_operators_sweep_settings(fake):
    """Automation must not silently reconfigure a bench instrument."""
    with NanoVNA() as v:
        before = v.sweep_settings()
        v.scan(800e6, 2e9, 201)
        after = v.sweep_settings()
    assert before == after == {"start_hz": 700e6, "stop_hz": 3e9, "points": 101}


def test_scan_restores_sweep_even_when_the_sweep_fails(fake):
    fake["kw"] = {"clamp_to": 7}          # instrument returns the wrong count
    with NanoVNA() as v:
        with pytest.raises(NanoVNAError):
            v.scan(800e6, 2e9, 201)
        assert v.sweep_settings()["points"] == 101


def test_scan_brackets_the_sweep_with_pause_and_resume(fake):
    with NanoVNA() as v:
        v.scan(700e6, 3e9, 101)
        assert fake["dev"].paused is False
    cmds = [c.split()[0] for c in fake["dev"].commands]
    assert cmds.index("pause") < cmds.index("scan") < cmds.index("resume")


def test_clamped_sweep_is_an_error_not_a_result(fake):
    """A short read is a different measurement, not the requested one (rule 3)."""
    fake["kw"] = {"clamp_to": 51}
    with NanoVNA() as v:
        with pytest.raises(NanoVNAError, match="clamped"):
            v.scan(700e6, 3e9, 101)


def test_oversized_request_refused_before_reaching_the_instrument(fake):
    with NanoVNA() as v:
        with pytest.raises(NanoVNAError, match="301-point limit"):
            v.scan(700e6, 3e9, 401)
    assert not any(c.startswith("scan") for c in fake["dev"].commands)


def test_scan_rejects_a_malformed_row(fake):
    """Three columns is an S11-only mask, not the five-column result asked for."""
    fake["kw"] = {"rows_override": b"700000000 0.1 0.2\r\n3000000000 0.1 0.2\r\n"}
    with NanoVNA() as v:
        with pytest.raises(NanoVNAError, match="columns"):
            v.scan(700e6, 3e9, 2)


def test_scan_needs_at_least_two_points(fake):
    with NanoVNA() as v:
        with pytest.raises(NanoVNAError, match="at least 2 points"):
            v.scan(700e6, 3e9, 1)


def test_scan_rejects_a_backwards_span(fake):
    with NanoVNA() as v:
        with pytest.raises(NanoVNAError, match="must be above"):
            v.scan(3e9, 700e6, 101)


# -- calibration provenance --------------------------------------------------

def _flat_thru(n=101, ripple_db=0.0, edge_db=0.0):
    """Synthetic S21: flat 0 dB, with optional ripple and edge-weighted error."""
    out = []
    for i in range(n):
        x = (i / (n - 1)) * 2 - 1            # -1 .. +1
        err = ripple_db * math.sin(i * 0.7) + edge_db * (x ** 2)
        out.append(cmath.rect(10 ** (err / 20), 0.0))
    return out


def test_residual_accepts_a_calibration_that_covers_the_span():
    freqs = [700e6 + i * 23e6 for i in range(101)]
    res = analyze_thru_residual(freqs, _flat_thru(101, ripple_db=0.05))
    assert res["covers_span"] is True
    assert res["max_deviation_db"] < 0.1
    assert "consistent with a calibration covering this span" in res["verdict"]


def test_residual_flags_edge_weighted_error_as_possible_interpolation():
    """Interpolated calibration leaves its error at the band edges."""
    freqs = [700e6 + i * 23e6 for i in range(101)]
    res = analyze_thru_residual(freqs, _flat_thru(101, edge_db=0.4))
    assert res["covers_span"] is False
    assert res["edge_mean_db"] > res["mid_mean_db"]
    assert "interpolated" in res["verdict"]


def test_residual_rejects_a_calibration_that_is_simply_wrong():
    freqs = [700e6 + i * 23e6 for i in range(101)]
    res = analyze_thru_residual(freqs, _flat_thru(101, ripple_db=3.0))
    assert res["covers_span"] is False
    assert "recalibrate" in res["verdict"]


def test_residual_hedges_rather_than_certifying():
    """The verdict is evidence, not a certificate (rule 2)."""
    freqs = [700e6 + i * 23e6 for i in range(101)]
    res = analyze_thru_residual(freqs, _flat_thru(101, edge_db=0.4))
    assert "consistent with" in res["verdict"]


def test_residual_needs_enough_points_to_judge():
    with pytest.raises(ValueError, match="at least 10"):
        analyze_thru_residual([1e9, 2e9], [1 + 0j, 1 + 0j])


# -- storage and provenance --------------------------------------------------

def test_instrument_sweep_records_that_the_cal_span_is_unknown(runtime, fake):
    """A sweep must not read as calibrated when that cannot be established."""
    comp = runtime.components.create("cable", "under test")
    res = runtime.vna_sweep(700e6, 3e9, 101, ports=2,
                            comp_id=comp["component_id"])
    cal = res["component"]["vna"]["calibration"]
    assert cal["known"] is False
    assert cal["applied"] is True          # the instrument says a cal is on...
    assert "not the span" in cal["note"]   # ...but not what it covers


def test_file_import_marks_calibration_unknown(runtime):
    """Touchstone carries no calibration record, so neither do we."""
    comp = runtime.components.create("cable", "from file")
    # a real .s2p carries nine columns: freq + S11 S21 S12 S22
    text = ("# HZ S RI R 50\n"
            "700000000  0.01 0  0.9 0  0.9 0  0.01 0\n"
            "3000000000 0.01 0  0.8 0  0.8 0  0.01 0\n")
    out = runtime.components.import_vna(comp["component_id"], text, "x.s2p")
    assert out["vna"]["calibration"]["known"] is False
    assert out["vna"]["source"]["kind"] == "file"


def test_one_port_sweep_does_not_store_s21_noise(runtime, fake):
    """Port 2 is open on an antenna sweep; its column is not a measurement."""
    comp = runtime.components.create("antenna", "log periodic")
    res = runtime.vna_sweep(700e6, 3e9, 101, ports=1,
                            comp_id=comp["component_id"])
    vna = res["component"]["vna"]
    assert vna["ports"] == 1
    assert vna["s21_db"] is None
    assert "s21_analysis" not in vna


def test_two_port_sweep_does_store_s21(runtime, fake):
    comp = runtime.components.create("cable", "10 ft")
    res = runtime.vna_sweep(700e6, 3e9, 101, ports=2,
                            comp_id=comp["component_id"])
    vna = res["component"]["vna"]
    assert vna["ports"] == 2
    assert vna["s21_db"] is not None
    assert "at_midband" in vna["s21_analysis"]


def test_sweep_rejects_an_impossible_port_count(runtime, fake):
    with pytest.raises(ValueError, match="1 .reflection. or 2"):
        runtime.vna_sweep(700e6, 3e9, 101, ports=3)


# -- rule 5: audited and band-checked, not gated -----------------------------

def test_sweep_is_written_to_the_safety_audit_log(runtime, fake):
    runtime.vna_sweep(700e6, 3e9, 101, ports=2)
    events = [e for e in runtime.safety.audit_tail(50) if e["event"] == "vna_sweep"]
    assert events, "a VNA sweep must leave an audit record"
    rec = events[-1]
    assert rec["start_hz"] == 700e6 and rec["stop_hz"] == 3e9
    assert rec["instrument"] == "NanoVNA-F_V2"
    assert rec["inside_profile"] is True


def test_sweep_outside_the_active_profile_warns_and_is_recorded(runtime, fake):
    """bench_cabled starts at 70 MHz; a 50 kHz sweep leaves the profile."""
    res = runtime.vna_sweep(50e3, 3e9, 101, ports=2)
    band = res["band_check"]
    assert band["inside_profile"] is False
    assert "not inside the active profile" in band["warning"]
    rec = [e for e in runtime.safety.audit_tail(50)
           if e["event"] == "vna_sweep"][-1]
    assert rec["inside_profile"] is False


def test_an_out_of_profile_sweep_is_not_blocked(runtime, fake):
    """The VNA is an instrument, not a transmitter: warn and record, not gate."""
    res = runtime.vna_sweep(50e3, 3e9, 101, ports=2)
    assert res["band_check"]["blocking"] is False
    assert res["points"] == 101          # the sweep still happened


def test_sweep_needs_no_arming(runtime, fake):
    """Measuring a cable must not require arming the transmit interlock."""
    assert runtime.safety.state.armed is False
    res = runtime.vna_sweep(700e6, 3e9, 101, ports=2)
    assert res["points"] == 101


def test_calibration_check_is_audited(runtime, fake):
    runtime.vna_verify_calibration(700e6, 3e9, 101)
    events = [e for e in runtime.safety.audit_tail(50)
              if e["event"] == "vna_calibration_check"]
    assert events and events[-1]["covers_span"] is True


def test_calibration_check_states_what_it_assumed(runtime, fake):
    """It cannot see the port, so it records what the operator was asked for."""
    out = runtime.vna_verify_calibration(700e6, 3e9, 101)
    assert out["assumed_connected"] == "thru"
