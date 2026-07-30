"""Touchstone (.s1p/.s2p) parser for VNA imports (FR-RFC-003).

Handles the format the NanoVNA-F V2 (and NanoVNA-Saver) exports: an options
line `# <freq-unit> S <format> R <impedance>` followed by data rows, with
`!` comments. Supported formats: RI (real/imag), MA (magnitude/angle-deg),
DB (dB-magnitude/angle-deg).
"""

from __future__ import annotations

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
