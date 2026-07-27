"""Experiment package store (§7).

Each experiment is a self-contained directory:

    <root>/<experiment_id>/
        manifest.json            identity, hardware, geometry, rf config,
                                 calibration, provenance, checksums, status
        raw/segment_0000.npy     immutable raw I/Q (FR-DAT-001)
        raw/segment_0000.json    per-segment metadata (FR-ACQ-002)
        derived/<name>.json      processed products with full lineage (FR-DAT-002)
        annotations.json         human review layer (FR-INT-006)

Segments are written incrementally as they arrive (FR-DAT-003); the manifest
is rewritten after every segment so a crash still leaves a readable package
(§12.1 "Experiment finalization"). Export/import is a plain zip (FR-DAT-007);
formats are numpy + JSON, both open and documented (FR-DAT-010).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile

import numpy as np

from .. import __version__


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ExperimentStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    # -- paths -------------------------------------------------------------
    def _dir(self, exp_id: str) -> str:
        path = os.path.normpath(os.path.join(self.root, exp_id))
        if not path.startswith(os.path.normpath(self.root) + os.sep):
            raise ValueError("invalid experiment id")
        return path

    def _manifest_path(self, exp_id: str) -> str:
        return os.path.join(self._dir(exp_id), "manifest.json")

    # -- lifecycle ---------------------------------------------------------
    def create(self, name: str, objective: str = "", operator: str = "",
               tags: list[str] | None = None, hardware: dict | None = None,
               geometry: dict | None = None, rf_config: dict | None = None,
               calibration: dict | None = None, parent_id: str | None = None,
               kind: str = "capture") -> dict:
        exp_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        d = self._dir(exp_id)
        os.makedirs(os.path.join(d, "raw"))
        os.makedirs(os.path.join(d, "derived"))
        manifest = {
            "identity": {
                "experiment_id": exp_id, "name": name, "objective": objective,
                "owner": operator, "tags": tags or [], "kind": kind,
                "started_at": time.time(), "ended_at": None,
                "status": "in_progress",
            },
            "hardware": hardware or {},
            "geometry": geometry or {},
            "rf_config": rf_config or {},
            "calibration": calibration or {},
            "segments": [],
            "derived": [],
            "provenance": {
                "software_version": __version__,
                "parent_experiment": parent_id,
                "checksums": {},
            },
        }
        self._write_manifest(exp_id, manifest)
        with open(os.path.join(d, "annotations.json"), "w", encoding="utf-8") as f:
            json.dump([], f)
        return manifest

    def _write_manifest(self, exp_id: str, manifest: dict) -> None:
        tmp = self._manifest_path(exp_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1)
        os.replace(tmp, self._manifest_path(exp_id))

    def load(self, exp_id: str) -> dict:
        with open(self._manifest_path(exp_id), encoding="utf-8") as f:
            return json.load(f)

    def add_segment(self, exp_id: str, segment) -> dict:
        manifest = self.load(exp_id)
        if manifest["identity"]["status"] == "finalized":
            raise PermissionError("experiment is finalized; raw data is immutable")
        seg_id = f"segment_{len(manifest['segments']):04d}"
        d = self._dir(exp_id)
        npy = os.path.join(d, "raw", seg_id + ".npy")
        meta_path = os.path.join(d, "raw", seg_id + ".json")
        np.save(npy, segment.iq)
        meta = segment.metadata()
        meta["segment_id"] = seg_id
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)
        entry = {
            "segment_id": seg_id,
            "timestamp": meta["timestamp"],
            "num_samples": meta["num_samples"],
            "position": meta.get("position"),
            "loss_events": meta.get("loss_events", []),
            "clipped": meta.get("clipped", False),
        }
        manifest["segments"].append(entry)
        manifest["provenance"]["checksums"][f"raw/{seg_id}.npy"] = _sha256(npy)
        self._write_manifest(exp_id, manifest)     # incremental save (FR-DAT-003)
        return entry

    def load_segment(self, exp_id: str, seg_id: str):
        d = self._dir(exp_id)
        iq = np.load(os.path.join(d, "raw", seg_id + ".npy"))
        with open(os.path.join(d, "raw", seg_id + ".json"), encoding="utf-8") as f:
            meta = json.load(f)
        return iq, meta

    def add_derived(self, exp_id: str, name: str, product: dict,
                    processing_record: dict, sources: list[str]) -> None:
        """Store a processed artifact with full lineage (FR-DAT-002)."""
        manifest = self.load(exp_id)
        d = self._dir(exp_id)
        fname = f"{name}.json"
        payload = {
            "name": name,
            "created_at": time.time(),
            "sources": sources,
            "processing": processing_record,
            "product": product,
        }
        path = os.path.join(d, "derived", fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
        manifest["derived"] = [x for x in manifest["derived"] if x["name"] != name]
        manifest["derived"].append({"name": name, "file": f"derived/{fname}",
                                    "created_at": payload["created_at"],
                                    "sources": sources})
        manifest["provenance"]["checksums"][f"derived/{fname}"] = _sha256(path)
        self._write_manifest(exp_id, manifest)

    def load_derived(self, exp_id: str, name: str) -> dict:
        with open(os.path.join(self._dir(exp_id), "derived", f"{name}.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def annotate(self, exp_id: str, annotation: dict) -> list[dict]:
        """Append-only human annotations; never erases automated results
        (FR-INT-006)."""
        path = os.path.join(self._dir(exp_id), "annotations.json")
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        annotation = {**annotation, "created_at": time.time(),
                      "annotation_id": uuid.uuid4().hex[:8]}
        items.append(annotation)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)
        return items

    def annotations(self, exp_id: str) -> list[dict]:
        with open(os.path.join(self._dir(exp_id), "annotations.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def finalize(self, exp_id: str, status: str = "finalized") -> dict:
        manifest = self.load(exp_id)
        manifest["identity"]["ended_at"] = time.time()
        manifest["identity"]["status"] = status
        self._write_manifest(exp_id, manifest)
        return manifest

    # -- integrity (FR-DAT-004) --------------------------------------------
    def verify(self, exp_id: str) -> dict:
        manifest = self.load(exp_id)
        d = self._dir(exp_id)
        bad, missing = [], []
        for rel, expected in manifest["provenance"]["checksums"].items():
            path = os.path.join(d, rel)
            if not os.path.exists(path):
                missing.append(rel)
            elif _sha256(path) != expected:
                bad.append(rel)
        return {"ok": not bad and not missing, "corrupt": bad, "missing": missing}

    # -- listing / search (FR-DAT-008) -------------------------------------
    def list(self, query: str = "", tag: str = "", kind: str = "") -> list[dict]:
        out = []
        for exp_id in sorted(os.listdir(self.root), reverse=True):
            try:
                m = self.load(exp_id)
            except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
                continue
            ident = m["identity"]
            if query:
                blob = json.dumps(m).lower()
                if query.lower() not in blob:
                    continue
            if tag and tag not in ident.get("tags", []):
                continue
            if kind and ident.get("kind") != kind:
                continue
            out.append({
                **ident,
                "num_segments": len(m["segments"]),
                "derived": [x["name"] for x in m["derived"]],
                "device": m.get("hardware", {}).get("device_id"),
                "parent": m["provenance"].get("parent_experiment"),
            })
        return out

    # -- export / import (FR-DAT-007) --------------------------------------
    def export(self, exp_id: str, dest_zip: str) -> str:
        d = self._dir(exp_id)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for base, _, files in os.walk(d):
                for fn in files:
                    full = os.path.join(base, fn)
                    z.write(full, os.path.join(exp_id, os.path.relpath(full, d)))
        return dest_zip

    def import_package(self, src_zip: str) -> dict:
        with zipfile.ZipFile(src_zip) as z:
            names = z.namelist()
            roots = {n.split("/", 1)[0] for n in names if "/" in n}
            if len(roots) != 1:
                raise ValueError("archive must contain exactly one experiment")
            exp_id = roots.pop()
            target = self._dir(exp_id)
            if os.path.exists(target):
                raise FileExistsError(f"experiment {exp_id} already exists")
            for n in names:
                if os.path.isabs(n) or ".." in n.split("/"):
                    raise ValueError(f"unsafe path in archive: {n}")
            z.extractall(self.root)
        check = self.verify(exp_id)
        if not check["ok"]:
            shutil.rmtree(target, ignore_errors=True)
            raise ValueError(f"imported package failed integrity check: {check}")
        return self.load(exp_id)

    def storage_stats(self) -> dict:
        total = 0
        for base, _, files in os.walk(self.root):
            total += sum(os.path.getsize(os.path.join(base, f)) for f in files)
        usage = shutil.disk_usage(self.root)
        return {
            "experiments_bytes": total,
            "disk_free_bytes": usage.free,
            "disk_total_bytes": usage.total,
            "low_space_warning": usage.free < 2 * (1 << 30),   # FR-DAT-005
        }
