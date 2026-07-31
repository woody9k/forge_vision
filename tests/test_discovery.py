"""Transport discovery (FR-DEV-002): find the board, measure, choose one.

A Pluto+ answers on several transports at once and they are not equivalent —
Ethernet measured 52.6 MB/s against USB's 28.3 on the bench. They are also the
same radio, so registering two produces entries whose cached configuration
drifts apart silently. These tests pin the three behaviours that matter:
transports get ranked by what was measured rather than assumed, an operator
override is honoured or reported unsatisfiable, and a board reached two ways
is still one board.
"""

import pytest

from forge_vision.devices import discovery
from forge_vision.devices.discovery import TransportProbe


def _p(uri, throughput=None, error="", identity=None, reachable=True, open_s=0.05):
    return TransportProbe(uri=uri, reachable=reachable, error=error,
                          identity=identity if identity is not None else {},
                          open_seconds=open_s, throughput_mb_s=throughput)


# -- candidates --------------------------------------------------------------
def test_default_candidates_cover_the_three_ways_in():
    uris = discovery.candidate_uris()
    assert "usb:" in uris
    assert "ip:192.168.2.1" in uris
    # mDNS finds the board without hardcoding a site-specific address
    assert "ip:pluto.local" in uris


def test_env_overrides_the_candidate_list(monkeypatch):
    monkeypatch.setenv("FORGE_VISION_PLUTO_URIS",
                       "ip:192.168.99.222, usb: ,ip:pluto.boblab.net")
    assert discovery.candidate_uris() == [
        "ip:192.168.99.222", "usb:", "ip:pluto.boblab.net"]


def test_explicit_candidates_come_first_and_are_not_duplicated(monkeypatch):
    monkeypatch.delenv("FORGE_VISION_PLUTO_URIS", raising=False)
    uris = discovery.candidate_uris(("ip:10.0.0.5", "usb:"))
    assert uris[0] == "ip:10.0.0.5"
    assert uris.count("usb:") == 1


# -- kinds -------------------------------------------------------------------
@pytest.mark.parametrize("uri,kind", [
    ("usb:", "usb"),
    ("usb:1.19.5", "usb"),
    ("ip:192.168.2.1", "usb-gadget"),
    ("ip:192.168.99.222", "network"),
    ("ip:pluto.local", "network"),
])
def test_transport_kind(uri, kind):
    assert _p(uri).kind == kind


# -- ranking -----------------------------------------------------------------
def test_measured_throughput_beats_assumptions():
    """The whole point is to measure. A slow network link must lose to USB."""
    ranked = discovery.rank([
        _p("ip:192.168.99.222", throughput=6.0),     # e.g. a 100M switch
        _p("usb:", throughput=28.3),
    ])
    assert ranked[0].uri == "usb:"


def test_ethernet_wins_when_it_is_actually_faster():
    ranked = discovery.rank([
        _p("usb:", throughput=28.3),
        _p("ip:192.168.99.222", throughput=52.6),
        _p("ip:192.168.2.1", throughput=21.3),
    ])
    assert [p.uri for p in ranked] == [
        "ip:192.168.99.222", "usb:", "ip:192.168.2.1"]


def test_reachable_but_unusable_sorts_last():
    """A wedged DMA answers attribute reads and times out on buffers. That is
    not a usable transport and must not be chosen."""
    ranked = discovery.rank([
        _p("usb:", error="opened but could not read samples: timed out"),
        _p("ip:192.168.2.1", throughput=21.3),
    ])
    assert ranked[0].uri == "ip:192.168.2.1"


def test_unmeasured_falls_back_to_documented_order():
    ranked = discovery.rank([_p("ip:192.168.2.1"), _p("usb:"),
                             _p("ip:192.168.99.222")])
    assert [p.kind for p in ranked] == ["network", "usb", "usb-gadget"]


# -- choosing and overriding -------------------------------------------------
def test_auto_picks_the_fastest_and_says_why():
    pick = discovery.choose([_p("usb:", throughput=28.3),
                             _p("ip:192.168.99.222", throughput=52.6)])
    assert pick["uri"] == "ip:192.168.99.222"
    assert pick["satisfied"] is True
    assert "52.6 MB/s" in pick["reason"]
    assert "1.9x" in pick["reason"]


def test_operator_can_prefer_usb_over_a_faster_link():
    """Fastest is not always wanted — USB needs no network at all, which
    matters when the network is what you are debugging."""
    pick = discovery.choose([_p("usb:", throughput=28.3),
                             _p("ip:192.168.99.222", throughput=52.6)],
                            prefer="usb")
    assert pick["uri"] == "usb:"
    assert pick["satisfied"] is True


def test_exact_uri_preference_is_honoured():
    pick = discovery.choose([_p("usb:", throughput=28.3),
                             _p("ip:192.168.99.222", throughput=52.6)],
                            prefer="ip:192.168.99.222")
    assert pick["uri"] == "ip:192.168.99.222"


def test_unsatisfiable_preference_is_reported_not_silently_ignored():
    """Quietly using a different transport than the one asked for is how an
    operator ends up debugging the wrong link."""
    pick = discovery.choose([_p("usb:", throughput=28.3)], prefer="network")
    assert pick["satisfied"] is False
    assert pick["uri"] == "usb:"
    assert "not reachable" in pick["reason"]


def test_no_usable_transport_is_stated_plainly():
    pick = discovery.choose([_p("usb:", reachable=False, error="no device")])
    assert pick["uri"] == ""
    assert pick["satisfied"] is False


# -- grouping ----------------------------------------------------------------
SERIAL_A = {"hw_serial": "abc123", "hw_model": "PlutoSDR Rev.C"}
SERIAL_B = {"hw_serial": "def456", "hw_model": "PlutoSDR Rev.C"}
NO_SERIAL = {"hw_model": "PlutoSDR Rev.C", "fw_version": "v0.33",
             "local,kernel": "5.4.0"}


def test_same_serial_is_one_board():
    boards = discovery.group_boards([
        _p("usb:", throughput=28.3, identity=SERIAL_A),
        _p("ip:192.168.99.222", throughput=52.6, identity=SERIAL_A)])
    assert len(boards) == 1
    assert boards[0]["identified_by"] == "serial"
    assert boards[0]["best_uri"] == "ip:192.168.99.222"
    assert len(boards[0]["transports"]) == 2


def test_different_serials_are_different_boards():
    boards = discovery.group_boards([
        _p("usb:", identity=SERIAL_A),
        _p("ip:192.168.99.222", identity=SERIAL_B)])
    assert len(boards) == 2


def test_grouping_without_a_serial_reports_its_uncertainty():
    """This Pluto reports an empty hw_serial, so matching attributes cannot
    distinguish it from an identical sibling. Say so rather than assert."""
    boards = discovery.group_boards([
        _p("usb:", throughput=28.3, identity=NO_SERIAL),
        _p("ip:192.168.99.222", throughput=52.6, identity=NO_SERIAL)])
    assert len(boards) == 1
    assert boards[0]["identified_by"] == "attributes"
    assert "no serial number" in boards[0]["note"]
    assert "pass an explicit uri" in boards[0]["note"]


def test_unreachable_probes_form_no_board():
    boards = discovery.group_boards([
        _p("usb:", reachable=False, error="no device"),
        _p("ip:pluto.local", reachable=False, error="name not resolved")])
    assert boards == []
