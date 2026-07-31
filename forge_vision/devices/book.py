"""Known radio addresses, kept where an operator can edit them (FR-DEV-002).

Discovery probes a set of candidate URIs. Two of those are fixed defaults that
work anywhere (`usb:`, the RNDIS gadget), but a board on a physical Ethernet
port lives at a site-specific address that the software cannot guess. That
address was originally supplied through an environment variable, which is the
wrong place for it: an operator adds a radio by typing where it is, not by
editing a deployment file and restarting the service.

So addresses live here — a small JSON file, edited through the API, merged
into the candidate list at discovery time. The environment variable still
works and still wins, because a deployment that pins its transports should not
have that silently overridden by something clicked in a browser.
"""

from __future__ import annotations

import json
import os
import time
import uuid


def normalise_uri(raw: str) -> str:
    """Accept what a person would type and produce a libiio URI.

    'pluto.boblab.net', '192.168.99.222' and 'ip:192.168.99.222' are all the
    same intent. Requiring the prefix is a detail of the library, not
    something an operator should have to know.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("an address is required")
    if text.startswith(("ip:", "usb:", "serial:", "local:", "xml:")):
        return text
    return f"ip:{text}"


class RadioBook:
    """Addresses the operator has told us about."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, entries: list[dict]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=1)
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        return sorted(self._load(), key=lambda e: e.get("label", "").lower())

    def add(self, address: str, label: str = "", enabled: bool = True) -> dict:
        uri = normalise_uri(address)
        entries = self._load()
        for e in entries:
            if e["uri"] == uri:
                # Adding the same address twice is a no-op, not an error — the
                # operator's intent is "make sure this is known".
                if label:
                    e["label"] = label
                e["enabled"] = enabled
                self._save(entries)
                return e
        entry = {
            "radio_id": uuid.uuid4().hex[:10],
            "label": label or uri,
            "uri": uri,
            "enabled": enabled,
            "added_at": time.time(),
        }
        entries.append(entry)
        self._save(entries)
        return entry

    def update(self, radio_id: str, fields: dict) -> dict:
        entries = self._load()
        for e in entries:
            if e["radio_id"] == radio_id:
                if fields.get("label") is not None:
                    e["label"] = fields["label"]
                if fields.get("enabled") is not None:
                    e["enabled"] = bool(fields["enabled"])
                if fields.get("address"):
                    e["uri"] = normalise_uri(fields["address"])
                self._save(entries)
                return e
        raise KeyError(f"unknown radio address: {radio_id}")

    def remove(self, radio_id: str) -> dict:
        entries = self._load()
        keep = [e for e in entries if e["radio_id"] != radio_id]
        if len(keep) == len(entries):
            raise KeyError(f"unknown radio address: {radio_id}")
        self._save(keep)
        return {"removed": radio_id}

    def uris(self) -> list[str]:
        return [e["uri"] for e in self.list() if e.get("enabled", True)]
