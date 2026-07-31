"""Find the radio, measure every way in, and choose one (FR-DEV-002).

A Pluto+ can be reached over several transports at once — the USB backend, the
RNDIS gadget on 192.168.2.1, and a physical Ethernet port. They are not
equivalent: measured on this bench, Ethernet moves 52.6 MB/s against USB's
28.3 and doubles the live frame rate. They are also, crucially, *the same
radio*, so registering two of them produces two device entries with
independent cached configuration that drifts apart in silence — one was
observed reporting 923 MHz while the hardware and the other entry were at
1090 MHz. A capture taken through the stale entry would record an RF config
the radio never had.

So discovery does three things: probe each candidate, group the ones that look
like the same board, and pick the fastest transport per board — with the
operator able to override the choice, because "fastest" is not always what you
want. USB needs no network at all, which matters when the LAN is the thing you
are debugging.

Identity is reported with its confidence rather than asserted. A Pluto with an
empty `hw_serial` — the common case — cannot be told apart from an identical
sibling by attributes alone, and this says so instead of guessing.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

# Candidate transports probed when the operator has not named one.
#   usb:            direct backend, no network involved
#   ip:pluto.local  mDNS; finds the board without hardcoding an address, though
#                   it often resolves to the USB gadget rather than Ethernet
#   ip:192.168.2.1  the RNDIS gadget's fixed default
# A board on a real Ethernet port has a site-specific address, so put it in
# FORGE_VISION_PLUTO_URIS (comma-separated) to have it probed automatically.
DEFAULT_CANDIDATES = ("usb:", "ip:pluto.local", "ip:192.168.2.1")

# Attributes that identify a board. hw_serial is the only one that is unique
# per unit; the rest match across identical hardware running identical firmware.
IDENTITY_ATTRS = ("hw_serial", "hw_model", "hw_model_variant", "fw_version",
                  "local,kernel", "ad9361-phy,xo_correction")

# Samples read when timing a transport. Large enough that the transfer, not the
# per-call overhead, dominates: ~4 MB, which is ~75 ms on Ethernet and ~140 ms
# on USB. Receive only — nothing here keys a transmitter.
PROBE_SAMPLES = 1 << 20


def candidate_uris(extra: tuple = (), book: tuple = ()) -> list[str]:
    """Candidates to probe.

    FORGE_VISION_PLUTO_URIS wins outright when set: a deployment that pins its
    transports should not have that overridden by something clicked in a
    browser. Otherwise the operator's saved addresses are probed alongside the
    fixed defaults.
    """
    env = os.environ.get("FORGE_VISION_PLUTO_URIS", "").strip()
    if env:
        base = [u.strip() for u in env.split(",") if u.strip()]
    else:
        base = [*book, *DEFAULT_CANDIDATES]
    out = []
    for uri in [*extra, *base]:
        if uri and uri not in out:
            out.append(uri)
    return out


_RESOLVED: dict[str, str] = {}


def resolve_host(host: str) -> str:
    """Best-effort A record for a hostname; "" if it will not resolve."""
    if not host:
        return ""
    if host[0].isdigit():
        return host
    if host in _RESOLVED:
        return _RESOLVED[host]
    try:
        import socket
        addr = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except Exception:  # noqa: BLE001 - an unresolvable name is a real answer
        addr = ""
    _RESOLVED[host] = addr
    return addr


def uri_kind(uri: str, resolve: bool = True) -> str:
    """How the radio is actually attached.

    Classified by the *resolved* address, not the URI text. `pluto.local`
    reads like a network address but mDNS answers it with the RNDIS gadget on
    192.168.2.1 — calling that "Ethernet" would offer the operator a switch to
    a link they are effectively already on, at a third of the throughput.
    """
    if uri.startswith("usb:"):
        return "usb"
    if uri.startswith("ip:"):
        host = uri[3:]
        addr = resolve_host(host) if resolve else host
        if addr.startswith("192.168.2."):
            return "usb-gadget"
        return "network"
    return "other"


def uri_address(uri: str) -> str:
    """The host part of an ip: URI, with the resolved address if it is a name."""
    if not uri.startswith("ip:"):
        return ""
    host = uri[3:]
    addr = resolve_host(host)
    return f"{host} ({addr})" if addr and addr != host else host


@dataclass
class TransportProbe:
    """What one candidate URI turned out to be."""

    uri: str
    reachable: bool = False
    error: str = ""
    identity: dict = field(default_factory=dict)
    open_seconds: float | None = None
    throughput_mb_s: float | None = None

    @property
    def kind(self) -> str:
        return uri_kind(self.uri)

    def to_dict(self) -> dict:
        return {
            "uri": self.uri, "kind": self.kind, "reachable": self.reachable,
            "error": self.error, "identity": self.identity,
            "open_seconds": (round(self.open_seconds, 3)
                             if self.open_seconds is not None else None),
            "throughput_mb_s": (round(self.throughput_mb_s, 1)
                                if self.throughput_mb_s is not None else None),
        }


def probe(uri: str, measure: bool = True,
          samples: int = PROBE_SAMPLES) -> TransportProbe:
    """Open a candidate, read its identity, and optionally time a read."""
    result = TransportProbe(uri=uri)
    try:
        import adi
    except Exception as exc:  # noqa: BLE001
        result.error = f"pyadi-iio unavailable: {exc}"
        return result

    sdr = None
    try:
        t0 = time.time()
        sdr = adi.Pluto(uri=uri)
        result.open_seconds = time.time() - t0
        result.reachable = True
        try:
            attrs = sdr._ctx.attrs
            result.identity = {k: str(attrs.get(k, "")) for k in IDENTITY_ATTRS
                               if attrs.get(k, "") != ""}
        except Exception:  # noqa: BLE001 - identity is best-effort
            pass
        if measure:
            try:
                sdr.rx_destroy_buffer()
                sdr.rx_buffer_size = int(samples)
                t0 = time.time()
                buf = sdr.rx()                       # receive only
                elapsed = time.time() - t0
                got = len(buf)
                if elapsed > 0 and got:
                    # complex samples: 2 x int16 on the wire
                    result.throughput_mb_s = (got * 4) / elapsed / (1 << 20)
            except Exception as exc:  # noqa: BLE001
                # Reachable but unusable is a real state — a wedged DMA answers
                # attribute reads and times out on buffers. Say so.
                result.error = f"opened but could not read samples: {exc}"
    except Exception as exc:  # noqa: BLE001 - an absent transport is expected
        result.error = str(exc)
    finally:
        if sdr is not None:
            try:
                sdr.rx_destroy_buffer()
            except Exception:  # noqa: BLE001
                pass
            del sdr
    return result


def survey(uris: list[str] | None = None, measure: bool = True) -> list[TransportProbe]:
    return [probe(u, measure=measure) for u in (uris or candidate_uris())]


def _fingerprint(identity: dict) -> tuple[str, str]:
    """(key, confidence) for grouping probes onto physical boards."""
    serial = (identity.get("hw_serial") or "").strip()
    if serial:
        return serial, "serial"
    stable = "|".join(f"{k}={identity.get(k, '')}" for k in IDENTITY_ATTRS
                      if k != "hw_serial")
    return stable, "attributes"


def group_boards(probes: list[TransportProbe]) -> list[dict]:
    """Cluster reachable probes that appear to be the same physical radio.

    Grouping by attributes alone cannot distinguish two identical boards
    running identical firmware, so the confidence is reported rather than the
    conclusion being asserted (FR-DEV-002).
    """
    boards: dict[str, dict] = {}
    for p in probes:
        if not p.reachable:
            continue
        key, confidence = _fingerprint(p.identity)
        b = boards.setdefault(key, {"transports": [], "identity": p.identity,
                                    "identified_by": confidence})
        b["transports"].append(p)
    out = []
    for b in boards.values():
        ts = rank(b["transports"])
        entry = {
            "identity": b["identity"],
            "identified_by": b["identified_by"],
            "transports": [t.to_dict() for t in ts],
            "best_uri": ts[0].uri if ts else "",
        }
        if b["identified_by"] == "attributes" and len(ts) > 1:
            entry["note"] = (
                "These transports share every identifying attribute but the "
                "board reports no serial number, so they are treated as one "
                "radio. Two identical Plutos on identical firmware would look "
                "the same here — pass an explicit uri if that is your setup.")
        out.append(entry)
    return out


def rank(probes: list[TransportProbe]) -> list[TransportProbe]:
    """Fastest measured transport first; unusable ones last.

    Falls back to a documented order when nothing could be measured: direct
    USB beats the RNDIS gadget, which adds a TCP hop over the same cable.
    """
    static_order = {"network": 0, "usb": 1, "usb-gadget": 2, "other": 3}

    def key(p: TransportProbe):
        usable = p.reachable and not p.error
        return (
            0 if usable else 1,
            -(p.throughput_mb_s or 0.0),
            static_order.get(p.kind, 9),
            p.open_seconds if p.open_seconds is not None else 9e9,
        )
    return sorted(probes, key=key)


def choose(probes: list[TransportProbe], prefer: str = "auto") -> dict:
    """Pick a transport, honouring an operator override.

    `prefer` is "auto", a transport kind ("usb", "network", "usb-gadget"), or
    an explicit URI. An override that cannot be satisfied is reported rather
    than silently ignored — quietly falling back to a different transport than
    the one asked for is how an operator ends up debugging the wrong link.
    """
    ranked = rank(probes)
    usable = [p for p in ranked if p.reachable and not p.error]
    if not usable:
        return {"uri": "", "reason": "no transport could be opened",
                "prefer": prefer, "satisfied": False}

    if prefer and prefer != "auto":
        want = [p for p in usable if p.uri == prefer or p.kind == prefer]
        if want:
            p = want[0]
            return {"uri": p.uri, "kind": p.kind, "prefer": prefer,
                    "satisfied": True,
                    "reason": f"operator preference {prefer!r}"}
        return {"uri": ranked and usable[0].uri, "kind": usable[0].kind,
                "prefer": prefer, "satisfied": False,
                "reason": (f"preference {prefer!r} is not reachable; "
                           f"using {usable[0].uri} instead")}

    best = usable[0]
    if best.throughput_mb_s is not None:
        others = [p for p in usable[1:] if p.throughput_mb_s is not None]
        margin = ""
        if others:
            margin = (f", {best.throughput_mb_s / max(others[0].throughput_mb_s, 1e-9):.1f}x "
                      f"the next best ({others[0].uri})")
        reason = f"fastest measured transport at {best.throughput_mb_s:.1f} MB/s{margin}"
    else:
        reason = "no throughput measured; chose by documented transport order"
    return {"uri": best.uri, "kind": best.kind, "prefer": prefer,
            "satisfied": True, "reason": reason}
