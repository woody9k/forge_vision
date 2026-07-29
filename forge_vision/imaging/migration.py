"""Focusing / migration for scanned imaging (FR-IMG-006).

A point reflector at depth z under lateral position x0 is seen from antenna
position x at one-way slant range r = sqrt(z^2 + (x - x0)^2). A raw B-scan
therefore smears every compact target into a hyperbola. Diffraction-stack
(Kirchhoff) migration inverts that: for each candidate output cell (x0, z) it
sums the B-scan energy along the hyperbola that cell would have produced.
Real targets sum coherently into a focused spot; everything else averages down.

The migrated image is an *interpretation* of the measurement, so it is stored
as a derived product alongside the raw B-scan rather than replacing it, and
every focused target carries the aperture and velocity assumptions used.
"""

from __future__ import annotations

import numpy as np


def migrate_bscan(positions_m, ranges_m, magnitude_db,
                  depth_step_m: float | None = None,
                  max_depth_m: float | None = None,
                  aperture_m: float | None = None,
                  min_depth_m: float = 0.0,
                  remove_mean_trace: bool = True) -> dict:
    """Diffraction-stack migration of a B-scan.

    Args:
        positions_m: scan-axis coordinate of each column.
        ranges_m: one-way range axis of the B-scan (already velocity-converted).
        magnitude_db: [n_positions][n_ranges], None for un-measured columns.
        aperture_m: half-width of the summation aperture. Beyond roughly the
            depth itself the geometry contributes little and mostly adds
            clutter, so it defaults to max(1.5 m, 1.5x max depth).

    Returns a dict with the migrated amplitude grid and the assumptions used.
    """
    pos = np.asarray(positions_m, dtype=float)
    rng = np.asarray(ranges_m, dtype=float)
    if pos.size < 2 or rng.size < 2:
        raise ValueError("migration needs at least two positions and two range bins")

    # measured columns only — never invent data for gaps (FR-IMG-005)
    amp = np.zeros((pos.size, rng.size))
    measured = np.zeros(pos.size, dtype=bool)
    for i, col in enumerate(magnitude_db):
        if col is None:
            continue
        arr = np.asarray([np.nan if v is None else v for v in col], dtype=float)
        if np.all(np.isnan(arr)):
            continue
        arr = np.nan_to_num(arr, nan=float(np.nanmin(arr)))
        amp[i] = 10 ** (arr / 20.0)      # dB -> linear amplitude
        measured[i] = True
    if measured.sum() < 2:
        raise ValueError("migration needs at least two measured scan points")

    if remove_mean_trace and measured.sum() >= 3:
        # The direct/leakage wave is identical in every trace, so migration
        # would stack it coherently across the whole aperture and bury real
        # targets under a bright shallow band. Removing the per-bin mean of
        # the measured traces cancels anything laterally invariant and leaves
        # the hyperbolas that migration exists to focus.
        mean_trace = amp[measured].mean(axis=0)
        amp[measured] = np.clip(amp[measured] - mean_trace, 0.0, None)

    max_depth_m = float(max_depth_m or rng.max())
    depth_step_m = float(depth_step_m or max(0.02, (rng[1] - rng[0])))
    depths = np.arange(max(min_depth_m, depth_step_m), max_depth_m, depth_step_m)
    if depths.size == 0:
        raise ValueError("no output depths in range")
    if aperture_m is None:
        aperture_m = max(1.5, 1.5 * max_depth_m)

    idx = np.where(measured)[0]
    out = np.zeros((pos.size, depths.size))
    fold = np.zeros((pos.size, depths.size))
    r_max = float(rng.max())

    for oi, x0 in enumerate(pos):
        dx = pos[idx] - x0
        within = np.abs(dx) <= aperture_m
        if not within.any():
            continue
        contrib = idx[within]
        dxw = dx[within]
        # slant range from each contributing trace to every output depth
        r = np.sqrt(depths[None, :] ** 2 + (dxw[:, None] ** 2))
        for k, tr in enumerate(contrib):
            # a trace only supports this cell if the required slant range was
            # actually measured; beyond the range axis there is no data, and
            # summing zeros there manufactures structure out of nothing
            supported = r[k] <= r_max
            out[oi] += np.where(supported,
                                np.interp(r[k], rng, amp[tr], left=0.0, right=0.0),
                                0.0)
            fold[oi] += supported

    # normalise by the number of traces that actually supported each cell, and
    # discard cells too thinly supported to mean anything (FR-IMG-005: never
    # present inferred structure as measurement)
    min_fold = max(3.0, 0.25 * len(idx))
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(fold >= min_fold, out / np.maximum(fold, 1), 0.0)
    supported_depths = fold.max(axis=0) >= min_fold
    if supported_depths.any():
        last = int(np.where(supported_depths)[0].max()) + 1
        out, depths = out[:, :last], depths[:last]

    peak = out.max() if out.size else 0.0
    db = 20 * np.log10(np.maximum(out, 1e-12) / max(peak, 1e-12))

    # How much hyperbola curvature the geometry actually offers, relative to
    # the range resolution, decides whether depth can be focused at all. A
    # shallow target seen through a coarse range cell focuses laterally but
    # not in depth, and the operator should be told that rather than left to
    # infer it from a blurry picture (§17 "Limited instantaneous bandwidth").
    range_res = float(rng[1] - rng[0]) if rng.size > 1 else 0.0
    half_aperture = min(aperture_m, (pos.max() - pos.min()) / 2)
    shallowest = float(depths[0]) if depths.size else 0.0
    curvature = (np.hypot(shallowest, half_aperture) - shallowest
                 if shallowest else 0.0)
    return {
        "positions_m": pos.tolist(),
        "depths_m": depths.tolist(),
        "amplitude_db": np.round(db, 2).tolist(),
        "aperture_m": aperture_m,
        "depth_step_m": depth_step_m,
        "measured_columns": int(measured.sum()),
        "min_fold": min_fold,
        "max_supported_depth_m": round(float(depths[-1]), 3) if depths.size else 0.0,
        "mean_trace_removed": bool(remove_mean_trace and measured.sum() >= 3),
        "range_bin_m": round(range_res, 4),
        "shallow_curvature_m": round(float(curvature), 3),
        "depth_focus_warning": (
            "aperture provides less travel-path curvature than one range bin "
            "at the shallowest imaged depth; lateral position is constrained "
            "but depth is not well focused — widen the scan, or use more "
            "bandwidth for a finer range cell"
            if curvature and range_res and curvature < range_res else ""),
        "method": "diffraction_stack",
        "version": "1.2",
    }


def focused_targets(migrated: dict, threshold_db: float = -12.0,
                    min_separation_m: float = 0.3,
                    max_targets: int = 12) -> list[dict]:
    """Local maxima of a migrated image, as candidate compact reflectors.

    Threshold is relative to the strongest cell in the image, so it reads as
    "within N dB of the brightest thing here" rather than an absolute level.
    """
    db = np.asarray(migrated["amplitude_db"], dtype=float)
    pos = np.asarray(migrated["positions_m"], dtype=float)
    depths = np.asarray(migrated["depths_m"], dtype=float)
    if db.size == 0:
        return []

    # 8-neighbour local maxima above the relative threshold
    cand = []
    for i in range(1, db.shape[0] - 1):
        for j in range(1, db.shape[1] - 1):
            v = db[i, j]
            if v < threshold_db:
                continue
            if v >= db[i - 1:i + 2, j - 1:j + 2].max():
                cand.append((v, i, j))
    cand.sort(reverse=True)

    chosen: list[tuple] = []
    for v, i, j in cand:
        if all((pos[i] - pos[ci]) ** 2 + (depths[j] - depths[cj]) ** 2
               >= min_separation_m ** 2 for _, ci, cj in chosen):
            chosen.append((v, i, j))
        if len(chosen) >= max_targets:
            break

    floor = float(np.median(db))
    return [{
        "x_m": round(float(pos[i]), 3),
        "depth_m": round(float(depths[j]), 3),
        "amplitude_db": round(float(v), 2),
        "contrast_db": round(float(v - floor), 1),
    } for v, i, j in sorted(chosen, key=lambda c: pos[c[1]])]
