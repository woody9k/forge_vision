"""Named, reusable RF chain configurations (FR-RFC-006).

A chain is the set of components a reading passes through — antennas, cables,
adapters, attenuators. Which chain was in use decides how a measurement should
be read, so the platform records it with every experiment.

Two things live here:

* the **working chain**, which is what the platform is using right now. There
  is always exactly one, and it is persisted, because an operator who declared
  a chain and then restarted the server would otherwise silently go back to
  "no antenna declared" while captures carried on recording an empty chain.
* **saved configurations**, which are named snapshots of a working chain.
  Activating one loads it back.

A saved configuration is a claim about a physical setup, so the working chain
tracks whether it still matches the configuration it came from. Once an
operator edits a cable, experiments must stop claiming to be from the pristine
named configuration — `modified` says so rather than letting the name imply
something untrue (FR-RFC-007).
"""

from __future__ import annotations

import json
import os
import time
import uuid

# Fields that define a chain. Used for saving, loading and drift detection.
CHAIN_FIELDS = ("tx_ids", "rx_ids", "antenna_tx", "antenna_rx")

_WORKING = "_working.json"


def _blank() -> dict:
    return {"tx_ids": [], "rx_ids": [], "antenna_tx": "", "antenna_rx": ""}


class ChainStore:
    """Persisted working chain plus a library of named configurations."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    # -- paths --------------------------------------------------------------
    def _path(self, config_id: str) -> str:
        # Same containment guard as ComponentStore: a config_id arrives from
        # the API and must not be able to escape the directory.
        if not config_id or config_id.startswith("_"):
            raise ValueError(f"invalid configuration id {config_id!r}")
        path = os.path.normpath(os.path.join(self.root, config_id + ".json"))
        if not path.startswith(os.path.normpath(self.root) + os.sep):
            raise ValueError(f"invalid configuration id {config_id!r}")
        return path

    def _working_path(self) -> str:
        return os.path.join(self.root, _WORKING)

    # -- working chain ------------------------------------------------------
    def working(self) -> dict:
        """The chain in use right now. Always present, always persisted."""
        try:
            with open(self._working_path(), encoding="utf-8") as f:
                w = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            w = {**_blank(), "config_id": "", "updated_at": 0.0}
        for k, v in _blank().items():
            w.setdefault(k, v)
        w.setdefault("config_id", "")
        # A configuration that has been deleted underneath us is no longer a
        # claim we can make about this chain.
        if w["config_id"] and not os.path.exists(
                os.path.join(self.root, w["config_id"] + ".json")):
            w["config_id"] = ""
        w["config_name"] = ""
        w["modified"] = False
        if w["config_id"]:
            cfg = self.load(w["config_id"])
            w["config_name"] = cfg.get("name", "")
            w["modified"] = any(cfg.get(k) != w.get(k) for k in CHAIN_FIELDS)
        return w

    def set_working(self, tx_ids=None, rx_ids=None, antenna_tx: str = "",
                    antenna_rx: str = "", config_id: str | None = None) -> dict:
        """Replace the working chain. Keeps the active configuration unless
        told otherwise, so editing marks it modified rather than detaching."""
        current = self.working()
        w = {
            "tx_ids": [str(x) for x in (tx_ids or [])],
            "rx_ids": [str(x) for x in (rx_ids or [])],
            "antenna_tx": str(antenna_tx or ""),
            "antenna_rx": str(antenna_rx or ""),
            "config_id": current["config_id"] if config_id is None else config_id,
            "updated_at": time.time(),
        }
        tmp = self._working_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(w, f, indent=1)
        os.replace(tmp, self._working_path())
        return self.working()

    # -- saved configurations ----------------------------------------------
    def _save(self, cfg: dict) -> None:
        with open(self._path(cfg["config_id"]), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)

    def load(self, config_id: str) -> dict:
        with open(self._path(config_id), encoding="utf-8") as f:
            return json.load(f)

    def save(self, name: str, tx_ids=None, rx_ids=None, antenna_tx: str = "",
             antenna_rx: str = "", notes: str = "") -> dict:
        """Store a named snapshot and make it the working chain."""
        name = (name or "").strip()
        if not name:
            raise ValueError("a configuration needs a name")
        cfg = {
            "config_id": uuid.uuid4().hex[:10],
            "name": name,
            "notes": notes,
            "tx_ids": [str(x) for x in (tx_ids or [])],
            "rx_ids": [str(x) for x in (rx_ids or [])],
            "antenna_tx": str(antenna_tx or ""),
            "antenna_rx": str(antenna_rx or ""),
            "created_at": time.time(),
            "updated_at": time.time(),
            "measurements": [],
        }
        self._save(cfg)
        self.set_working(cfg["tx_ids"], cfg["rx_ids"], cfg["antenna_tx"],
                         cfg["antenna_rx"], config_id=cfg["config_id"])
        return cfg

    def update(self, config_id: str, fields: dict) -> dict:
        cfg = self.load(config_id)
        for k in ("name", "notes", *CHAIN_FIELDS):
            if k in fields and fields[k] is not None:
                cfg[k] = fields[k]
        cfg["updated_at"] = time.time()
        self._save(cfg)
        return cfg

    def delete(self, config_id: str) -> None:
        os.remove(self._path(config_id))

    def activate(self, config_id: str) -> dict:
        """Load a saved configuration into the working chain."""
        cfg = self.load(config_id)
        self.set_working(cfg["tx_ids"], cfg["rx_ids"], cfg["antenna_tx"],
                         cfg["antenna_rx"], config_id=config_id)
        return cfg

    def save_working_as(self, name: str, notes: str = "") -> dict:
        w = self.working()
        return self.save(name, w["tx_ids"], w["rx_ids"], w["antenna_tx"],
                         w["antenna_rx"], notes=notes)

    def list(self) -> list[dict]:
        active = self.working()["config_id"]
        out = []
        for fn in sorted(os.listdir(self.root)):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            try:
                with open(os.path.join(self.root, fn), encoding="utf-8") as f:
                    cfg = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            cfg["active"] = cfg.get("config_id") == active
            cfg["measurement_count"] = len(cfg.get("measurements", []))
            out.append(cfg)
        out.sort(key=lambda c: c.get("name", "").lower())
        return out

    # -- measurements -------------------------------------------------------
    def record_measurement(self, config_id: str, experiment_id: str,
                           kind: str = "", summary: dict | None = None) -> dict:
        """Attach a measurement taken with this configuration (FR-RFC-004).

        Re-measuring appends rather than replaces: a configuration measured
        twice a month apart is evidence about drift, and overwriting the older
        reading would throw that away.
        """
        cfg = self.load(config_id)
        cfg.setdefault("measurements", [])
        if not any(m.get("experiment_id") == experiment_id
                   for m in cfg["measurements"]):
            cfg["measurements"].append({
                "experiment_id": experiment_id,
                "kind": kind,
                "at": time.time(),
                "summary": summary or {},
            })
            cfg["updated_at"] = time.time()
            self._save(cfg)
        return cfg
