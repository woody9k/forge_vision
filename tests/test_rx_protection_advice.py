"""Receive-protection warnings must be actionable and state their premise.

Two defects, both reported by an operator who followed the advice and watched
it fail:

  * "Reduce RX gain" was emitted whenever input+gain exceeded ADC full scale,
    including when the *input alone* already did. Turning the gain down to 0
    then leaves the warning unchanged with nothing explaining why, because no
    gain setting could ever have cleared it.

  * The estimate assumes transmit is cabled straight into receive
    (`path_attenuation_db = 0`, the default). On a bench with two antennas
    that is wrong by 30-60 dB, and the message asserted the resulting number
    without saying what it rested on.

Neither fix makes the warning quieter. A warning nobody can act on is not
caution, it is noise that trains an operator to ignore the panel.
"""

from __future__ import annotations

from forge_vision.safety import (RX_FULL_SCALE_DBM, PLUTO_TX_MAX_OUTPUT_DBM,
                                 rx_protection_check)


def _fullscale_warning(result) -> str:
    for w in result["warnings"]:
        if "full scale" in w:
            return w
    return ""


# -- advice that cannot be followed ------------------------------------------

def test_it_does_not_tell_you_to_reduce_gain_when_that_cannot_help():
    """TX -30 dB into a bare cable puts -23 dBm in, past the -30 dBm ceiling."""
    r = rx_protection_check(-30.0, 40.0, 0.0)
    w = _fullscale_warning(r)
    assert "Reducing RX gain cannot fix this" in w
    assert "Reduce RX gain to" not in w


def test_the_advice_is_the_same_at_zero_gain_and_says_why():
    """The operator went to 0 dB and the warning stayed. It should explain."""
    at40 = _fullscale_warning(rx_protection_check(-30.0, 40.0, 0.0))
    at0 = _fullscale_warning(rx_protection_check(-30.0, 0.0, 0.0))
    assert "cannot fix this" in at0
    assert "even at 0 dB it would still clip" in at0
    # the diagnosis does not depend on the gain, because the gain is not the cause
    assert at40 == at0


def test_it_says_how_much_isolation_would_actually_help():
    r = rx_protection_check(-30.0, 40.0, 0.0)
    over = (PLUTO_TX_MAX_OUTPUT_DBM - 30.0) - RX_FULL_SCALE_DBM
    assert f"{over:.0f} dB more isolation" in _fullscale_warning(r)


def test_a_reachable_gain_limit_is_given_when_gain_really_is_the_problem():
    """With isolation declared, turning the gain down does work — so say by how much."""
    r = rx_protection_check(-30.0, 40.0, 40.0)
    w = _fullscale_warning(r)
    assert "Reduce RX gain to" in w
    assert "cannot fix this" not in w


def test_the_stated_gain_limit_actually_clears_the_warning():
    """Advice that is followed must succeed, or it is the same defect again."""
    import re
    r = rx_protection_check(-30.0, 40.0, 40.0)
    limit = float(re.search(r"Reduce RX gain to (-?\d+) dB", _fullscale_warning(r)).group(1))
    assert _fullscale_warning(rx_protection_check(-30.0, limit, 40.0)) == ""


# -- the premise is stated, not assumed --------------------------------------

def test_undeclared_attenuation_says_the_estimate_assumes_a_bare_cable():
    r = rx_protection_check(-30.0, 40.0, 0.0)
    joined = " ".join(r["warnings"])
    assert "straight into" in joined
    assert "separate antennas" in joined


def test_it_still_warns_loudly_about_a_real_cabled_loopback():
    """Naming the assumption must not soften the case it exists for."""
    r = rx_protection_check(0.0, 40.0, 0.0)
    assert r["severity"] == "critical"
    assert r["safe"] is False


def test_declaring_isolation_clears_the_undeclared_warning():
    r = rx_protection_check(-30.0, 20.0, 60.0)
    assert not any("straight into" in w for w in r["warnings"])
    assert r["severity"] == "ok"


# -- the numbers themselves --------------------------------------------------

def test_gain_below_the_stated_limit_is_reported_safe():
    r = rx_protection_check(-30.0, 10.0, 60.0)
    assert r["severity"] == "ok" and r["safe"] is True


def test_damage_threshold_still_dominates():
    """A genuinely dangerous level must not be reported as a gain problem."""
    r = rx_protection_check(0.0, 0.0, 0.0)
    assert r["severity"] == "critical"
    assert any("damage threshold" in w for w in r["warnings"])
