"""RF component inventory (FR-RFC-001/002/006): antennas, cables, adapters,
attenuators — each a JSON file with metadata plus imported VNA measurements."""

from __future__ import annotations

import json
import math
import os
import time
import uuid

from .touchstone import (analyze_s11, analyze_s21, loss_at,
                         parse_touchstone)

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

    def describe_chain(self, tx_ids: list, rx_ids: list,
                       antenna_tx: str = "", antenna_rx: str = "") -> dict:
        """Resolve a connector chain for the experiment record (FR-RFC-006).

        Stores the exact components in each path, not just their names, and
        totals their nominal loss and delay where those are known — a chain
        with an unmeasured adapter should not silently read as lossless
        (FR-RFC-007).
        """
        def resolve(ids):
            out, loss, delay, unknown = [], 0.0, 0.0, []
            for cid in ids or []:
                try:
                    c = self.load(cid)
                except FileNotFoundError:
                    unknown.append(cid)
                    continue
                out.append({k: c.get(k) for k in
                            ("component_id", "kind", "name", "connector",
                             "nominal_loss_db", "nominal_delay_ns")})
                if c.get("nominal_loss_db") is None or \
                        c.get("nominal_delay_ns") is None:
                    unknown.append(c["name"])
                loss += c.get("nominal_loss_db") or 0.0
                delay += c.get("nominal_delay_ns") or 0.0
            return out, loss, delay, unknown

        tx, tx_loss, tx_delay, tx_unknown = resolve(tx_ids)
        rx, rx_loss, rx_delay, rx_unknown = resolve(rx_ids)
        chain = {
            "tx_path": tx, "rx_path": rx,
            "antenna_tx": antenna_tx, "antenna_rx": antenna_rx,
            "total_loss_db": round(tx_loss + rx_loss, 2),
            "total_delay_ns": round(tx_delay + rx_delay, 3),
            "components_without_characterisation":
                sorted(set(tx_unknown + rx_unknown)),
        }
        if chain["components_without_characterisation"]:
            chain["note"] = (
                "Totals exclude components with no measured loss or delay; "
                "the real path loss and delay are higher than shown.")
        chain["band"] = self._chain_band(
            [antenna_tx, antenna_rx, *(tx_ids or []), *(rx_ids or [])])
        return chain

    def _recommended_intervals(self, comp_id: str):
        """(name, intervals) for a component, or (name, None) if unmeasured."""
        try:
            c = self.load(comp_id)
        except (FileNotFoundError, ValueError):
            return None, None
        vna = c.get("vna")
        if not vna:
            return c.get("name", comp_id), None
        return c["name"], [(b["start_hz"], b["stop_hz"])
                           for b in vna["analysis"]["bands"]
                           if b["rating"] == "recommended"]

    def _chain_band(self, comp_ids: list) -> dict:
        """Where the whole chain is usable (FR-RFC-004).

        The usable band of a series path is the intersection of its parts:
        an antenna good from 800-1000 MHz behind a filter good from
        900-2000 MHz is a 900-1000 MHz chain. Components with no measurement
        are named rather than assumed transparent — an unmeasured part could
        be narrowing the band and nothing here would know.
        """
        measured, unverified = [], []
        for cid in [c for c in comp_ids if c]:
            name, intervals = self._recommended_intervals(cid)
            if name is None:
                continue
            if intervals is None:
                unverified.append(name)
            else:
                measured.append((name, intervals))

        usable = None
        for _, intervals in measured:
            if usable is None:
                usable = list(intervals)
                continue
            merged = []
            for s1, e1 in usable:
                for s2, e2 in intervals:
                    s, e = max(s1, s2), min(e1, e2)
                    if e > s:
                        merged.append((s, e))
            usable = sorted(merged)

        out = {
            "usable_bands": [{"start_hz": s, "stop_hz": e}
                             for s, e in (usable or [])],
            "measured_components": [n for n, _ in measured],
            "unverified_components": sorted(set(unverified)),
        }
        if measured and not out["usable_bands"]:
            out["note"] = ("The measured components have no frequency range in "
                           "common; this chain has no recommended band.")
        elif not measured:
            out["note"] = ("No component in this chain has a VNA measurement, "
                           "so its usable band is unknown.")
        elif out["unverified_components"]:
            out["note"] = (
                "Range shown is the intersection of the measured components "
                "only. " + ", ".join(out["unverified_components"])
                + " could narrow it further.")
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
        if "s21" in data:
            # A two-port sweep of a cable or attenuator measures the very
            # thing the chain totals need. Without this it was stored and
            # then ignored, leaving the component "uncharacterised" next to
            # its own measurement.
            comp["vna"]["s21_analysis"] = analyze_s21(data["freqs_hz"],
                                                      data["s21"])
        self._save(comp)
        return comp

    def adopt_measured_loss(self, comp_id: str,
                            freq_hz: float | None = None) -> dict:
        """Set nominal loss from an imported S21 sweep (FR-RFC-004).

        Records the frequency the figure was taken at, because cable loss
        rises with frequency and a bare number would be a claim the operator
        could not check.
        """
        comp = self.load(comp_id)
        vna = comp.get("vna") or {}
        s21 = (vna.get("s21_analysis") or {}).get("insertion_loss_db")
        if not s21:
            raise ValueError(
                f"{comp['name']} has no two-port (.s2p) measurement to take "
                "insertion loss from")
        if freq_hz is None:
            freq_hz = vna["s21_analysis"]["at_midband"]["freq_hz"]
        hit = loss_at(vna["freqs_hz"], s21, freq_hz)
        comp["nominal_loss_db"] = hit["loss_db"]
        note = (f"insertion loss {hit['loss_db']} dB measured at "
                f"{hit['freq_hz'] / 1e6:.1f} MHz"
                + (f" (from {vna['filename']})" if vna.get("filename") else ""))
        comp["notes"] = (comp.get("notes", "") + "\n" + note).strip()
        self._save(comp)
        return comp
