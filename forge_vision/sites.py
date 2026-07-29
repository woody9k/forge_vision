"""Site model and cross-scan fusion (release 0.4, §5.6 World View).

A Site is a physical place with a documented local coordinate system
(UX-WLD-001). Scans are *registered* into it by giving each one an origin and
a heading, which is what lets separate B-scans — repeated passes, or
perpendicular lines — be compared in one frame (FR-IMG-009).

Fusion then asks the question release 0.4 exists to answer: which anomalies
recur across independent scans, and how much do those scans actually agree
(FR-INT-002 persistence, FR-INT-009 cross-experiment comparison)? A response
seen once is a candidate; a response seen from two directions at the same
place is evidence.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid


class SiteStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, site_id: str) -> str:
        path = os.path.normpath(os.path.join(self.root, site_id + ".json"))
        if not path.startswith(os.path.normpath(self.root) + os.sep):
            raise ValueError("invalid site id")
        return path

    def create(self, name: str, coordinate_system: str = "",
               notes: str = "", units: str = "meters") -> dict:
        site = {
            "site_id": uuid.uuid4().hex[:10],
            "name": name,
            "coordinate_system": coordinate_system or
                "local site frame: +x east, +y north, origin at site datum",
            "units": units,
            "notes": notes,
            "created_at": time.time(),
            "scans": [],
        }
        self._save(site)
        return site

    def _save(self, site: dict) -> None:
        with open(self._path(site["site_id"]), "w", encoding="utf-8") as f:
            json.dump(site, f, indent=1)

    def load(self, site_id: str) -> dict:
        with open(self._path(site_id), encoding="utf-8") as f:
            return json.load(f)

    def list(self) -> list[dict]:
        out = []
        for fn in sorted(os.listdir(self.root)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(self.root, fn), encoding="utf-8") as f:
                s = json.load(f)
            out.append({"site_id": s["site_id"], "name": s["name"],
                        "created_at": s["created_at"],
                        "num_scans": len(s["scans"]),
                        "notes": s.get("notes", "")})
        return out

    def delete(self, site_id: str) -> None:
        os.remove(self._path(site_id))

    # -- registration (UX-WLD-001, FR-IMG-004) -----------------------------
    def register_scan(self, site_id: str, experiment_id: str,
                      origin_x_m: float = 0.0, origin_y_m: float = 0.0,
                      heading_deg: float = 0.0, label: str = "",
                      position_uncertainty_m: float = 0.05) -> dict:
        """Place a scan line in site coordinates.

        The scan's own axis runs from its origin along `heading_deg`, measured
        counter-clockwise from the +x axis.
        """
        site = self.load(site_id)
        entry = {
            "experiment_id": experiment_id,
            "origin_x_m": float(origin_x_m),
            "origin_y_m": float(origin_y_m),
            "heading_deg": float(heading_deg) % 360.0,
            "label": label or experiment_id,
            "position_uncertainty_m": float(position_uncertainty_m),
            "registered_at": time.time(),
        }
        site["scans"] = [s for s in site["scans"]
                         if s["experiment_id"] != experiment_id]
        site["scans"].append(entry)
        self._save(site)
        return site

    def unregister_scan(self, site_id: str, experiment_id: str) -> dict:
        site = self.load(site_id)
        site["scans"] = [s for s in site["scans"]
                         if s["experiment_id"] != experiment_id]
        self._save(site)
        return site


def scan_to_site(placement: dict, along_m: float) -> tuple[float, float]:
    """Map a distance along a scan line into site coordinates."""
    theta = math.radians(placement["heading_deg"])
    return (placement["origin_x_m"] + along_m * math.cos(theta),
            placement["origin_y_m"] + along_m * math.sin(theta))


def scan_path(placement: dict, positions_m) -> list[list[float]]:
    """The scan line itself, in site coordinates, for map rendering."""
    if not positions_m:
        return []
    return [list(scan_to_site(placement, p))
            for p in (positions_m[0], positions_m[-1])]


def fuse_targets(scan_results: list[dict], tolerance_m: float = 0.6,
                 depth_tolerance_m: float = 0.5) -> list[dict]:
    """Cluster focused targets from several scans into site-frame findings.

    `scan_results` is a list of {placement, experiment_id, targets, medium},
    where targets carry scan-frame x_m and depth_m.

    Confidence follows FR-INT-003: it rises with the number of independent
    scans that agree and with signal contrast, and is capped by how well the
    propagation model is known — an anomaly seen from two directions in a
    medium of unknown permittivity is confidently *located laterally* but not
    confidently placed in depth, and the result says so.
    """
    observations = []
    for res in scan_results:
        placement = res["placement"]
        for t in res["targets"]:
            sx, sy = scan_to_site(placement, t["x_m"])
            observations.append({
                "experiment_id": res["experiment_id"],
                "label": placement.get("label", res["experiment_id"]),
                "site_x_m": sx, "site_y_m": sy,
                "depth_m": t["depth_m"],
                "amplitude_db": t["amplitude_db"],
                "contrast_db": t.get("contrast_db", 0.0),
                "scan_x_m": t["x_m"],
                "medium": res.get("medium", {}),
                "position_uncertainty_m": placement.get(
                    "position_uncertainty_m", 0.05),
            })

    # greedy spatial clustering, strongest observation first
    observations.sort(key=lambda o: -o["contrast_db"])
    clusters: list[list[dict]] = []
    for obs in observations:
        for cl in clusters:
            cx = sum(o["site_x_m"] for o in cl) / len(cl)
            cy = sum(o["site_y_m"] for o in cl) / len(cl)
            cd = sum(o["depth_m"] for o in cl) / len(cl)
            if (math.hypot(obs["site_x_m"] - cx, obs["site_y_m"] - cy) <= tolerance_m
                    and abs(obs["depth_m"] - cd) <= depth_tolerance_m):
                cl.append(obs)
                break
        else:
            clusters.append([obs])

    findings = []
    for cl in clusters:
        scans = sorted({o["experiment_id"] for o in cl})
        xs = [o["site_x_m"] for o in cl]
        ys = [o["site_y_m"] for o in cl]
        ds = [o["depth_m"] for o in cl]
        spread = max((math.hypot(x - sum(xs) / len(xs), y - sum(ys) / len(ys))
                      for x, y in zip(xs, ys)), default=0.0)
        eps_u = max((o["medium"].get("epsilon_r_uncertainty", 0.0) for o in cl),
                    default=0.0)
        findings.append(_finding(cl, scans, xs, ys, ds, spread, eps_u))

    findings.sort(key=lambda f: (-f["supporting_scans"], -f["max_contrast_db"]))
    return findings


def _finding(cl, scans, xs, ys, ds, spread, eps_u) -> dict:
    n_scans = len(scans)
    max_contrast = max(o["contrast_db"] for o in cl)

    if n_scans >= 2 and max_contrast >= 6:
        lateral = "high"
    elif n_scans >= 2 or max_contrast >= 12:
        lateral = "medium"
    else:
        lateral = "low"
    depth_conf = "high" if eps_u == 0 else ("medium" if eps_u <= 1 else "low")

    # depth interval widens with permittivity uncertainty: depth scales as
    # 1/sqrt(eps), so a +/- on eps maps directly onto a depth interval
    mean_depth = sum(ds) / len(ds)
    eps_mean = max((o["medium"].get("epsilon_r", 1.0) for o in cl), default=1.0)
    lo_scale = math.sqrt(eps_mean / max(1.0, eps_mean + eps_u))
    hi_scale = math.sqrt(eps_mean / max(1.0, eps_mean - eps_u)) if eps_u else 1.0
    depth_interval = [round(mean_depth * lo_scale, 3),
                      round(mean_depth * hi_scale, 3)]

    return {
        "site_x_m": round(sum(xs) / len(xs), 3),
        "site_y_m": round(sum(ys) / len(ys), 3),
        "depth_m": round(mean_depth, 3),
        "depth_interval_m": depth_interval,
        "position_spread_m": round(spread, 3),
        "supporting_scans": n_scans,
        "observations": len(cl),
        "scan_ids": scans,
        "max_contrast_db": round(max_contrast, 1),
        "classification": "persistent anomaly, unknown type",   # FR-INT-008
        "confidence": {
            "lateral_position": lateral,
            "depth": depth_conf,
            "overall": min(lateral, depth_conf,
                           key=["low", "medium", "high"].index),
        },
        "evidence": [{"experiment_id": o["experiment_id"],
                      "label": o["label"],
                      "scan_x_m": round(o["scan_x_m"], 3),
                      "depth_m": round(o["depth_m"], 3),
                      "contrast_db": round(o["contrast_db"], 1)} for o in cl],
        "epistemic": {
            "observation": f"focused response in {len(cl)} migrated trace(s) "
                           f"across {n_scans} scan(s)",
            "inference": "lateral position from scan geometry; depth from the "
                         "assumed propagation model",
            "unknown": "material type and object class are not determined by "
                       "these measurements",
        },
    }


def depth_slice(scan_results: list[dict], depth_m: float,
                thickness_m: float = 0.25) -> dict:
    """Plan-view slice at a chosen depth (FR-IMG-003, UX-WLD-003).

    Energy is reported only along the paths that were actually measured. With
    a handful of scan lines there is no basis for filling the plane between
    them, and inventing one would be exactly the false confidence §2.4 forbids.
    """
    samples = []
    for res in scan_results:
        mig = res.get("migrated")
        if not mig:
            continue
        depths = mig["depths_m"]
        band = [j for j, d in enumerate(depths)
                if abs(d - depth_m) <= thickness_m / 2]
        if not band:
            continue
        for i, along in enumerate(mig["positions_m"]):
            row = mig["amplitude_db"][i]
            value = max(row[j] for j in band)
            sx, sy = scan_to_site(res["placement"], along)
            samples.append({"x_m": round(sx, 3), "y_m": round(sy, 3),
                            "amplitude_db": round(value, 2),
                            "experiment_id": res["experiment_id"]})
    return {
        "depth_m": depth_m,
        "thickness_m": thickness_m,
        "samples": samples,
        "coverage_note": "values exist only along measured scan lines; the "
                         "space between lines is unmeasured, not empty",
    }
