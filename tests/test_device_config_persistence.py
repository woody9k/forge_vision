"""Device configuration survives a restart (FR-DEV-002).

`DeviceConfig()` was constructed fresh for every registration, so centre
frequency, sample rate and both gains returned to their defaults on every
start — and `connect()` pushes them at the radio, so restarting the service
silently retuned the bench. An operator who set a receive gain, restarted,
and found it back at 40 dB was not imagining it.

Restoring the operator's settings is safe as well as least-surprising:
transmit still requires arming, which deliberately does not persist, and any
restored gain is re-checked by the TX fingerprint before it can key anything.
There is a test below asserting exactly that.
"""

from __future__ import annotations

import json
import os

import pytest

from forge_vision.devices.base import DeviceConfig
from forge_vision.server.runtime import Runtime

SIM = "sim-pluto-0"


def restart(tmp_path) -> Runtime:
    return Runtime(data_dir=str(tmp_path / "data"))


# -- the regression ----------------------------------------------------------

def test_configuration_survives_a_restart(tmp_path):
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 12.0, "center_frequency_hz": 2437e6})

    again = restart(tmp_path)
    cfg = again.device(SIM).config
    assert cfg.rx_gain_db == 12.0
    assert cfg.center_frequency_hz == 2437e6


def test_a_restart_no_longer_retunes_the_radio(tmp_path):
    """connect() pushes config at the hardware, so the restored value is what lands."""
    rt = restart(tmp_path)
    rt.configure(SIM, {"center_frequency_hz": 2437e6})
    again = restart(tmp_path)
    again.connect(SIM)
    assert again.device(SIM).config.center_frequency_hz == 2437e6


def test_restart_reports_restored_rather_than_default(tmp_path):
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 12.0})
    again = restart(tmp_path)
    dev = [d for d in again.status()["devices"] if d["device_id"] == SIM][0]
    assert dev["config_source"]["source"] == "restored"


def test_a_fresh_bench_reports_defaults_as_defaults(tmp_path):
    rt = restart(tmp_path)
    dev = [d for d in rt.status()["devices"] if d["device_id"] == SIM][0]
    assert dev["config_source"]["source"] == "default"


def test_every_configured_field_is_restored(tmp_path):
    rt = restart(tmp_path)
    wanted = {"center_frequency_hz": 1.2e9, "sample_rate_hz": 20e6,
              "rx_bandwidth_hz": 18e6, "rx_gain_db": 22.0, "tx_gain_db": -41.0}
    rt.configure(SIM, wanted)
    again = restart(tmp_path)
    got = again.device(SIM).config.to_dict()
    for k, v in wanted.items():
        assert got[k] == v, k


# -- restoring must not smuggle anything past the interlock ------------------

def test_a_restored_config_does_not_arm_anything(tmp_path):
    """Settings persist; permission to transmit does not."""
    rt = restart(tmp_path)
    rt.configure(SIM, {"tx_gain_db": -12.0})
    again = restart(tmp_path)
    assert again.device(SIM).config.tx_gain_db == -12.0
    assert again.safety.state.armed is False
    assert again.device(SIM).tx_enabled is False


def test_a_restored_config_is_clamped_to_the_device(tmp_path):
    """The saved values may predate a firmware change or a different radio."""
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 30.0})
    with open(rt._device_config_path, encoding="utf-8") as f:
        saved = json.load(f)
    saved[SIM]["rx_gain_db"] = 9999.0          # impossible for any front end
    with open(rt._device_config_path, "w", encoding="utf-8") as f:
        json.dump(saved, f)

    again = restart(tmp_path)
    caps = again.device(SIM).capabilities
    assert again.device(SIM).config.rx_gain_db <= caps.max_rx_gain_db


def test_clamping_is_reported_not_silent(tmp_path):
    rt = restart(tmp_path)
    rt.configure(SIM, {"center_frequency_hz": 1e9})
    with open(rt._device_config_path, encoding="utf-8") as f:
        saved = json.load(f)
    saved[SIM]["center_frequency_hz"] = 1e12
    with open(rt._device_config_path, "w", encoding="utf-8") as f:
        json.dump(saved, f)

    again = restart(tmp_path)
    note = again.device_config_restore[SIM]["note"]
    assert note and "limits" in note


# -- failure paths -----------------------------------------------------------

def test_an_unreadable_file_falls_back_to_defaults_and_audits(tmp_path):
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 12.0})
    with open(rt._device_config_path, "w", encoding="utf-8") as f:
        f.write("{ not json")

    again = restart(tmp_path)
    assert again.device(SIM).config.rx_gain_db == DeviceConfig().rx_gain_db
    assert any(e["event"] == "device_config_restore_failed"
               for e in again.safety.audit_tail(20))


def test_a_saved_entry_for_an_absent_device_is_ignored(tmp_path):
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 12.0})
    with open(rt._device_config_path, encoding="utf-8") as f:
        saved = json.load(f)
    saved["pluto-ip:some-radio-that-is-gone"] = {"rx_gain_db": 5.0}
    with open(rt._device_config_path, "w", encoding="utf-8") as f:
        json.dump(saved, f)

    again = restart(tmp_path)          # must not raise
    assert again.device(SIM).config.rx_gain_db == 12.0


def test_unknown_fields_in_a_saved_config_are_ignored(tmp_path):
    """A field removed from DeviceConfig must not break every later start."""
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 12.0})
    with open(rt._device_config_path, encoding="utf-8") as f:
        saved = json.load(f)
    saved[SIM]["a_field_from_a_future_version"] = 1
    with open(rt._device_config_path, "w", encoding="utf-8") as f:
        json.dump(saved, f)

    again = restart(tmp_path)
    assert again.device(SIM).config.rx_gain_db == 12.0


def test_a_failed_save_is_reported_and_leaves_no_temp_file(tmp_path, monkeypatch):
    rt = restart(tmp_path)
    monkeypatch.setattr(os, "replace",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    out = rt.configure(SIM, {"rx_gain_db": 7.0})
    assert rt.device(SIM).config.rx_gain_db == 7.0       # applied now
    assert "could not be saved" in out["config_not_saved"]
    monkeypatch.undo()
    d = os.path.dirname(rt._device_config_path)
    assert [p for p in os.listdir(d) if p.endswith(".tmp")] == []


def test_saving_is_atomic(tmp_path):
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 7.0})
    with open(rt._device_config_path, encoding="utf-8") as f:
        json.load(f)                                     # parses, so it is whole
    d = os.path.dirname(rt._device_config_path)
    assert [p for p in os.listdir(d) if p.endswith(".tmp")] == []


def test_configs_are_kept_per_device(tmp_path):
    rt = restart(tmp_path)
    rt._register(type(rt.device(SIM))("sim-pluto-1"))
    rt.configure(SIM, {"rx_gain_db": 11.0})
    rt.configure("sim-pluto-1", {"rx_gain_db": 22.0})

    again = restart(tmp_path)
    again._register(type(again.device(SIM))("sim-pluto-1"))
    assert again.device(SIM).config.rx_gain_db == 11.0
    assert again.device("sim-pluto-1").config.rx_gain_db == 22.0
