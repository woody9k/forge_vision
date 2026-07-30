"""Saved RF chain configurations (FR-RFC-006, FR-RFC-007).

Which antenna and cables a reading passed through decides how that reading
should be interpreted, so the platform has to be able to state it exactly:
persist it across restarts, let an operator name and reuse a setup, and refuse
to claim a named configuration once the patching no longer matches it.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from forge_vision.server.runtime import Runtime


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_VISION_DATA", str(tmp_path / "data"))
    import forge_vision.config as config
    importlib.reload(config)
    import forge_vision.server.runtime as runtime_mod
    importlib.reload(runtime_mod)
    import forge_vision.server.app as app_mod
    importlib.reload(app_mod)
    return TestClient(app_mod.app)


def _components(rt):
    ant = rt.components.create("antenna", "log-periodic")
    cab = rt.components.create("cable", "10ft coax")
    return ant["component_id"], cab["component_id"]


# -- the working chain must outlive the process -----------------------------
def test_declared_chain_survives_a_restart(tmp_path):
    """The original defect: the chain was an in-memory dict, so a restart
    silently reverted to "no antenna declared" while captures carried on
    recording an empty chain."""
    data = str(tmp_path / "data")
    rt = Runtime(data_dir=data)
    ant, cab = _components(rt)
    rt.set_rf_chain(rx_ids=[cab], antenna_rx=ant)

    reopened = Runtime(data_dir=data)          # simulates a service restart
    assert reopened.rf_chain["antenna_rx"] == ant
    assert reopened.rf_chain["rx_ids"] == [cab]
    assert reopened.current_chain()["antenna_rx"] == ant


def test_experiments_record_the_persisted_chain(armed_runtime, tmp_path):
    rt = armed_runtime
    ant, cab = _components(rt)
    rt.set_rf_chain(rx_ids=[cab], antenna_rx=ant)
    out = rt.band_survey("sim-pluto-0", 100e6, 120e6, step_hz=10e6)
    chain = rt.store.load(out["experiment_id"])["hardware"]["rf_chain"]
    assert chain["antenna_rx"] == ant
    assert [c["component_id"] for c in chain["rx_path"]] == [cab]


# -- naming, reuse, activation ----------------------------------------------
def test_save_list_and_activate_a_configuration(runtime):
    rt = runtime
    ant, cab = _components(rt)

    rt.set_rf_chain(rx_ids=[cab], antenna_rx=ant)
    saved = rt.save_chain_config("bench: LPDA + 10ft")
    assert saved["name"] == "bench: LPDA + 10ft"

    # a second, different setup
    rt.set_rf_chain(rx_ids=[], antenna_rx="")
    other = rt.save_chain_config("bare port")

    names = [c["name"] for c in rt.list_chain_configs()]
    assert names == ["bare port", "bench: LPDA + 10ft"]
    assert [c["active"] for c in rt.list_chain_configs()
            if c["config_id"] == other["config_id"]] == [True]

    # going back to the first restores its patching exactly
    rt.activate_chain_config(saved["config_id"])
    assert rt.rf_chain["antenna_rx"] == ant
    assert rt.rf_chain["rx_ids"] == [cab]
    assert rt.current_chain()["config_name"] == "bench: LPDA + 10ft"


def test_a_configuration_needs_a_name(runtime):
    with pytest.raises(ValueError, match="needs a name"):
        runtime.save_chain_config("   ")


# -- honesty about drift -----------------------------------------------------
def test_editing_a_configuration_stops_it_claiming_that_name(runtime):
    """A capture taken after the operator repatched must not read as though it
    came from the pristine saved configuration."""
    rt = runtime
    ant, cab = _components(rt)
    rt.set_rf_chain(rx_ids=[cab], antenna_rx=ant)
    cfg = rt.save_chain_config("as-built")

    clean = rt.current_chain()
    assert clean["config_name"] == "as-built"
    assert clean.get("config_modified") is not True

    rt.set_rf_chain(rx_ids=[], antenna_rx=ant)      # cable pulled out
    drifted = rt.current_chain()
    assert drifted["config_id"] == cfg["config_id"]
    assert drifted["config_modified"] is True
    assert "is not that configuration" in drifted["note"]


def test_deleting_a_configuration_detaches_the_working_chain(runtime):
    rt = runtime
    ant, _ = _components(rt)
    rt.set_rf_chain(antenna_rx=ant)
    cfg = rt.save_chain_config("temporary")
    rt.delete_chain_config(cfg["config_id"])
    chain = rt.current_chain()
    assert chain["config_id"] == ""
    assert chain["config_name"] == ""
    assert chain["antenna_rx"] == ant      # the patching itself is untouched


# -- measurements attach to the configuration they were taken with ----------
def test_survey_is_recorded_against_the_active_configuration(armed_runtime):
    rt = armed_runtime
    ant, cab = _components(rt)
    rt.set_rf_chain(rx_ids=[cab], antenna_rx=ant)
    cfg = rt.save_chain_config("bench baseline")

    first = rt.band_survey("sim-pluto-0", 100e6, 120e6, step_hz=10e6)
    detail = rt.chain_config_measurements(cfg["config_id"])
    assert [m["experiment_id"] for m in detail["measurements"]] == \
        [first["experiment_id"]]
    assert detail["measurements"][0]["kind"] == "survey"
    assert "median_noise_floor_dbfs" in detail["measurements"][0]["summary"]

    # re-measuring appends: two readings a month apart are evidence of drift,
    # and overwriting the older one would discard it
    second = rt.band_survey("sim-pluto-0", 100e6, 120e6, step_hz=10e6)
    detail = rt.chain_config_measurements(cfg["config_id"])
    assert [m["experiment_id"] for m in detail["measurements"]] == \
        [first["experiment_id"], second["experiment_id"]]


def test_measurement_through_an_edited_chain_is_not_attributed(armed_runtime):
    """A reading taken through repatched cables is not a measurement of the
    saved configuration, so it must not be filed under it."""
    rt = armed_runtime
    ant, cab = _components(rt)
    rt.set_rf_chain(rx_ids=[cab], antenna_rx=ant)
    cfg = rt.save_chain_config("as-built")

    rt.set_rf_chain(rx_ids=[], antenna_rx=ant)     # repatched
    rt.band_survey("sim-pluto-0", 100e6, 120e6, step_hz=10e6)

    assert rt.chain_config_measurements(cfg["config_id"])["measurements"] == []


def test_missing_capture_is_reported_not_dropped(armed_runtime):
    rt = armed_runtime
    ant, _ = _components(rt)
    rt.set_rf_chain(antenna_rx=ant)
    cfg = rt.save_chain_config("baseline")
    out = rt.band_survey("sim-pluto-0", 100e6, 120e6, step_hz=10e6)

    rt.chains.record_measurement(cfg["config_id"], "20990101-000000-gone",
                                 "survey", {})
    ms = rt.chain_config_measurements(cfg["config_id"])["measurements"]
    assert [m["experiment_id"] for m in ms] == [out["experiment_id"],
                                                "20990101-000000-gone"]
    assert ms[1]["missing"] is True


# -- API ---------------------------------------------------------------------
def test_chain_config_api_round_trip(client):
    ant = client.post("/api/components",
                      json={"kind": "antenna", "name": "LPDA"}).json()
    cab = client.post("/api/components",
                      json={"kind": "cable", "name": "10ft"}).json()
    client.post("/api/rf_chain", json={
        "rx_ids": [cab["component_id"]], "antenna_rx": ant["component_id"]})

    saved = client.post("/api/chains", json={"name": "bench"}).json()
    assert saved["name"] == "bench"

    listing = client.get("/api/chains").json()
    assert [c["name"] for c in listing] == ["bench"]
    assert listing[0]["active"] is True

    client.post("/api/rf_chain", json={"rx_ids": [], "antenna_rx": ""})
    assert client.get("/api/rf_chain").json()["resolved"]["antenna_rx"] == ""

    client.post(f"/api/chains/{saved['config_id']}/activate")
    resolved = client.get("/api/rf_chain").json()["resolved"]
    assert resolved["antenna_rx"] == ant["component_id"]
    assert resolved["config_name"] == "bench"

    assert client.post(f"/api/chains/{saved['config_id']}/delete").status_code == 200
    assert client.get("/api/chains").json() == []


def test_unnamed_configuration_is_rejected_by_the_contract(client):
    assert client.post("/api/chains", json={"name": ""}).status_code == 422
    assert client.post("/api/chains", json={"nmae": "typo"}).status_code == 422


def test_config_id_cannot_escape_the_directory(runtime):
    with pytest.raises(ValueError, match="invalid configuration id"):
        runtime.chains.load("../../etc/passwd")
    with pytest.raises(ValueError, match="invalid configuration id"):
        runtime.chains.load("_working")
