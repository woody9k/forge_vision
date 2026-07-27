"""B-scan assembly (FR-IMG-001/002/004/005) with a companion quality layer
(UX-SCN-006). A-scans are indexed by scan position; missing positions may be
interpolated only on request and are always marked as inferred."""

from __future__ import annotations

import numpy as np


class BScanBuilder:
    def __init__(self, plan: dict):
        self.plan = plan
        start = float(plan["start_m"])
        end = float(plan["end_m"])
        step = float(plan["step_m"])
        n = int(round((end - start) / step)) + 1
        self.positions = np.round(start + np.arange(n) * step, 6)
        self.columns: dict[int, dict] = {}     # index -> {profile, quality}
        self.range_axis: list[float] | None = None
        self.resolution_m: float | None = None

    def index_of(self, x_m: float) -> int | None:
        idx = int(round((x_m - self.positions[0]) / float(self.plan["step_m"])))
        if 0 <= idx < len(self.positions):
            return idx
        return None

    def add_column(self, x_m: float, profile: dict, quality: dict,
                   position_uncertainty_m: float = 0.0) -> dict:
        idx = self.index_of(x_m)
        if idx is None:
            raise ValueError(f"position {x_m} m is outside the scan plan")
        if self.range_axis is None:
            self.range_axis = profile["ranges_m"]
            self.resolution_m = profile.get("resolution_m")
        repeated = idx in self.columns
        self.columns[idx] = {
            "magnitude_db": profile["magnitude_db"],
            "quality": {**quality, "position_uncertainty_m": position_uncertainty_m},
            "x_m": float(self.positions[idx]),
            "measured": True,
        }
        return {"index": idx, "repeated": repeated,
                "completed": len(self.columns), "total": len(self.positions)}

    def status(self) -> dict:
        done = sorted(self.columns)
        return {
            "total_points": len(self.positions),
            "completed_points": len(done),
            "pending": [float(self.positions[i]) for i in range(len(self.positions))
                        if i not in self.columns],
            "completed": [float(self.positions[i]) for i in done],
            "low_quality": [float(self.positions[i]) for i in done
                            if self.columns[i]["quality"].get("profile_peak_snr_db", 99) < 10
                            or self.columns[i]["quality"].get("near_clipping")],
        }

    def render(self, interpolate_missing: bool = False, max_bins: int = 400,
               remove_mean_trace: bool = False) -> dict:
        """Assemble the position × range matrix plus quality/inferred masks."""
        if self.range_axis is None:
            # no points captured yet — still a valid (empty) image with status
            return {
                "positions_m": [float(p) for p in self.positions],
                "ranges_m": [],
                "resolution_m": None,
                "magnitude_db": [None] * len(self.positions),
                "inferred_columns": [False] * len(self.positions),
                "quality": {"snr_db": [None] * len(self.positions),
                            "clipped": [False] * len(self.positions),
                            "position_uncertainty_m": [0.0] * len(self.positions)},
                "status": self.status(),
            }
        nbins = len(self.range_axis)
        stride = max(1, nbins // max_bins)
        axis = self.range_axis[::stride]
        ncols = len(self.positions)

        matrix = np.full((ncols, len(axis)), np.nan)
        inferred = np.zeros(ncols, dtype=bool)
        snr = np.full(ncols, np.nan)
        clip = np.zeros(ncols, dtype=bool)
        pos_unc = np.zeros(ncols)

        for i, col in self.columns.items():
            matrix[i] = np.asarray(col["magnitude_db"])[::stride]
            q = col["quality"]
            snr[i] = q.get("profile_peak_snr_db", np.nan)
            clip[i] = bool(q.get("near_clipping", False))
            pos_unc[i] = q.get("position_uncertainty_m", 0.0)

        if remove_mean_trace and len(self.columns) >= 3:
            # classic GPR clutter removal: subtract the mean trace (per-bin
            # average across measured columns) so laterally-invariant returns
            # (leakage, flat layers) vanish and hyperbolas stand out
            measured = sorted(self.columns)
            lin = 10 ** (matrix[measured] / 10)
            mean_trace = np.mean(lin, axis=0)
            for i in measured:
                sub = np.clip(10 ** (matrix[i] / 10) - mean_trace, 1e-14, None)
                matrix[i] = 10 * np.log10(sub)

        if interpolate_missing and len(self.columns) >= 2:
            have = np.array(sorted(self.columns))
            missing = [i for i in range(ncols) if i not in self.columns
                       and have.min() < i < have.max()]
            for i in missing:
                lo = have[have < i].max()
                hi = have[have > i].min()
                w = (i - lo) / (hi - lo)
                matrix[i] = (1 - w) * matrix[lo] + w * matrix[hi]
                inferred[i] = True          # clearly marked (FR-IMG-005)

        return {
            "positions_m": [float(p) for p in self.positions],
            "ranges_m": axis,
            "resolution_m": self.resolution_m,
            "magnitude_db": np.where(np.isnan(matrix), None, np.round(matrix, 1)).tolist(),
            "inferred_columns": inferred.tolist(),
            "mean_trace_removed": bool(remove_mean_trace),
            "quality": {
                "snr_db": np.where(np.isnan(snr), None, np.round(snr, 1)).tolist(),
                "clipped": clip.tolist(),
                "position_uncertainty_m": pos_unc.tolist(),
            },
            "status": self.status(),
        }

    # -- persistence for resume (UX-SCN-008) -------------------------------
    def to_dict(self) -> dict:
        return {
            "plan": self.plan,
            "range_axis": self.range_axis,
            "resolution_m": self.resolution_m,
            "columns": {str(k): v for k, v in self.columns.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BScanBuilder":
        b = cls(d["plan"])
        b.range_axis = d.get("range_axis")
        b.resolution_m = d.get("resolution_m")
        b.columns = {int(k): v for k, v in d.get("columns", {}).items()}
        return b
