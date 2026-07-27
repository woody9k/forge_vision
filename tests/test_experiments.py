"""Experiment package tests (§7, FR-DAT-*, AC-001, AC-006, REF-08)."""

import os

import numpy as np
import pytest


def test_package_is_complete_and_reproducible(armed_runtime):
    """AC-001: a finalized experiment documents hardware, config, calibration,
    raw source, and processing history."""
    result = armed_runtime.range_run("sim-pluto-0", name="doc test",
                                     medium="air", use_background=False)
    m = armed_runtime.store.load(result["experiment_id"])
    assert m["identity"]["status"] == "finalized"
    assert m["hardware"]["device_id"] == "sim-pluto-0"
    assert m["rf_config"]["center_frequency_hz"] > 0
    assert m["calibration"]["propagation_model"]["name"] == "air"
    assert len(m["segments"]) == 1
    derived = armed_runtime.store.load_derived(result["experiment_id"],
                                               "range_profile")
    assert derived["processing"]["stages"], "processing history missing"
    assert derived["sources"] == ["segment_0000"]
    assert derived["processing"]["fingerprint"]


def test_raw_immutable_after_finalize(armed_runtime):
    """FR-DAT-001: no segments can be added after finalization."""
    result = armed_runtime.range_run("sim-pluto-0", use_background=False)
    exp_id = result["experiment_id"]
    seg = armed_runtime.device("sim-pluto-0")
    with pytest.raises(PermissionError):
        armed_runtime.store.add_segment(exp_id, _fake_segment())


def _fake_segment():
    from forge_vision.devices.base import CaptureSegment
    return CaptureSegment(
        iq=np.zeros(64, dtype=np.complex64), timestamp=0.0, config={},
        waveform=None, device_id="x", sample_rate_hz=1e6,
        center_frequency_hz=1e9)


def test_integrity_check_detects_corruption(armed_runtime):
    """FR-DAT-004: checksum verification catches tampering."""
    result = armed_runtime.range_run("sim-pluto-0", use_background=False)
    exp_id = result["experiment_id"]
    assert armed_runtime.store.verify(exp_id)["ok"] is True
    npy = os.path.join(armed_runtime.store.root, exp_id, "raw", "segment_0000.npy")
    with open(npy, "r+b") as f:
        f.seek(200)
        f.write(b"CORRUPT")
    check = armed_runtime.store.verify(exp_id)
    assert check["ok"] is False
    assert "raw/segment_0000.npy" in check["corrupt"]


def test_export_import_roundtrip(armed_runtime, tmp_path):
    """FR-DAT-007: a package survives export and re-import intact."""
    result = armed_runtime.range_run("sim-pluto-0", use_background=False)
    exp_id = result["experiment_id"]
    dest = str(tmp_path / "pkg.zip")
    armed_runtime.store.export(exp_id, dest)

    from forge_vision.experiments.store import ExperimentStore
    other = ExperimentStore(str(tmp_path / "other-root"))
    manifest = other.import_package(dest)
    assert manifest["identity"]["experiment_id"] == exp_id
    assert other.verify(exp_id)["ok"] is True
    iq, meta = other.load_segment(exp_id, "segment_0000")
    assert len(iq) == meta["num_samples"]


def test_replay_without_hardware(armed_runtime, tmp_path):
    """AC-006 / FR-ACQ-008: reprocess a recorded experiment with no device."""
    armed_runtime.set_sim_scene(
        "sim-pluto-0", targets=[{"kind": "plate", "range_m": 8.0, "amplitude": 0.08}])
    original = armed_runtime.range_run("sim-pluto-0", use_background=False)
    exp_id = original["experiment_id"]

    # replay on a fresh runtime whose simulated device is never connected
    from forge_vision.server.runtime import Runtime
    cold = Runtime(data_dir=armed_runtime.data_dir)
    assert cold.device("sim-pluto-0").connected is False
    replayed = cold.replay(exp_id)
    orig_peak = original["peaks"][0]["range_m"]
    replay_peak = min(replayed["peaks"], key=lambda p: abs(p["range_m"] - orig_peak))
    assert abs(replay_peak["range_m"] - orig_peak) < 1e-6, \
        "replay must reproduce the original result from raw data"
    # replay result is stored as a new derived product with lineage
    m = cold.store.load(exp_id)
    assert any(d["name"].startswith("replay_") for d in m["derived"])


def test_replay_with_new_parameters(armed_runtime):
    """§3.1 'Algorithm replay': new detector settings, same raw data."""
    armed_runtime.set_sim_scene(
        "sim-pluto-0", targets=[{"kind": "plate", "range_m": 8.0, "amplitude": 0.08}])
    result = armed_runtime.range_run("sim-pluto-0", use_background=False)
    strict = armed_runtime.replay(
        result["experiment_id"],
        pipeline_overrides={"detect_peaks": {"threshold_db": 40.0}})
    loose = armed_runtime.replay(
        result["experiment_id"],
        pipeline_overrides={"detect_peaks": {"threshold_db": 6.0}})
    assert len(strict["peaks"]) <= len(loose["peaks"])
    assert strict["processing"]["fingerprint"] != loose["processing"]["fingerprint"]


def test_annotations_append_only(armed_runtime):
    """FR-INT-006: human review never erases automated results."""
    result = armed_runtime.range_run("sim-pluto-0", use_background=False)
    exp_id = result["experiment_id"]
    armed_runtime.store.annotate(exp_id, {"type": "note", "text": "first"})
    items = armed_runtime.store.annotate(exp_id, {"type": "reject",
                                                  "text": "false positive"})
    assert len(items) == 2
    assert items[0]["text"] == "first"


def test_search(armed_runtime):
    """FR-DAT-008: search by text and kind."""
    armed_runtime.range_run("sim-pluto-0", name="alpha bench", tags=["bench"],
                            use_background=False)
    armed_runtime.record_capture("sim-pluto-0", num_samples=4096,
                                 name="beta raw")
    assert any("alpha" in e["name"] for e in armed_runtime.store.list(query="alpha"))
    kinds = {e["kind"] for e in armed_runtime.store.list(kind="capture")}
    assert kinds == {"capture"}


def test_storage_forecast_blocks_absurd_capture(armed_runtime):
    """FR-DAT-005: impossible capture sizes are refused up front."""
    with pytest.raises(ValueError, match="insufficient"):
        armed_runtime.record_capture("sim-pluto-0",
                                     num_samples=10 ** 15, segments=1)


def test_partial_experiment_readable_after_fault(armed_runtime, monkeypatch):
    """REF-08: an interrupted run leaves a readable, uncorrupted package."""
    dev = armed_runtime.device("sim-pluto-0")
    real_receive = dev.receive
    calls = {"n": 0}

    def flaky(num_samples, position=None):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise RuntimeError("injected power failure")
        return real_receive(num_samples, position=position)

    monkeypatch.setattr(dev, "receive", flaky)
    with pytest.raises(RuntimeError):
        armed_runtime.record_capture("sim-pluto-0", num_samples=8192, segments=5,
                                     name="interrupted")
    # the package exists, is listed, and its completed segments verify clean
    listed = [e for e in armed_runtime.store.list() if e["name"] == "interrupted"]
    assert listed, "partial experiment must still be listed"
    exp_id = listed[0]["experiment_id"]
    m = armed_runtime.store.load(exp_id)
    assert len(m["segments"]) == 2
    assert armed_runtime.store.verify(exp_id)["ok"] is True
