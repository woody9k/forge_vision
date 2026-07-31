"""Touchstone (.s1p/.s2p) parser for VNA imports (FR-RFC-003).

Handles the format the NanoVNA-F V2 (and NanoVNA-Saver) exports: an options
line `# <freq-unit> S <format> R <impedance>` followed by data rows, with
`!` comments. Supported formats: RI (real/imag), MA (magnitude/angle-deg),
DB (dB-magnitude/angle-deg).
"""

from __future__ import annotations

import cmath
import math

FREQ_UNITS = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}


class TouchstoneError(ValueError):
    pass


def _pair_to_complex(a: float, b: float, fmt: str) -> complex:
    if fmt == "RI":
        return complex(a, b)
    if fmt == "MA":
        return a * complex(math.cos(math.radians(b)), math.sin(math.radians(b)))
    if fmt == "DB":
        mag = 10 ** (a / 20)
        return mag * complex(math.cos(math.radians(b)), math.sin(math.radians(b)))
    raise TouchstoneError(f"unsupported touchstone format: {fmt}")


def parse_touchstone(text: str, num_ports: int | None = None) -> dict:
    """Parse touchstone text -> {freqs_hz, s11, s21?, z0, format}.

    `num_ports` may be inferred from the data width when not given.
    """
    freq_scale = 1e9          # touchstone default is GHz
    fmt = "MA"                # touchstone default
    z0 = 50.0
    rows: list[list[float]] = []

    for raw in text.splitlines():
        line = raw.split("!", 1)[0].strip()
        if not line:
            continue
        if line.startswith("#"):
            tokens = line[1:].upper().split()
            i = 0
            while i < len(tokens):
                tok = tokens[i]
                if tok in FREQ_UNITS:
                    freq_scale = FREQ_UNITS[tok]
                elif tok in ("RI", "MA", "DB"):
                    fmt = tok
                elif tok == "R" and i + 1 < len(tokens):
                    z0 = float(tokens[i + 1])
                    i += 1
                # "S" (parameter type) is accepted and ignored; Y/Z refused
                elif tok in ("Y", "Z", "G", "H"):
                    raise TouchstoneError(
                        f"only S-parameter files are supported, got {tok}")
                i += 1
            continue
        try:
            rows.append([float(x) for x in line.split()])
        except ValueError as exc:
            raise TouchstoneError(f"bad data line: {line!r}") from exc

    if not rows:
        raise TouchstoneError("no data rows found")

    width = len(rows[0])
    if any(len(r) != width for r in rows):
        # s2p files sometimes wrap rows; simplest robust approach: flatten
        flat: list[float] = [v for r in rows for v in r]
        if num_ports == 2 or (num_ports is None and len(flat) % 9 == 0):
            width, rows = 9, [flat[i:i + 9] for i in range(0, len(flat), 9)]
        elif len(flat) % 3 == 0:
            width, rows = 3, [flat[i:i + 3] for i in range(0, len(flat), 3)]
        else:
            raise TouchstoneError("inconsistent row widths")

    if width == 3:
        ports = 1
    elif width == 9:
        ports = 2
    else:
        raise TouchstoneError(f"unexpected column count {width} "
                              "(expected 3 for .s1p or 9 for .s2p)")
    if num_ports and num_ports != ports:
        raise TouchstoneError(f"file has {ports} port(s), expected {num_ports}")

    freqs = [r[0] * freq_scale for r in rows]
    if any(b <= a for a, b in zip(freqs, freqs[1:])):
        raise TouchstoneError("frequencies are not strictly increasing")

    s11 = [_pair_to_complex(r[1], r[2], fmt) for r in rows]
    out = {"freqs_hz": freqs, "s11": s11, "z0": z0, "format": fmt,
           "ports": ports}
    if ports == 2:
        out["s21"] = [_pair_to_complex(r[3], r[4], fmt) for r in rows]
    return out


def analyze_s21(freqs_hz: list, s21: list) -> dict:
    """Insertion loss from a two-port sweep (FR-RFC-004).

    Reported positive-as-loss, which is how an operator talks about a cable.
    Loss is frequency-dependent — roughly with the square root of frequency
    for coax — so the per-point curve is kept and the summary always states
    the frequency a single figure was taken at. A lone "1.4 dB" with no
    frequency attached is not a measurement anyone can check.
    """
    loss_db = []
    for g in s21:
        mag = min(max(abs(g), 1e-9), 1.0)
        loss_db.append(round(-20 * math.log10(mag), 3))
    lo_i, hi_i = 0, len(freqs_hz) - 1
    mid_i = len(freqs_hz) // 2
    return {
        "insertion_loss_db": loss_db,
        "at_lowest": {"freq_hz": freqs_hz[lo_i], "loss_db": loss_db[lo_i]},
        "at_midband": {"freq_hz": freqs_hz[mid_i], "loss_db": loss_db[mid_i]},
        "at_highest": {"freq_hz": freqs_hz[hi_i], "loss_db": loss_db[hi_i]},
        "min_loss_db": min(loss_db),
        "max_loss_db": max(loss_db),
    }


def _unwrap(phases: list) -> list:
    """Unwrap a phase sequence, assuming steps smaller than pi (see below)."""
    out = [phases[0]]
    for i in range(1, len(phases)):
        d = phases[i] - phases[i - 1]
        while d > math.pi:
            d -= 2 * math.pi
        while d < -math.pi:
            d += 2 * math.pi
        out.append(out[-1] + d)
    return out


def _fit_line(xs: list, ys: list) -> tuple:
    """Least-squares slope and intercept for y = a*x + b."""
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        raise ValueError("cannot fit a line to a single frequency")
    a = (n * sxy - sx * sy) / denom
    return a, (sy - a * sx) / n


def analyze_delay(freqs_hz: list, s21: list,
                  min_magnitude_db: float = -30.0) -> dict:
    """Electrical delay from the slope of S21 phase (FR-RFC-004).

    Group delay is -dphi/dw. For a cable the phase is very nearly linear, so
    the slope of a least-squares fit across the whole sweep is both the
    delay and — through its residual — evidence of whether "a delay" was a
    fair description of the part at all.

    Two things make a delay number meaningless rather than merely imprecise,
    and both are checked instead of assumed:

    * **Not enough signal.** With port 2 open, S21 is noise, and noise has a
      phase slope like anything else. Below `min_magnitude_db` the result is
      marked unusable rather than reported (rule 1).
    * **Phase aliasing.** Unwrapping assumes the true phase moves less than pi
      between adjacent points, which caps the measurable delay at
      `1 / (2 * step)`. A longer cable wraps past that and the fit reports a
      *shorter* delay that is just as linear and just as clean — a 40 ns cable
      sampled every 23 MHz reads 3.5 ns with zero residual. **A single sweep
      cannot detect this**, because every symptom is indistinguishable from a
      genuinely short cable. `unambiguous_max_ns` is returned so the caller
      knows the ceiling, and `alias_checked` is False to say plainly that
      nothing here has ruled it out. Comparing two sweeps at different
      frequency steps is what settles it — see `delays_agree()`.

    The sign is reported separately from the magnitude. A delay line is
    `exp(-j*w*tau)` and should show *falling* phase, but this instrument
    reports the conjugate (phase rises with frequency), which would otherwise
    yield a negative delay. A passive cable cannot advance a signal, so the
    magnitude is the physical answer and the direction is recorded as the
    convention observation it is.
    """
    n = len(s21)
    if n < 3 or len(freqs_hz) != n:
        raise ValueError("need at least 3 matched points to fit a delay")

    mags = [abs(x) for x in s21]
    ordered = sorted(mags)
    median_db = 20 * math.log10(max(ordered[n // 2], 1e-12))

    phase = _unwrap([cmath.phase(x) for x in s21])
    omega = [2 * math.pi * f for f in freqs_hz]
    slope, intercept = _fit_line(omega, phase)
    tau = -slope
    resid = [p - (slope * w + intercept) for p, w in zip(phase, omega)]
    resid_rms = math.sqrt(sum(r * r for r in resid) / n)

    # point-wise group delay, as a spread rather than a second estimate
    pointwise = []
    for i in range(1, n):
        dw = omega[i] - omega[i - 1]
        if dw:
            pointwise.append(-(phase[i] - phase[i - 1]) / dw)
    pw_mean = sum(pointwise) / len(pointwise) if pointwise else 0.0
    pw_var = (sum((x - pw_mean) ** 2 for x in pointwise) / len(pointwise)
              if pointwise else 0.0)

    step = (freqs_hz[-1] - freqs_hz[0]) / (n - 1)
    unambiguous = 1.0 / (2 * step) if step else float("inf")

    usable, notes = True, []
    if median_db < min_magnitude_db:
        usable = False
        notes.append(
            f"Median |S21| is {median_db:.1f} dB, below the {min_magnitude_db:.0f} dB "
            "floor for a meaningful phase slope. This looks like an open port, "
            "not a through path — the phase here is noise.")
    notes.append(
        f"Valid only if the true delay is under {unambiguous * 1e9:.1f} ns, the "
        "aliasing limit for this frequency step. A longer part reads short and "
        "looks equally clean, and one sweep cannot tell the difference — "
        "compare two sweeps at different point counts to settle it.")
    if abs(tau) > 0.8 * unambiguous:
        usable = False
        notes.append(
            f"Measured {abs(tau) * 1e9:.2f} ns is close enough to that limit that "
            "the phase has probably already wrapped. Re-sweep with more points.")
    if resid_rms > 0.5:
        notes.append(
            f"Phase is not clean and linear (residual {resid_rms:.2f} rad rms), "
            "so a single delay figure describes this part only loosely.")

    return {
        "delay_ns": round(abs(tau) * 1e9, 3),
        "signed_slope_delay_ns": round(tau * 1e9, 3),
        "phase_convention": ("standard (phase falls with frequency)" if tau > 0
                             else "conjugate (phase rises with frequency)"),
        "fit_residual_rad": round(resid_rms, 4),
        "pointwise_mean_ns": round(abs(pw_mean) * 1e9, 3),
        "pointwise_std_ns": round(math.sqrt(pw_var) * 1e9, 3),
        "median_s21_db": round(median_db, 2),
        "unambiguous_max_ns": round(unambiguous * 1e9, 3),
        "alias_checked": False,     # a single sweep cannot establish this
        "span_hz": [freqs_hz[0], freqs_hz[-1]],
        "points": n,
        "usable": bool(usable),
        "note": " ".join(notes),
    }


def delays_agree(a: dict, b: dict, tolerance_ns: float = 0.5) -> dict:
    """Cross-check two `analyze_delay` results taken at different steps.

    Aliasing folds a long delay down by a whole number of cycles per step, and
    the fold depends on the step — so two sweeps of the same part at different
    point counts agree only if neither wrapped. This is the check a single
    sweep cannot do for itself, and the reason `analyze_delay` refuses to
    claim it has.

    Agreement is evidence, not proof: two steps can in principle alias to the
    same answer. It takes a contrived pair of values to do so, and the
    alternative is reporting a number nothing has tested.
    """
    if not (a.get("usable") and b.get("usable")):
        return {"agree": False, "checked": False,
                "note": "At least one sweep could not support a delay figure, "
                        "so there is nothing to cross-check."}
    if a["points"] == b["points"]:
        return {"agree": False, "checked": False,
                "note": "Both sweeps used the same point count, so they share "
                        "a frequency step and would alias identically. Use "
                        "different point counts."}
    diff = abs(a["delay_ns"] - b["delay_ns"])
    agree = diff <= tolerance_ns
    return {
        "agree": bool(agree),
        "checked": True,
        "delay_ns": round((a["delay_ns"] + b["delay_ns"]) / 2, 3) if agree
                    else None,
        "difference_ns": round(diff, 3),
        "tolerance_ns": tolerance_ns,
        "compared": [{"points": a["points"], "delay_ns": a["delay_ns"]},
                     {"points": b["points"], "delay_ns": b["delay_ns"]}],
        "note": (f"Two sweeps at different frequency steps agree to "
                 f"{diff:.3f} ns, so neither wrapped and the delay is "
                 "unambiguous."
                 if agree else
                 f"Sweeps at different steps disagree by {diff:.3f} ns, which "
                 "means at least one has aliased. The true delay is longer "
                 "than both. Re-sweep with more points."),
    }


def loss_at(freqs_hz: list, loss_db: list, freq_hz: float) -> dict:
    """The measured loss nearest a frequency, with the frequency it came from."""
    i = min(range(len(freqs_hz)), key=lambda j: abs(freqs_hz[j] - freq_hz))
    return {"freq_hz": freqs_hz[i], "loss_db": loss_db[i]}


def analyze_s11(freqs_hz: list, s11: list,
                vswr_recommended: float = 2.0,
                vswr_marginal: float = 3.0) -> dict:
    """Derive display/decision products from S11 (FR-RFC-004).

    Returns per-point return loss and VSWR plus contiguous frequency bands
    classified recommended / marginal / unsuitable.
    """
    s11_db, vswr = [], []
    for g in s11:
        mag = min(abs(g), 0.999999)
        s11_db.append(20 * math.log10(max(mag, 1e-9)))
        vswr.append((1 + mag) / (1 - mag))

    def classify(v: float) -> str:
        if v <= vswr_recommended:
            return "recommended"
        if v <= vswr_marginal:
            return "marginal"
        return "unsuitable"

    bands = []
    start = 0
    for i in range(1, len(vswr) + 1):
        if i == len(vswr) or classify(vswr[i]) != classify(vswr[start]):
            bands.append({
                "start_hz": freqs_hz[start],
                "stop_hz": freqs_hz[i - 1],
                "rating": classify(vswr[start]),
                "min_vswr": round(min(vswr[start:i]), 2),
            })
            start = i

    best = min(range(len(vswr)), key=lambda i: vswr[i])
    return {
        "s11_db": [round(x, 2) for x in s11_db],
        "vswr": [round(x, 3) for x in vswr],
        "bands": bands,
        "best_match": {"freq_hz": freqs_hz[best], "vswr": round(vswr[best], 3),
                       "s11_db": round(s11_db[best], 2)},
        "thresholds": {"recommended_vswr": vswr_recommended,
                       "marginal_vswr": vswr_marginal},
    }
