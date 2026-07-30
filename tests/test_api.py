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


def arm(client, operator="api-test"):
    """Complete the required pre-transmit checks, then arm (FR-SAF-009)."""
    for item in client.get("/api/safety/checklist").json()["items"]:
        if item["required"]:
            client.post("/api/safety/checklist",
                        json={"id": item["id"], "confirmed": True})
    return client.post("/api/safety/arm",
                       json={"operator": operator, "acknowledgement": "ack"})


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
    r = arm(client)
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
    arm(client, "t")
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
    arm(client, "t")
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


def test_checklist_api_gates_arming(client):
    """Arming over HTTP is refused until the required checks are confirmed."""
    client.post("/api/devices/sim-pluto-0/connect")
    r = client.post("/api/safety/arm",
                    json={"operator": "t", "acknowledgement": "a"})
    assert r.status_code == 403
    assert "checklist" in r.json()["detail"]
    assert arm(client, "t").status_code == 200
    assert client.get("/api/status").json()["safety"]["checklist"]["complete"]


def test_survey_endpoint_is_receive_only(client):
    client.post("/api/devices/sim-pluto-0/connect")
    r = client.post("/api/survey", json={"device_id": "sim-pluto-0",
                                         "start_hz": 902e6, "stop_hz": 910e6,
                                         "step_hz": 2e6, "samples": 16384})
    assert r.status_code == 200
    body = r.json()
    assert len(body["points"]) == 5
    assert body["quietest"]["peak_dbfs"] <= body["busiest"]["peak_dbfs"]
    # nothing transmitted, and the interlock was never armed
    assert client.get("/api/status").json()["safety"]["armed"] is False


def test_site_api_flow(client):
    """Register two crossing scans to a site and fuse them over HTTP."""
    client.post("/api/devices/sim-pluto-0/connect")
    arm(client, "t")
    client.post("/api/sim/sim-pluto-0/scene", json={
        "targets": [{"kind": "point", "x_m": 1.0, "depth_m": 0.8,
                     "amplitude": 0.35}],
        "medium": "soil_dry", "leakage_amplitude": 1e-4})

    scans = []
    for _ in range(2):
        r = client.post("/api/scan/start", json={
            "device_id": "sim-pluto-0",
            "plan": {"start_m": 0, "end_m": 2.0, "step_m": 0.25,
                     "medium": "soil_dry", "chirps": 2, "max_range_m": 8.0}})
        sid = r.json()["scan_id"]
        for x in r.json()["positions_m"]:
            client.post(f"/api/scan/{sid}/point",
                        json={"x_m": x, "operator_override": True})
        client.post(f"/api/scan/{sid}/finalize")
        scans.append(sid)

    site = client.post("/api/sites", json={"name": "api site"}).json()
    sid = site["site_id"]
    assert client.post(f"/api/sites/{sid}/register", json={
        "experiment_id": scans[0], "origin_x_m": 0, "origin_y_m": 1.0,
        "heading_deg": 0, "label": "EW"}).status_code == 200
    assert client.post(f"/api/sites/{sid}/register", json={
        "experiment_id": scans[1], "origin_x_m": 1.0, "origin_y_m": 0,
        "heading_deg": 90, "label": "NS"}).status_code == 200

    scene = client.get(f"/api/sites/{sid}/scene?tolerance_m=0.8").json()
    assert scene["errors"] == []
    assert len(scene["scans"]) == 2
    assert scene["findings"]
    assert all("depth_interval_m" in f for f in scene["findings"])

    sliced = client.get(f"/api/sites/{sid}/scene?slice_depth_m=0.8").json()
    assert sliced["depth_slice"]["samples"]

    rep = client.get(f"/api/sites/{sid}/report").json()
    assert "# Site report" in rep["markdown"]
    assert "Limitations" in rep["markdown"] or "limitations" in rep["markdown"]


def test_site_scene_404_on_unknown_site(client):
    assert client.get("/api/sites/nope/scene").status_code == 404


def test_sage_api_milestone_e(client):
    """Milestone E over HTTP: ask why an anomaly was highlighted and get an
    evidence-linked answer."""
    client.post("/api/devices/sim-pluto-0/connect")
    arm(client, "t")
    client.post("/api/sim/sim-pluto-0/scene", json={
        "targets": [{"kind": "point", "x_m": 1.0, "depth_m": 0.8,
                     "amplitude": 0.35}],
        "medium": "soil_dry", "leakage_amplitude": 1e-4})
    scans = []
    for _ in range(2):
        r = client.post("/api/scan/start", json={
            "device_id": "sim-pluto-0",
            "plan": {"start_m": 0, "end_m": 2.0, "step_m": 0.25,
                     "medium": "soil_dry", "chirps": 2, "max_range_m": 8.0}})
        sid = r.json()["scan_id"]
        for x in r.json()["positions_m"]:
            client.post(f"/api/scan/{sid}/point",
                        json={"x_m": x, "operator_override": True})
        client.post(f"/api/scan/{sid}/finalize")
        scans.append(sid)

    site = client.post("/api/sites", json={"name": "sage site"}).json()
    sid = site["site_id"]
    client.post(f"/api/sites/{sid}/register", json={
        "experiment_id": scans[0], "origin_x_m": 0, "origin_y_m": 1.0,
        "heading_deg": 0})
    client.post(f"/api/sites/{sid}/register", json={
        "experiment_id": scans[1], "origin_x_m": 1.0, "origin_y_m": 0,
        "heading_deg": 90})

    r = client.get(f"/api/sage/site/{sid}/finding/0")
    assert r.status_code == 200
    body = r.json()
    assert body["evidence_count"] >= 2
    for f in body["facts"]:
        assert f["kind"] in ("observation", "calculation", "inference",
                             "hypothesis", "unknown")
        if f["kind"] != "unknown":
            assert f["evidence"]

    r = client.post("/api/sage/ask", json={
        "question": "which findings are confirmed by more than one scan",
        "site_id": sid})
    assert r.json()["understood"] is True

    r = client.post("/api/sage/ask", json={
        "question": "is there treasure down there", "site_id": sid})
    assert r.json()["understood"] is False
    assert r.json()["facts"] == []

    assert client.get(f"/api/sage/site/{sid}/recommend").status_code == 200
    assert client.get(f"/api/sage/experiment/{scans[0]}").status_code == 200
    assert client.get(f"/api/sage/site/{sid}/finding/99").status_code == 404


def test_job_api_lifecycle(client):
    """FR-API-003 over HTTP: submit, monitor, inspect, cancel, retry."""
    import time
    client.post("/api/devices/sim-pluto-0/connect")
    r = client.post("/api/jobs", json={"kind": "survey", "params": {
        "device_id": "sim-pluto-0", "start_hz": 902e6, "stop_hz": 908e6,
        "step_hz": 2e6, "samples": 8192}})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    deadline = time.time() + 60
    state = "queued"
    while time.time() < deadline:
        state = client.get(f"/api/jobs/{job_id}").json()["state"]
        if state in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert state == "succeeded"

    full = client.get(f"/api/jobs/{job_id}?include_result=true").json()
    assert full["result"]["points"]

    listing = client.get("/api/jobs").json()
    assert listing["summary"].get("succeeded") == 1
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.post("/api/jobs", json={"kind": "bogus"}).status_code == 404


def test_rx_protection_and_chain_api(client):
    client.post("/api/devices/sim-pluto-0/connect")
    r = client.post("/api/safety/path_attenuation", json={"attenuation_db": 0})
    assert r.json()["path_attenuation_db"] == 0
    check = client.get("/api/safety/rx_protection?device_id=sim-pluto-0").json()
    assert check["severity"] in ("warn", "critical")
    assert check["warnings"]

    client.post("/api/safety/path_attenuation", json={"attenuation_db": 40})
    client.post("/api/devices/sim-pluto-0/configure",
                json={"tx_gain_db": -30, "rx_gain_db": 20})
    assert client.get("/api/safety/rx_protection?device_id=sim-pluto-0"
                      ).json()["safe"] is True

    cable = client.post("/api/components", json={
        "kind": "cable", "name": "test cable", "nominal_loss_db": 1.0,
        "nominal_delay_ns": 5.0}).json()
    r = client.post("/api/rf_chain", json={"tx_ids": [cable["component_id"]]})
    assert r.json()["total_loss_db"] == 1.0
    assert client.get("/api/rf_chain").json()["resolved"]["tx_path"]
