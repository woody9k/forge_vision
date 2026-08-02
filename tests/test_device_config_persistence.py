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

from forge_vision.devices.base import ConfigurationError, DeviceConfig
from forge_vision.devices.simulated import SimulatedPluto
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


# -- the restore must reach the hardware, not just the cache -----------------

class PreConnectedRadio(SimulatedPluto):
    """A radio that is already connected when the runtime registers it.

    This is the real hardware order: `PlutoDevice.discover()` calls
    `connect()` — which pushes `DeviceConfig()` defaults through `_apply()` —
    and only then hands the device to `Runtime._register()`. Every test that
    registers a disconnected simulator exercises the opposite order and
    cannot see the defect this class exists to catch.
    """

    def __init__(self, device_id="pre-connected"):
        super().__init__(device_id)
        self.pushed_to_hardware = None
        self.connect()

    def connect(self):
        super().connect()
        self._apply_to_hardware()

    def configure(self, cfg):
        super().configure(cfg)
        if self.connected:
            self._apply_to_hardware()

    def _apply_to_hardware(self):
        self.pushed_to_hardware = self.config.to_dict()


def test_a_config_restored_onto_a_connected_radio_reaches_the_hardware(tmp_path):
    """Assigning dev.config would leave the radio on defaults while the API
    reported the restored values — asserting a configuration it never had."""
    rt = restart(tmp_path)
    rt._register(PreConnectedRadio())
    rt.configure("pre-connected", {"center_frequency_hz": 2437e6,
                                   "rx_gain_db": 12.0})

    again = restart(tmp_path)
    dev = PreConnectedRadio()
    again._register(dev)
    assert dev.pushed_to_hardware["center_frequency_hz"] == 2437e6
    assert dev.pushed_to_hardware["rx_gain_db"] == 12.0


def test_what_the_api_reports_matches_what_the_hardware_was_given(tmp_path):
    rt = restart(tmp_path)
    rt._register(PreConnectedRadio())
    rt.configure("pre-connected", {"center_frequency_hz": 2437e6})

    again = restart(tmp_path)
    dev = PreConnectedRadio()
    again._register(dev)
    described = [d for d in again.status()["devices"]
                 if d["device_id"] == "pre-connected"][0]
    assert (described["config"]["center_frequency_hz"]
            == dev.pushed_to_hardware["center_frequency_hz"])
    assert described["config_source"]["applied_to_hardware"] is True


def test_a_device_the_hardware_refuses_is_not_reported_as_restored(tmp_path):
    rt = restart(tmp_path)
    rt._register(PreConnectedRadio())
    rt.configure("pre-connected", {"rx_gain_db": 12.0})

    again = restart(tmp_path)

    class Refuses(PreConnectedRadio):
        def configure(self, cfg):
            raise ConfigurationError("driver said no")

    again._register(Refuses())
    src = again.device_config_restore["pre-connected"]
    assert src["source"] == "default"
    assert "refused by the device" in src["note"]


# -- the same board reached a different way -----------------------------------

def test_switching_transport_carries_the_current_config_not_a_stale_one(tmp_path):
    """`usb:` and `ip:...` are the same board, but device ids are per-URI, so
    the new entry would restore *its own* saved key — pushing an old
    configuration onto the radio and calling it `restored` at the moment it
    discarded the operator's current one."""
    rt = restart(tmp_path)

    old = PreConnectedRadio("pluto-ip:bench")
    rt._register(old)
    rt.configure("pluto-ip:bench", {"center_frequency_hz": 2437e6,
                                    "rx_gain_db": 12.0})
    # a stale configuration saved months ago under the other transport
    rt._saved_device_configs["pluto-usb:"] = {
        **DeviceConfig().to_dict(), "center_frequency_hz": 433e6,
        "rx_gain_db": 40.0}

    new = PreConnectedRadio("pluto-usb:")
    rt._register(new, carry_config=rt.device("pluto-ip:bench").config)

    assert new.pushed_to_hardware["center_frequency_hz"] == 2437e6, (
        "the operator's current settings must follow the board")
    assert rt.device_config_restore["pluto-usb:"]["source"] == "carried"


def test_a_serial_less_board_is_told_its_settings_are_per_transport(tmp_path):
    """The bench's own radio reports an empty hw_serial — `discovery.py` calls
    that "the common case" — so the alias gate is *closed* here and switching
    transport then restarting silently presents a stale entry as the
    operator's choice. The two tests below pass only because their fixtures
    were handed a serial the real board does not have; this one is built from
    what the bench actually reports."""
    rt = restart(tmp_path)
    dev = PreConnectedRadio("pluto-ip:192.168.99.222")
    dev.discovery = {"identified_by": "attributes",
                     "alternatives": [{"uri": "usb:"},
                                      {"uri": "ip:192.168.99.222"}]}
    rt._register(dev)
    rt.configure("pluto-ip:192.168.99.222", {"center_frequency_hz": 2450e6})

    described = [d for d in rt.status()["devices"]
                 if d["device_id"] == "pluto-ip:192.168.99.222"][0]
    assert described["config_source"]["saved_per_transport"] is True
    assert "no serial number" in described["config_source"]["note"]


def _serial_less(device_id, uris=("usb:", "ip:192.168.99.222")):
    dev = PreConnectedRadio(device_id)
    dev.discovery = {"identified_by": "attributes",
                     "alternatives": [{"uri": u} for u in uris]}
    return dev


def test_the_per_transport_note_describes_what_actually_happens(tmp_path):
    """Assert the claim, not the sentence.

    The previous version checked that the note *contained* "will not carry
    them across" — which locked in wording that was false: a switch on its
    own does carry, because the carry path saves under the new id. A test
    that matches substrings will happily pin an inaccurate claim in place.
    So drive the three cases and check the note matches each outcome.
    """
    # A: switch, change nothing, restart on the other transport -> carries
    rt = restart(tmp_path)
    rt._register(_serial_less("pluto-ip:192.168.99.222"))
    rt.configure("pluto-ip:192.168.99.222", {"center_frequency_hz": 2437e6})
    rt._register(_serial_less("pluto-usb:"),
                 carry_config=rt.device("pluto-ip:192.168.99.222").config)

    back = restart(tmp_path)
    eth = _serial_less("pluto-ip:192.168.99.222")
    back._register(eth)
    assert eth.pushed_to_hardware["center_frequency_hz"] == 2437e6, (
        "a switch with no later change does carry — the note must not deny it")

    # B: change *after* switching -> that change is stranded on that transport
    rt2 = restart(tmp_path)
    rt2._register(_serial_less("pluto-ip:192.168.99.222"))
    rt2.configure("pluto-ip:192.168.99.222", {"center_frequency_hz": 2437e6})
    rt2._register(_serial_less("pluto-usb:"),
                  carry_config=rt2.device("pluto-ip:192.168.99.222").config)
    rt2.configure("pluto-usb:", {"center_frequency_hz": 2450e6})

    back2 = restart(tmp_path)
    eth2 = _serial_less("pluto-ip:192.168.99.222")
    back2._register(eth2)
    assert eth2.pushed_to_hardware["center_frequency_hz"] == 2437e6, (
        "the post-switch change is stranded — this is the real failure")

    note = [d for d in back2.status()["devices"]
            if d["device_id"] == "pluto-ip:192.168.99.222"
            ][0]["config_source"]["note"]
    # the note must describe *this* case: a change made after switching
    assert "changed after switching" in note
    # and must not blame the operator's button alone, since discovery can
    # move transports on any boot without anyone touching it
    assert "discovery picked differently" in note


def test_config_source_has_the_same_keys_for_every_device(tmp_path):
    """One /api/status returned four keys for a discovered radio and two for
    the simulator, so a consumer indexing `applied_to_hardware` raised on one
    and worked on the other."""
    rt = restart(tmp_path)
    rt._register(_serial_less("pluto-ip:192.168.99.222"))
    shapes = {frozenset(d["config_source"])
              for d in rt.status()["devices"]}
    assert len(shapes) == 1, f"config_source shapes differ: {shapes}"
    assert {"source", "note", "applied_to_hardware",
            "saved_per_transport"} <= set(next(iter(shapes)))


def test_a_serial_identified_board_is_not_warned(tmp_path):
    """The limitation is real only when the gate is closed."""
    rt = restart(tmp_path)
    dev = PreConnectedRadio("pluto-ip:bench")
    dev.discovery = {"identified_by": "serial",
                     "alternatives": [{"uri": "usb:"}, {"uri": "ip:bench"}]}
    rt._register(dev)
    described = [d for d in rt.status()["devices"]
                 if d["device_id"] == "pluto-ip:bench"][0]
    assert described["config_source"].get("saved_per_transport") is not True


def test_every_transport_of_a_serial_identified_board_shares_a_saved_config(tmp_path):
    """Device ids are per-URI but the board is one radio. Saving under a
    single key meant a restart landing on the other transport restored a
    stale entry and called it `restored` — observed setting a radio to
    2437 MHz / RX 40 dB when the last choice was 2450 MHz / RX 18 dB."""
    rt = restart(tmp_path)
    dev = PreConnectedRadio("pluto-ip:bench")
    dev.discovery = {"uri": "ip:bench", "identified_by": "serial",
                     "alternatives": [{"uri": "usb:"}, {"uri": "ip:bench"}]}
    rt._register(dev)
    rt.configure("pluto-ip:bench", {"center_frequency_hz": 2450e6,
                                    "rx_gain_db": 18.0})

    saved = rt._saved_device_configs
    assert "pluto-usb:" in saved, "the other transport of the same board"
    assert saved["pluto-usb:"]["center_frequency_hz"] == 2450e6
    assert saved["pluto-usb:"]["rx_gain_db"] == 18.0


def test_aliases_are_only_written_when_the_board_is_identified_by_serial(tmp_path):
    """`group_boards` returns `identified_by` because "these transports are
    one radio" is sometimes an inference: with no serial it matches on
    attributes, and two identical Plutos — or two boards whose identity probe
    failed — are indistinguishable that way. Writing aliases on that would
    save one radio's configuration under another's id."""
    rt = restart(tmp_path)
    dev = PreConnectedRadio("pluto-ip:bench")
    dev.discovery = {"identified_by": "attributes",
                     "alternatives": [{"uri": "usb:"}, {"uri": "ip:bench"}]}
    rt._register(dev)
    rt.configure("pluto-ip:bench", {"center_frequency_hz": 2450e6})

    assert "pluto-usb:" not in rt._saved_device_configs, (
        "an attribute-matched grouping is not evidence of one board")
    assert "pluto-ip:bench" in rt._saved_device_configs


def test_a_second_radio_is_not_configured_from_the_first(tmp_path):
    """The failure this guards: two identical serial-less Plutos group as one,
    so radio B would be set from radio A's saved config and reported as
    `restored`."""
    rt = restart(tmp_path)
    a = PreConnectedRadio("pluto-ip:radio-a")
    a.discovery = {"identified_by": "attributes",
                   "alternatives": [{"uri": "ip:radio-a"}, {"uri": "usb:1.5.5"}]}
    rt._register(a)
    rt.configure("pluto-ip:radio-a", {"center_frequency_hz": 2437e6,
                                      "rx_gain_db": 12.0})

    b = PreConnectedRadio("pluto-usb:1.5.5")     # a *different* radio
    rt._register(b)
    assert b.pushed_to_hardware["center_frequency_hz"] != 2437e6, (
        "radio B must not be tuned from radio A's saved configuration")
    assert rt.device_config_restore.get("pluto-usb:1.5.5", {}).get(
        "source", "default") == "default"


def test_a_serial_identified_board_restores_across_transports(tmp_path):
    rt = restart(tmp_path)
    dev = PreConnectedRadio("pluto-ip:bench")
    dev.discovery = {"identified_by": "serial",
                     "alternatives": [{"uri": "usb:"}, {"uri": "ip:bench"}]}
    rt._register(dev)
    rt.configure("pluto-ip:bench", {"center_frequency_hz": 2450e6,
                                    "rx_gain_db": 18.0})

    # discovery picks the faster transport next boot — a different device id
    again = restart(tmp_path)
    usb = PreConnectedRadio("pluto-usb:")
    again._register(usb)
    assert usb.pushed_to_hardware["center_frequency_hz"] == 2450e6
    assert usb.pushed_to_hardware["rx_gain_db"] == 18.0


def test_a_carry_that_cannot_be_saved_says_so(tmp_path, monkeypatch):
    """Auditing alone reintroduced the swallowed-failure defect: the carry
    works, the save fails, and the next restart quietly reverts."""
    rt = restart(tmp_path)
    rt._register(PreConnectedRadio("pluto-ip:bench"))
    rt.configure("pluto-ip:bench", {"center_frequency_hz": 2437e6})

    monkeypatch.setattr(os, "replace",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    rt._register(PreConnectedRadio("pluto-usb:"),
                 carry_config=rt.device("pluto-ip:bench").config)
    note = rt.device_config_restore["pluto-usb:"]["note"] or ""
    assert "could not be saved" in note


def test_a_carried_config_is_saved_under_the_new_id(tmp_path):
    """So the next restart restores what is actually on the radio."""
    rt = restart(tmp_path)
    old = PreConnectedRadio("pluto-ip:bench")
    rt._register(old)
    rt.configure("pluto-ip:bench", {"center_frequency_hz": 2437e6})
    rt._register(PreConnectedRadio("pluto-usb:"),
                 carry_config=rt.device("pluto-ip:bench").config)
    assert (rt._saved_device_configs["pluto-usb:"]["center_frequency_hz"]
            == 2437e6)


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

def test_a_disconnected_device_validates_the_saved_config_too(tmp_path):
    """clamp_config does not touch channel indices, so without an explicit
    validate the disconnected path accepted and reported an rx_channel no
    adapter would honour — while the connected path refused the same file."""
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 12.0})
    with open(rt._device_config_path, encoding="utf-8") as f:
        saved = json.load(f)
    saved[SIM]["rx_channel"] = 7
    with open(rt._device_config_path, "w", encoding="utf-8") as f:
        json.dump(saved, f)

    again = restart(tmp_path)
    assert again.device(SIM).config.rx_channel == DeviceConfig().rx_channel
    src = again.device_config_restore[SIM]
    assert src["source"] == "default"
    assert "rx channel" in src["note"]


class HalfWrites(PreConnectedRadio):
    """A radio whose apply genuinely fails part-way through.

    An earlier version of this fixture raised *before* writing anything, so
    the radio really was at its starting values and the assertion held
    vacuously — the docstring claimed "fails midway" and the fixture did not.
    `PlutoDevice._apply()` writes seven libiio attributes in sequence, so it
    updates some and abandons the rest.
    """

    def configure(self, cfg):
        SimulatedPluto.configure(self, cfg)          # assigns self.config
        # rate and bandwidth land; the LO and gains never do
        self.pushed_to_hardware = {
            **(self.pushed_to_hardware or {}),
            "sample_rate_hz": cfg.sample_rate_hz,
            "rx_bandwidth_hz": cfg.rx_bandwidth_hz,
        }
        raise ConfigurationError("libiio write failed after rx_rf_bandwidth")

    def read_hardware_config(self):
        return dict(self.pushed_to_hardware or {})


def test_a_partial_write_reports_what_the_radio_actually_holds(tmp_path):
    """Rolling back to the pre-restore config swaps one false claim for
    another: after a partial write the radio holds a mixture neither
    describes. Ask it instead."""
    rt = restart(tmp_path)
    rt._register(PreConnectedRadio("half-writer"))
    rt.configure("half-writer", {"sample_rate_hz": 20e6,
                                 "rx_bandwidth_hz": 18e6})

    again = restart(tmp_path)
    dev = HalfWrites("half-writer")
    again._register(dev)

    # the two fields that did land must be what the API now reports
    assert dev.config.sample_rate_hz == 20e6
    assert dev.config.rx_bandwidth_hz == 18e6
    src = again.device_config_restore["half-writer"]
    assert src["source"] == "default"
    assert "read back from the radio" in src["note"]


def test_an_unreadable_radio_after_a_partial_write_is_not_asserted(tmp_path):
    """If the read-back also fails, neither config is trustworthy — so the
    platform must say it does not know rather than pick one."""
    rt = restart(tmp_path)
    rt._register(PreConnectedRadio("blind"))
    # bandwidth must come down with the rate, or configure() refuses the pair
    rt.configure("blind", {"sample_rate_hz": 20e6, "rx_bandwidth_hz": 18e6})

    again = restart(tmp_path)

    class Blind(HalfWrites):
        def read_hardware_config(self):
            raise OSError("libiio timed out")

    again._register(Blind("blind"))
    assert "unverified" in again.device_config_restore["blind"]["note"]
    rec = again.sync_record("blind")
    assert rec["in_sync"] is None and rec["readable"] is False


def test_gain_clamps_are_reported_not_silent(tmp_path):
    """Every other field reported what the clamp moved; gains did not — and a
    transmit gain clamps *upward*, toward more power."""
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 30.0})
    with open(rt._device_config_path, encoding="utf-8") as f:
        saved = json.load(f)
    saved[SIM]["rx_gain_db"] = 9999.0
    saved[SIM]["tx_gain_db"] = -9999.0
    with open(rt._device_config_path, "w", encoding="utf-8") as f:
        json.dump(saved, f)

    again = restart(tmp_path)
    note = again.device_config_restore[SIM]["note"] or ""
    assert "rx gain" in note and "tx gain" in note


def test_a_corrupt_file_is_distinguishable_from_a_fresh_bench(tmp_path):
    """Both fall back to defaults; only one of them lost something."""
    fresh = restart(tmp_path)
    fresh_src = [d for d in fresh.status()["devices"]
                 if d["device_id"] == SIM][0]["config_source"]

    fresh.configure(SIM, {"rx_gain_db": 12.0})
    with open(fresh._device_config_path, "w", encoding="utf-8") as f:
        f.write("{ not json")
    broken = restart(tmp_path)
    broken_src = [d for d in broken.status()["devices"]
                  if d["device_id"] == SIM][0]["config_source"]

    assert fresh_src["note"] is None
    assert broken_src["note"] and "could not be read" in broken_src["note"]


def test_a_failed_save_does_not_leak_into_a_later_successful_one(tmp_path,
                                                                monkeypatch):
    """Memory and disk must not diverge, or a later save on another device
    persists the entry the operator was told had not been saved."""
    rt = restart(tmp_path)
    rt._register(type(rt.device(SIM))("sim-pluto-1"))
    rt.configure(SIM, {"rx_gain_db": 5.0})            # succeeds

    monkeypatch.setattr(os, "replace",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    rt.configure(SIM, {"rx_gain_db": 33.0})           # fails to save
    monkeypatch.undo()
    rt.configure("sim-pluto-1", {"rx_gain_db": 7.0})  # succeeds

    again = restart(tmp_path)
    assert again.device(SIM).config.rx_gain_db == 5.0, (
        "the unsaved 33 dB must not have been smuggled onto disk")


def test_the_save_failure_message_names_what_it_would_revert_to(tmp_path,
                                                               monkeypatch):
    rt = restart(tmp_path)
    rt.configure(SIM, {"rx_gain_db": 5.0})            # a good save exists
    monkeypatch.setattr(os, "replace",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    out = rt.configure(SIM, {"rx_gain_db": 33.0})
    assert "last configuration that saved successfully" in out["config_not_saved"]


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
