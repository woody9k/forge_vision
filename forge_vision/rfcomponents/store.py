"""RF component inventory (FR-RFC-001/002/006): antennas, cables, adapters,
attenuators — each a JSON file with metadata plus imported VNA measurements."""

from __future__ import annotations

import json
import math
import os
import time
import uuid

from .touchstone import analyze_s11, parse_touchstone

KINDS = ("antenna", "cable", "adapter", "attenuator", "filter", "amplifier",
         "splitter", "termination")


class ComponentStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, comp_id: str) -> str:
        path = os.path.normpath(os.path.join(self.root, comp_id + ".json"))
        if not path.startswith(os.path.normpath(self.root) + os.sep):
            raise ValueError("invalid component id")
        return path

    def create(self, kind: str, name: str, connector: str = "",
               claimed_band: str = "", polarization: str = "",
               notes: str = "", nominal_loss_db: float | None = None,
               nominal_delay_ns: float | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        comp = {
            "component_id": uuid.uuid4().hex[:10],
            "kind": kind, "name": name, "connector": connector,
            "claimed_band": claimed_band, "polarization": polarization,
            "notes": notes,
            "nominal_loss_db": nominal_loss_db,
            "nominal_delay_ns": nominal_delay_ns,
            "created_at": time.time(),
            "vna": None,
        }
        self._save(comp)
        return comp

    def _save(self, comp: dict) -> None:
        with open(self._path(comp["component_id"]), "w", encoding="utf-8") as f:
            json.dump(comp, f, indent=1)

    def load(self, comp_id: str) -> dict:
        with open(self._path(comp_id), encoding="utf-8") as f:
            return json.load(f)

    def update(self, comp_id: str, fields: dict) -> dict:
        comp = self.load(comp_id)
        allowed = {"name", "connector", "claimed_band", "polarization",
                   "notes", "nominal_loss_db", "nominal_delay_ns"}
        comp.update({k: v for k, v in fields.items() if k in allowed})
        self._save(comp)
        return comp

    def delete(self, comp_id: str) -> None:
        os.remove(self._path(comp_id))

    def list(self, kind: str = "") -> list[dict]:
        out = []
        for fn in sorted(os.listdir(self.root)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(self.root, fn), encoding="utf-8") as f:
                comp = json.load(f)
            if kind and comp.get("kind") != kind:
                continue
            summary = {k: comp[k] for k in
                       ("component_id", "kind", "name", "connector",
                        "claimed_band", "polarization", "created_at")}
            vna = comp.get("vna")
            summary["has_vna"] = vna is not None
            if vna:
                summary["best_match"] = vna["analysis"]["best_match"]
                summary["recommended_bands"] = [
                    b for b in vna["analysis"]["bands"]
                    if b["rating"] == "recommended"]
            out.append(summary)
        return out

    def import_vna(self, comp_id: str, text: str, filename: str = "") -> dict:
        """Attach a touchstone measurement and derived analysis (FR-RFC-003/004)."""
        comp = self.load(comp_id)
        data = parse_touchstone(text)
        analysis = analyze_s11(data["freqs_hz"], data["s11"])
        comp["vna"] = {
            "filename": filename,
            "imported_at": time.time(),
            "ports": data["ports"],
            "format": data["format"],
            "z0": data["z0"],
            "freqs_hz": data["freqs_hz"],
            "s11_db": analysis["s11_db"],
            "vswr": analysis["vswr"],
            "s21_db": ([round(20 * math.log10(max(abs(x), 1e-9)), 2)
                        for x in data["s21"]] if "s21" in data else None),
            "analysis": {k: analysis[k] for k in
                         ("bands", "best_match", "thresholds")},
        }
        self._save(comp)
        return comp
