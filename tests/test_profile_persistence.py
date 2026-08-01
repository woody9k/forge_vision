"""The frequency profile survives a restart (FR-SAF-007).

It did not. `SafetyLimits()` was constructed with its defaults in
`Runtime.__init__` and nothing persisted `active_profile`, so every start
silently returned to `bench_cabled` — 70 MHz to 6 GHz, the closed-circuit
profile. On 2026-07-31 the profile was deliberately narrowed to
`ism_conservative` because antennas had gone on the bench, and a later
service restart widened it again with nobody told.

A correction that is lost degrades a measurement. A gate that is lost is a
gate, so these tests exist to keep it shut.

Path attenuation is deliberately *not* persisted, and there is a test for
that too: it asserts a fact about physical cabling, and restoring yesterday's
wiring onto today's bench would be the software making a claim only a person
standing at the bench can make.
"""

from __future__ import annotations

import json
import os

import pytest

from forge_vision.server.runtime import Runtime

NARROW = "ism_conservative"
DEFAULT = "bench_cabled"


def restart(tmp_path) -> Runtime:
    """A new Runtime over the same data directory — i.e. a service restart."""
    return Runtime(data_dir=str(tmp_path / "data"))


# -- the regression ----------------------------------------------------------

def test_profile_survives_a_restart(tmp_path):
    rt = restart(tmp_path)
    assert rt.safety.limits.active_profile == DEFAULT
    rt.set_frequency_profile(NARROW)

    again = restart(tmp_path)
    assert again.safety.limits.active_profile == NARROW, (
        "a profile narrowed for antenna work must not widen on restart")


def test_restart_reports_that_it_restored_rather_than_defaulted(tmp_path):
    rt = restart(tmp_path)
    rt.set_frequency_profile(NARROW)
    again = restart(tmp_path)
    assert again.profile_restore["source"] == "restored"
    assert again.profile_restore["profile"] == NARROW


def test_a_fresh_bench_reports_the_default_as_a_default(tmp_path):
    """First ever start: the default is a default, not a restored choice."""
    rt = restart(tmp_path)
    assert rt.profile_restore["source"] == "default"
    assert rt.profile_restore["profile"] == DEFAULT


def test_status_exposes_where_the_profile_came_from(tmp_path):
    rt = restart(tmp_path)
    rt.set_frequency_profile(NARROW)
    again = restart(tmp_path)
    src = again.status()["safety"]["profile_source"]
    assert src["source"] == "restored" and src["profile"] == NARROW


def test_the_narrowed_profile_actually_gates_after_a_restart(tmp_path):
    """Restoring the name is worthless if the bands do not come with it."""
    rt = restart(tmp_path)
    rt.set_frequency_profile(NARROW)
    again = restart(tmp_path)
    bands = again.safety.limits.allowed_bands()
    assert [902e6, 928e6] in [list(b) for b in bands]
    # 70 MHz-6 GHz is bench_cabled's span; it must not still be in force
    assert not any(lo <= 100e6 and hi >= 5e9 for lo, hi in bands)


# -- failure paths must not pretend ------------------------------------------

def test_unreadable_state_falls_back_and_says_so(tmp_path):
    rt = restart(tmp_path)
    rt.set_frequency_profile(NARROW)
    with open(rt._safety_state_path, "w", encoding="utf-8") as f:
        f.write("{ this is not json")

    again = restart(tmp_path)
    assert again.safety.limits.active_profile == DEFAULT
    assert again.profile_restore["source"] == "default"
    assert "could not be read" in again.profile_restore["note"]
    assert any(e["event"] == "frequency_profile_restore_failed"
               for e in again.safety.audit_tail(20))


def test_a_profile_that_no_longer_exists_falls_back_and_says_so(tmp_path):
    rt = restart(tmp_path)
    with open(rt._safety_state_path, "w", encoding="utf-8") as f:
        json.dump({"active_profile": "profile_deleted_last_year"}, f)

    again = restart(tmp_path)
    assert again.safety.limits.active_profile == DEFAULT
    assert "no longer exists" in again.profile_restore["note"]


def test_a_failed_save_is_reported_not_swallowed(tmp_path, monkeypatch):
    """The change applies in memory but will not survive; say so.

    Patch only the final rename — patching `open` globally would take the
    audit log down with it and test the wrong failure.
    """
    rt = restart(tmp_path)
    monkeypatch.setattr(os, "replace",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            OSError("read-only file system")))
    status = rt.set_frequency_profile(NARROW)
    assert rt.safety.limits.active_profile == NARROW      # applied now
    assert "could not be saved" in status["profile_not_saved"]
    assert any(e["event"] == "frequency_profile_save_failed"
               for e in rt.safety.audit_tail(20))


def test_a_failed_save_leaves_no_half_written_file(tmp_path, monkeypatch):
    rt = restart(tmp_path)
    monkeypatch.setattr(os, "replace",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    rt.set_frequency_profile(NARROW)
    monkeypatch.undo()
    d = os.path.dirname(rt._safety_state_path)
    assert [p for p in os.listdir(d) if p.endswith(".tmp")] == []


def test_an_unknown_profile_is_still_refused(tmp_path):
    rt = restart(tmp_path)
    with pytest.raises(KeyError):
        rt.set_frequency_profile("not_a_profile")
    assert rt.safety.limits.active_profile == DEFAULT


# -- what must NOT persist ---------------------------------------------------

def test_path_attenuation_does_not_survive_a_restart(tmp_path):
    """It is a claim about cabling, and cabling changes while the service is down."""
    rt = restart(tmp_path)
    rt.declare_path_attenuation(40.0)
    assert rt.safety.path_attenuation_db == 40.0

    again = restart(tmp_path)
    assert again.safety.path_attenuation_db == 0.0, (
        "restoring path attenuation would re-assert yesterday's wiring")


def test_arming_does_not_survive_a_restart(tmp_path):
    """An armed interlock is a live operator decision, not stored policy."""
    rt = restart(tmp_path)
    for item in rt.safety.checklist:
        if item["required"]:
            rt.safety.confirm_checklist_item(item["id"], True)
    rt.safety.arm("tester", "bench, cabled, attenuated — authorized")
    assert rt.safety.state.armed is True

    again = restart(tmp_path)
    assert again.safety.state.armed is False


def test_the_saved_file_holds_only_policy(tmp_path):
    """Guard against physical assertions creeping into the persisted state."""
    rt = restart(tmp_path)
    rt.declare_path_attenuation(30.0)
    rt.set_frequency_profile(NARROW)
    with open(rt._safety_state_path, encoding="utf-8") as f:
        saved = json.load(f)
    assert set(saved) == {"active_profile", "saved_at"}
    assert "path_attenuation" not in json.dumps(saved)


def test_saving_is_atomic(tmp_path):
    """A torn write would leave the bench unable to read its own policy."""
    rt = restart(tmp_path)
    rt.set_frequency_profile(NARROW)
    leftovers = [p for p in os.listdir(os.path.dirname(rt._safety_state_path))
                 if p.endswith(".tmp")]
    assert leftovers == []
    with open(rt._safety_state_path, encoding="utf-8") as f:
        json.load(f)          # parses, so the file is whole
