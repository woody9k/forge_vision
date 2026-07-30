"""Antenna Lab tests (FR-RFC-001..004): touchstone parsing, VSWR analysis,
component inventory, VNA import API."""

import math

import pytest

from forge_vision.rfcomponents.touchstone import (TouchstoneError, analyze_s11,
                                                  parse_touchstone)

S1P_RI = """! NanoVNA-style export
# MHZ S RI R 50
700  0.6   0.0
800  0.333333 0.0
900  0.1   0.0
1000 0.333333 0.0
1100 0.7   0.0
"""

S1P_DB = """# MHZ S DB R 50
700  -4.436  0
900  -20.0   0
1100 -3.098  0
"""

S1P_MA = """# GHZ S MA R 50
0.7 0.6 45
0.9 0.1 90
1.1 0.7 180
"""

S2P_RI = """# MHZ S RI R 50
100 0.5 0.0  0.1 0.0  0.1 0.0  0.5 0.0
200 0.2 0.0  0.5 0.0  0.5 0.0  0.2 0.0
"""


def test_parse_ri_format():
    d = parse_touchstone(S1P_RI)
    assert d["ports"] == 1
    assert d["freqs_hz"] == [700e6, 800e6, 900e6, 1000e6, 1100e6]
    assert abs(d["s11"][2]) == pytest.approx(0.1)


def test_parse_db_and_ma_formats():
    db = parse_touchstone(S1P_DB)
    assert abs(db["s11"][1]) == pytest.approx(0.1, rel=1e-3)
    ma = parse_touchstone(S1P_MA)
    assert ma["freqs_hz"][0] == pytest.approx(0.7e9)
    assert abs(ma["s11"][0]) == pytest.approx(0.6)
    assert math.degrees(math.atan2(ma["s11"][0].imag, ma["s11"][0].real)) == \
        pytest.approx(45)


def test_parse_s2p():
    d = parse_touchstone(S2P_RI)
    assert d["ports"] == 2
    assert abs(d["s21"][1]) == pytest.approx(0.5)


def test_parse_rejects_garbage():
    with pytest.raises(TouchstoneError):
        parse_touchstone("# MHZ S RI R 50\nnot numbers at all\n")
    with pytest.raises(TouchstoneError):
        parse_touchstone("# MHZ Z RI R 50\n100 0.1 0\n")   # Z-params
    with pytest.raises(TouchstoneError):
        parse_touchstone("! only comments\n")


def test_vswr_math_and_bands():
    """|Gamma|=1/3 -> VSWR exactly 2.0 (the recommended threshold)."""
    d = parse_touchstone(S1P_RI)
    a = analyze_s11(d["freqs_hz"], d["s11"])
    # 800-1000 MHz region: gamma <= 1/3 -> vswr <= 2.0 -> recommended
    assert a["vswr"][1] == pytest.approx(2.0, abs=1e-3)
    assert a["best_match"]["freq_hz"] == 900e6
    ratings = {(b["start_hz"], b["stop_hz"]): b["rating"] for b in a["bands"]}
    assert ratings[(800e6, 1000e6)] == "recommended"   # |G|<=1/3 -> VSWR<=2
    assert ratings[(700e6, 700e6)] == "unsuitable"     # |G|=0.6 -> VSWR 4.0
    assert ratings[(1100e6, 1100e6)] == "unsuitable"   # |G|=0.7 -> VSWR 5.7


def test_component_store_roundtrip(runtime):
    comp = runtime.components.create(kind="antenna", name="log-periodic",
                                     connector="SMA", claimed_band="0.7-6 GHz")
    cid = comp["component_id"]
    runtime.components.import_vna(cid, S1P_RI, filename="lp.s1p")
    loaded = runtime.components.load(cid)
    assert loaded["vna"]["filename"] == "lp.s1p"
    assert len(loaded["vna"]["vswr"]) == 5
    listing = runtime.components.list(kind="antenna")
    assert listing[0]["has_vna"] is True
    assert listing[0]["best_match"]["freq_hz"] == 900e6
    runtime.components.update(cid, {"notes": "measured on NanoVNA-F V2"})
    assert runtime.components.load(cid)["notes"] == "measured on NanoVNA-F V2"
    runtime.components.delete(cid)
    assert runtime.components.list() == []


def test_component_kind_validated(runtime):
    with pytest.raises(ValueError):
        runtime.components.create(kind="flux_capacitor", name="nope")


def test_rescan_graceful_without_driver(runtime):
    """Without libiio installed, rescan reports the missing driver instead
    of crashing (the UI shows the install hint)."""
    result = runtime.rescan_hardware()
    assert "driver" in result
    if not result["driver"]["available"]:
        assert "libiio" in result["driver"]["detail"]
        assert result["added"] == []


# -- insertion loss from a two-port sweep (FR-RFC-004) -----------------------
S2P_CABLE = """! a lossy cable: S21 falls with frequency, as coax does
# MHZ S RI R 50
100  0.02 0.0  0.891 0.0  0.891 0.0  0.02 0.0
500  0.02 0.0  0.794 0.0  0.794 0.0  0.02 0.0
1000 0.02 0.0  0.708 0.0  0.708 0.0  0.02 0.0
"""


def test_s21_becomes_positive_insertion_loss():
    from forge_vision.rfcomponents.touchstone import analyze_s21
    d = parse_touchstone(S2P_CABLE)
    a = analyze_s21(d["freqs_hz"], d["s21"])
    # 0.891 linear ~= 1 dB down, 0.708 ~= 3 dB down; reported positive-as-loss
    assert a["at_lowest"]["loss_db"] == pytest.approx(1.0, abs=0.05)
    assert a["at_highest"]["loss_db"] == pytest.approx(3.0, abs=0.05)
    assert a["at_lowest"]["freq_hz"] == 100e6
    assert a["max_loss_db"] > a["min_loss_db"]


def test_adopting_measured_loss_records_the_frequency(tmp_path):
    """A bare loss figure with no frequency attached is not checkable, so the
    frequency it came from is written into the notes."""
    from forge_vision.rfcomponents.store import ComponentStore
    store = ComponentStore(str(tmp_path / "components"))
    c = store.create("cable", "10ft coax")
    store.import_vna(c["component_id"], S2P_CABLE, "cable.s2p")

    updated = store.adopt_measured_loss(c["component_id"], freq_hz=1000e6)
    assert updated["nominal_loss_db"] == pytest.approx(3.0, abs=0.05)
    assert "1000.0 MHz" in updated["notes"]
    assert "cable.s2p" in updated["notes"]


def test_adopting_loss_without_a_two_port_sweep_is_refused(tmp_path):
    from forge_vision.rfcomponents.store import ComponentStore
    store = ComponentStore(str(tmp_path / "components"))
    c = store.create("antenna", "whip")
    store.import_vna(c["component_id"], S1P_RI, "ant.s1p")
    with pytest.raises(ValueError, match="no two-port"):
        store.adopt_measured_loss(c["component_id"])


# -- chain usable band (FR-RFC-004) ------------------------------------------
def _antenna(store, name, s1p):
    c = store.create("antenna", name)
    store.import_vna(c["component_id"], s1p, f"{name}.s1p")
    return c["component_id"]


NARROW = """# MHZ S RI R 50
700  0.9 0.0
800  0.05 0.0
900  0.05 0.0
1000 0.9 0.0
"""
WIDE = """# MHZ S RI R 50
700  0.05 0.0
800  0.05 0.0
900  0.05 0.0
1000 0.9 0.0
"""


def test_chain_band_is_the_intersection_of_its_parts(tmp_path):
    """A series path is only usable where every measured part is."""
    from forge_vision.rfcomponents.store import ComponentStore
    store = ComponentStore(str(tmp_path / "components"))
    a = _antenna(store, "narrow", NARROW)     # recommended 800-900
    b = _antenna(store, "wide", WIDE)         # recommended 700-900
    band = store.describe_chain([], [b], antenna_rx=a)["band"]
    assert band["usable_bands"] == [{"start_hz": 800e6, "stop_hz": 900e6}]
    assert sorted(band["measured_components"]) == ["narrow", "wide"]


def test_unmeasured_parts_are_named_not_assumed_transparent(tmp_path):
    from forge_vision.rfcomponents.store import ComponentStore
    store = ComponentStore(str(tmp_path / "components"))
    a = _antenna(store, "narrow", NARROW)
    cable = store.create("cable", "unknown cable")["component_id"]
    band = store.describe_chain([], [cable], antenna_rx=a)["band"]
    assert band["unverified_components"] == ["unknown cable"]
    assert "could narrow it further" in band["note"]


def test_chain_with_nothing_measured_says_so(tmp_path):
    from forge_vision.rfcomponents.store import ComponentStore
    store = ComponentStore(str(tmp_path / "components"))
    cable = store.create("cable", "unknown cable")["component_id"]
    band = store.describe_chain([], [cable])["band"]
    assert band["usable_bands"] == []
    assert "usable band is unknown" in band["note"]
