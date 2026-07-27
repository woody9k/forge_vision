"""HTTP API smoke tests over the FastAPI app (FR-API-001)."""

import importlib

import pytest
from fastapi.testclient import TestClient


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


def test_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert any(d["device_id"] == "sim-pluto-0" for d in body["devices"])
    assert body["safety"]["armed"] is False


def test_tx_refused_without_arm(client):
    client.post("/api/devices/sim-pluto-0/connect")
    r = client.post("/api/devices/sim-pluto-0/tx",
                    json={"enable": True, "waveform": "fmcw_bench_56M"})
    assert r.status_code == 403


def test_full_range_flow(client):
    client.post("/api/devices/sim-pluto-0/connect")
    r = client.post("/api/safety/arm",
                    json={"operator": "api-test", "acknowledgement": "ack"})
    assert r.status_code == 200
    r = client.post("/api/range/run", json={"device_id": "sim-pluto-0",
                                            "use_background": False})
    assert r.status_code == 200
    body = r.json()
    assert body["range_profile"]["ranges_m"]
    assert "peaks" in body
    exp_id = body["experiment_id"]

    r = client.get(f"/api/experiments/{exp_id}")
    assert r.status_code == 200
    r = client.get(f"/api/experiments/{exp_id}/verify")
    assert r.json()["ok"] is True
    r = client.post(f"/api/experiments/{exp_id}/replay", json={})
    assert r.status_code == 200


def test_scan_flow(client):
    client.post("/api/devices/sim-pluto-0/connect")
    client.post("/api/safety/arm", json={"operator": "t", "acknowledgement": "a"})
    client.post("/api/sim/sim-pluto-0/scene", json={"preset": "scan"})
    r = client.post("/api/scan/start", json={
        "device_id": "sim-pluto-0",
        "plan": {"start_m": 0, "end_m": 0.4, "step_m": 0.2, "chirps": 2}})
    assert r.status_code == 200
    scan_id = r.json()["scan_id"]
    for x in r.json()["positions_m"]:
        pr = client.post(f"/api/scan/{scan_id}/point",
                         json={"x_m": x, "operator_override": True})
        assert pr.status_code == 200
    img = client.get(f"/api/scan/{scan_id}/render").json()
    assert len(img["positions_m"]) == 3
    fin = client.post(f"/api/scan/{scan_id}/finalize")
    assert fin.json()["status"] == "finalized"


def test_emergency_stop_endpoint(client):
    client.post("/api/devices/sim-pluto-0/connect")
    client.post("/api/safety/arm", json={"operator": "t", "acknowledgement": "a"})
    client.post("/api/devices/sim-pluto-0/tx",
                json={"enable": True, "waveform": "fmcw_bench_56M"})
    r = client.post("/api/safety/stop")
    assert r.status_code == 200
    status = client.get("/api/status").json()
    assert status["safety"]["tx_active"] is False


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "FORGE" in r.text


def test_rescan_endpoint(client):
    r = client.post("/api/devices/rescan", json={})
    assert r.status_code == 200
    body = r.json()
    assert "driver" in body and "added" in body


def test_component_api_flow(client):
    r = client.post("/api/components", json={
        "kind": "antenna", "name": "vivaldi", "connector": "SMA",
        "claimed_band": "0.8-6 GHz"})
    assert r.status_code == 200
    cid = r.json()["component_id"]

    s1p = "# MHZ S RI R 50\n700 0.6 0\n900 0.1 0\n1100 0.7 0\n"
    r = client.post(f"/api/components/{cid}/vna",
                    files={"file": ("ant.s1p", s1p, "text/plain")})
    assert r.status_code == 200
    vna = r.json()["vna"]
    assert vna["analysis"]["best_match"]["freq_hz"] == 900e6

    r = client.get("/api/components")
    assert any(c["component_id"] == cid and c["has_vna"] for c in r.json())

    bad = client.post(f"/api/components/{cid}/vna",
                      files={"file": ("bad.s1p", "# MHZ S RI R 50\njunk\n",
                                      "text/plain")})
    assert bad.status_code == 400

    r = client.post(f"/api/components/{cid}/delete")
    assert r.status_code == 200
