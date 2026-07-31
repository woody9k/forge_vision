"""RF component inventory (FR-RFC-001/002/006): antennas, cables, adapters,
attenuators — each a JSON file with metadata plus imported VNA measurements."""

from __future__ import annotations

import json
import math
import os
import time
import uuid

from .touchstone import (analyze_delay, analyze_s11, analyze_s21, loss_at,
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
        """Attach a touchstone measurement and derived analysis (FR-RFC-003/004).

        A file carries no calibration provenance — nothing in the touchstone
        format records whether the sweep was calibrated, or over what span —
        so the stored record says so rather than leaving it to be assumed.
        """
        return self.attach_measurement(
            comp_id, parse_touchstone(text),
            source={"kind": "file", "filename": filename},
            calibration={"known": False,
                         "note": "Imported from a file. Touchstone carries no "
                                 "calibration record, so whether this sweep was "
                                 "calibrated, and over what span, is unknown."})

    def attach_measurement(self, comp_id: str, data: dict,
                           source: dict | None = None,
                           calibration: dict | None = None) -> dict:
        """Store a parsed sweep plus its derived analysis (FR-RFC-003/004).

        `data` is the structure `parse_touchstone()` returns, which is also
        what `nanovna.NanoVNA.scan()` produces — a file import and a live
        instrument sweep travel the same path from here on.

        `calibration` records how far the numbers can be trusted. It is stored
        verbatim and never defaulted to something reassuring: an absent record
        becomes an explicit "unknown", because a measurement whose calibration
        cannot be established is a different claim from a calibrated one
        (rules 1 and 3).
        """
        comp = self.load(comp_id)
        analysis = analyze_s11(data["freqs_hz"], data["s11"])
        filename = (source or {}).get("filename", "")
        comp["vna"] = {
            "filename": filename,
            "source": source or {"kind": "unknown"},
            "calibration": calibration or {
                "known": False,
                "note": "No calibration provenance was recorded with this "
                        "measurement."},
            "imported_at": time.time(),
            "ports": data["ports"],
            "format": data["format"],
            "z0": data["z0"],
            "freqs_hz": data["freqs_hz"],
            "s11_db": analysis["s11_db"],
            "vswr": analysis["vswr"],
            "s21_db": ([round(20 * math.log10(max(abs(x), 1e-9)), 2)
                        for x in data["s21"]] if "s21" in data else None),
            # Complex values, kept as [real, imag]. Magnitudes alone cannot
            # answer a question about phase, and delay *is* a phase question —
            # storing only dB threw away the one thing electrical delay is
            # derived from, so a stored sweep had to be re-measured to get it.
            "s11_ri": [[round(x.real, 6), round(x.imag, 6)] for x in data["s11"]],
            "s21_ri": ([[round(x.real, 6), round(x.imag, 6)]
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
            try:
                comp["vna"]["delay_analysis"] = analyze_delay(
                    data["freqs_hz"], data["s21"])
            except ValueError:
                # Too few points to fit a slope. Delay is enrichment on top of
                # the measurement, so its absence is a missing field, not a
                # failed import — the sweep itself is still valid data.
                pass
        self._save(comp)
        return comp

    def set_delay(self, comp_id: str, delay_ns: float, note: str) -> dict:
        """Record an electrical delay with the provenance that justifies it."""
        comp = self.load(comp_id)
        comp["nominal_delay_ns"] = round(float(delay_ns), 3)
        comp["notes"] = (comp.get("notes", "") + "\n" + note).strip()
        self._save(comp)
        return comp

    def adopt_measured_delay(self, comp_id: str,
                             reference_plane_ns: float = 0.0) -> dict:
        """Set nominal delay from the S21 phase slope of an imported sweep.

        `reference_plane_ns` is added to the measured figure. A thru
        calibration defines whatever was connected during it as zero, so a
        calibration performed through a jumper has that jumper's delay
        subtracted from every later measurement. The operator is the only one
        who knows what was on the ports, so the correction is theirs to state
        — and it is recorded in the notes as an assumption rather than folded
        in silently, because it is not something this software measured.
        """
        comp = self.load(comp_id)
        vna = comp.get("vna") or {}
        delay = vna.get("delay_analysis")
        if not delay:
            raise ValueError(
                f"{comp['name']} has no two-port (.s2p) measurement to take "
                "electrical delay from")
        if not delay.get("usable"):
            raise ValueError(
                f"{comp['name']}: the sweep cannot support a delay figure. "
                + (delay.get("note") or ""))

        measured = delay["delay_ns"]
        total = round(measured + reference_plane_ns, 3)
        comp["nominal_delay_ns"] = total
        lo, hi = delay["span_hz"]
        note = (f"electrical delay {total} ns from S21 phase slope over "
                f"{lo / 1e6:.1f}-{hi / 1e6:.1f} MHz "
                f"(measured {measured} ns")
        if reference_plane_ns:
            note += (f", plus {reference_plane_ns} ns declared for the "
                     "calibration reference plane — an operator assumption, "
                     "not a measurement")
        note += ")"
        comp["notes"] = (comp.get("notes", "") + "\n" + note).strip()
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
