"""Long-running job interface (FR-API-003) and receive-path protection
(FR-SAF-005/006), plus the connector chain record (FR-RFC-006)."""

import time

import pytest

from forge_vision.jobs import (CANCELLED, FAILED, RUNNING, SUCCEEDED,
                               JobCancelled, JobManager)
from forge_vision.safety import SafetyViolation, rx_protection_check


def _wait(mgr, job_id, timeout=10.0, until=None):
    until = until or (lambda j: j.state in (SUCCEEDED, FAILED, CANCELLED))
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = mgr.get(job_id)
        if until(job):
            return job
        time.sleep(0.01)
    raise AssertionError(f"job stayed {mgr.get(job_id).state}")


# -- job lifecycle ----------------------------------------------------------
def test_job_runs_and_reports_progress():
    mgr = JobManager(max_workers=2)
    seen = []

    def work(ctx):
        for i in range(5):
            ctx.check()
            ctx.progress((i + 1) / 5, f"step {i + 1}")
            seen.append(i)
        return {"steps": 5}

    job = mgr.submit("test", "counts to five", work)
    done = _wait(mgr, job.job_id)
    assert done.state == SUCCEEDED
    assert done.result == {"steps": 5}
    assert done.progress == 1.0
    assert done.to_dict()["duration_s"] >= 0
    assert seen == [0, 1, 2, 3, 4]
    mgr.shutdown()


def test_job_failure_is_captured_not_raised():
    mgr = JobManager()

    def work(ctx):
        raise RuntimeError("scan hardware exploded")

    job = mgr.submit("test", "fails", work)
    done = _wait(mgr, job.job_id)
    assert done.state == FAILED
    assert "exploded" in done.error
    assert "RuntimeError" in done.to_dict(include_result=True)["traceback"]
    # the pool survives a failed job
    ok = mgr.submit("test", "still works", lambda ctx: 42)
    assert _wait(mgr, ok.job_id).result == 42
    mgr.shutdown()


def test_cooperative_cancellation():
    """Cancellation is cooperative so a job cannot be killed mid-write and
    leave a half-finished experiment package (FR-DAT-003)."""
    mgr = JobManager()
    started = []

    def work(ctx):
        started.append(True)
        for _ in range(500):
            ctx.check()
            time.sleep(0.01)
        return "never"

    job = mgr.submit("test", "long", work)
    _wait(mgr, job.job_id, until=lambda j: bool(started))
    mgr.cancel(job.job_id)
    done = _wait(mgr, job.job_id)
    assert done.state == CANCELLED
    assert done.result is None
    mgr.shutdown()


def test_job_ignoring_cancellation_is_reported_honestly():
    """A function that never checks its context runs to completion. The job
    must say the work finished, not pretend it was stopped."""
    mgr = JobManager()
    running = []

    def stubborn(ctx):
        running.append(True)
        time.sleep(0.25)          # never calls ctx.check()
        return "done"

    job = mgr.submit("test", "stubborn", stubborn)
    _wait(mgr, job.job_id, until=lambda j: bool(running))
    mgr.cancel(job.job_id)        # requested only after work began
    done = _wait(mgr, job.job_id)
    assert done.state == CANCELLED
    assert "already completed" in done.message
    assert done.result == "done", "the work did finish and must be reported"
    mgr.shutdown()


def test_cancel_before_start():
    mgr = JobManager(max_workers=1)
    blocker = mgr.submit("test", "blocks", lambda ctx: time.sleep(0.4))
    queued = mgr.submit("test", "queued", lambda ctx: "should not run")
    mgr.cancel(queued.job_id)
    done = _wait(mgr, queued.job_id)
    assert done.state == CANCELLED
    assert done.result is None
    _wait(mgr, blocker.job_id)
    mgr.shutdown()


def test_retry_resubmits_finished_job():
    mgr = JobManager()
    calls = []
    job = mgr.submit("test", "counts calls", lambda ctx: calls.append(1))
    _wait(mgr, job.job_id)
    again = mgr.retry(job.job_id)
    _wait(mgr, again.job_id)
    assert len(calls) == 2
    assert again.job_id != job.job_id
    mgr.shutdown()


def test_retry_refuses_while_running():
    mgr = JobManager()
    job = mgr.submit("test", "slow", lambda ctx: time.sleep(0.3))
    with pytest.raises(ValueError, match="still"):
        mgr.retry(job.job_id)
    _wait(mgr, job.job_id)
    mgr.shutdown()


def test_listing_and_summary():
    mgr = JobManager()
    mgr.submit("alpha", "a", lambda ctx: 1)
    mgr.submit("beta", "b", lambda ctx: 2)
    time.sleep(0.15)
    assert len(mgr.list()) == 2
    assert len(mgr.list(kind="alpha")) == 1
    assert mgr.summary()["counts"].get(SUCCEEDED) == 2
    mgr.shutdown()


def test_unknown_job_id():
    mgr = JobManager()
    with pytest.raises(KeyError):
        mgr.get("nope")
    mgr.shutdown()


# -- jobs through the runtime -----------------------------------------------
def test_survey_as_a_job_reports_progress_and_stores_result(runtime):
    runtime.connect("sim-pluto-0")
    job = runtime.submit_job("survey", {
        "device_id": "sim-pluto-0", "start_hz": 902e6, "stop_hz": 912e6,
        "step_hz": 2e6, "samples": 8192})
    done = _wait(runtime.jobs, job["job_id"], timeout=60)
    assert done.state == SUCCEEDED, done.error
    assert len(done.result["points"]) == 6
    assert runtime.store.load(done.result["experiment_id"])


def test_long_survey_can_be_cancelled(runtime):
    """A wide sweep must stop when asked, and leave no half-written result."""
    runtime.connect("sim-pluto-0")
    job = runtime.submit_job("survey", {
        "device_id": "sim-pluto-0", "start_hz": 400e6, "stop_hz": 3000e6,
        "step_hz": 10e6, "samples": 65536})
    _wait(runtime.jobs, job["job_id"],
          until=lambda j: j.state == RUNNING and j.progress > 0)
    runtime.jobs.cancel(job["job_id"])
    done = _wait(runtime.jobs, job["job_id"], timeout=60)
    assert done.state == CANCELLED
    # the device config must still be restored despite the abort
    assert runtime.device("sim-pluto-0").config.center_frequency_hz > 0


def test_unknown_job_kind_rejected(runtime):
    with pytest.raises(KeyError, match="unknown job kind"):
        runtime.submit_job("mine_bitcoin", {})


# -- FR-SAF-005 / FR-SAF-006: receive-path protection -----------------------
def test_bare_tx_to_rx_cable_is_critical():
    """The mistake that costs hardware: TX straight into RX, no attenuator."""
    check = rx_protection_check(tx_gain_db=0.0, rx_gain_db=40.0,
                                path_attenuation_db=0.0)
    assert check["severity"] == "critical"
    assert not check["safe"]
    assert check["rx_input_dbm"] == pytest.approx(7.0)
    assert any("damage threshold" in w for w in check["warnings"])
    assert any("attenuation" in w for w in check["warnings"])


def test_attenuated_loopback_is_acceptable():
    """REF-01 as the spec describes it: 30-40 dB inline attenuation."""
    check = rx_protection_check(tx_gain_db=-30.0, rx_gain_db=20.0,
                                path_attenuation_db=40.0)
    assert check["severity"] == "ok"
    assert check["safe"]
    assert check["warnings"] == []


def test_compression_warned_without_being_fatal():
    check = rx_protection_check(tx_gain_db=-10.0, rx_gain_db=5.0,
                                path_attenuation_db=10.0)
    assert check["rx_input_dbm"] == pytest.approx(-13.0)
    assert check["severity"] == "warn"
    assert any("compress" in w for w in check["warnings"])


def test_excess_rx_gain_flagged_as_clipping():
    check = rx_protection_check(tx_gain_db=-40.0, rx_gain_db=70.0,
                                path_attenuation_db=40.0)
    assert any("full scale" in w for w in check["warnings"])


def test_transmit_blocked_when_receiver_would_be_damaged(runtime):
    """FR-SAF-005 as an interlock, not advice — on real hardware."""
    from conftest import complete_checklist
    from forge_vision.waveforms import CATALOG
    complete_checklist(runtime)
    runtime.safety.arm("op", "ack")
    runtime.safety.declare_path_attenuation(0.0)      # bare TX->RX cable
    # -10 dB is within the transmit-gain limit, so this reaches the receive
    # check rather than tripping FR-SAF-004 first
    with pytest.raises(SafetyViolation, match="receive path protection"):
        runtime.safety.validate_tx(915e6, CATALOG["fmcw_bench_56M"],
                                   tx_gain_db=-10.0, rx_gain_db=40.0,
                                   enforce_rx_protection=True)
    assert "tx_blocked_rx_protection" in [e["event"] for e in
                                          runtime.safety.audit_tail()]


def test_simulated_receiver_is_warned_but_not_blocked(runtime):
    """The same configuration on the simulator is recorded, not refused —
    there is no physical receiver to protect."""
    from conftest import complete_checklist
    runtime.connect("sim-pluto-0")
    complete_checklist(runtime)
    runtime.safety.arm("op", "ack")
    runtime.safety.declare_path_attenuation(0.0)
    runtime.configure("sim-pluto-0", {"tx_gain_db": -30.0})
    runtime.set_tx("sim-pluto-0", True, "fmcw_bench_56M")
    assert runtime.device("sim-pluto-0").tx_enabled is True
    runtime.set_tx("sim-pluto-0", False)
    events = [e["event"] for e in runtime.safety.audit_tail()]
    assert "rx_protection_warning_not_enforced" in events, \
        "the warning must still be recorded for the operator"


def test_transmit_allowed_once_attenuation_declared(runtime):
    """With REF-01's 40 dB inline attenuation the same check passes even
    under full enforcement."""
    from conftest import complete_checklist
    from forge_vision.waveforms import CATALOG
    complete_checklist(runtime)
    runtime.safety.arm("op", "ack")
    runtime.safety.declare_path_attenuation(40.0)
    runtime.safety.validate_tx(915e6, CATALOG["fmcw_bench_56M"],
                               tx_gain_db=-30.0, rx_gain_db=20.0,
                               enforce_rx_protection=True)


def test_negative_attenuation_refused(runtime):
    with pytest.raises(SafetyViolation):
        runtime.safety.declare_path_attenuation(-5.0)


# -- FR-RFC-006: connector chain --------------------------------------------
def test_experiment_records_the_connector_chain(armed_runtime):
    rt = armed_runtime
    cable = rt.components.create(kind="cable", name="SMA 1m RG316",
                                 nominal_loss_db=0.6, nominal_delay_ns=4.9)
    atten = rt.components.create(kind="attenuator", name="30 dB SMA",
                                 nominal_loss_db=30.0, nominal_delay_ns=0.1)
    ant = rt.components.create(kind="antenna", name="vivaldi A")
    rt.set_rf_chain(tx_ids=[cable["component_id"], atten["component_id"]],
                    rx_ids=[cable["component_id"]],
                    antenna_tx=ant["component_id"])

    result = rt.range_run("sim-pluto-0", use_background=False)
    chain = rt.store.load(result["experiment_id"])["hardware"]["rf_chain"]
    assert [c["name"] for c in chain["tx_path"]] == ["SMA 1m RG316", "30 dB SMA"]
    assert chain["total_loss_db"] == pytest.approx(31.2)
    assert chain["total_delay_ns"] == pytest.approx(9.9)
    assert chain["antenna_tx"] == ant["component_id"]


def test_uncharacterised_components_are_flagged_not_assumed_lossless(runtime):
    mystery = runtime.components.create(kind="adapter", name="SMA-N barrel")
    chain = runtime.components.describe_chain([mystery["component_id"]], [])
    assert "SMA-N barrel" in chain["components_without_characterisation"]
    assert "higher than shown" in chain["note"]


def test_missing_component_id_does_not_break_the_chain(runtime):
    chain = runtime.components.describe_chain(["does-not-exist"], [])
    assert chain["tx_path"] == []
    assert "does-not-exist" in chain["components_without_characterisation"]
