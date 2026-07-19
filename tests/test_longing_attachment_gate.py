"""Longing gate on attachment gains + awake-hours absence clock."""

import time

from desire_engine import (
    attachment_gain_scale,
    awake_absence_hours,
    is_quiet_hours,
    longing_value,
    DesireEngine,
    DRIVE_BASELINES,
)


def test_attachment_gain_scale_bands():
    assert attachment_gain_scale(0.0) == 0.30
    assert attachment_gain_scale(0.14) == 0.30
    assert attachment_gain_scale(0.15) == 0.55
    assert attachment_gain_scale(0.34) == 0.55
    assert attachment_gain_scale(0.35) == 1.00
    assert attachment_gain_scale(0.69) == 1.00
    assert attachment_gain_scale(0.70) == 1.20
    assert attachment_gain_scale(1.0) == 1.20


def test_awake_absence_skips_quiet_hours(monkeypatch):
    # Force quiet for the whole window → 0 awake hours.
    monkeypatch.setattr("desire_engine.is_quiet_hours", lambda now_ts=None: True)
    assert awake_absence_hours(time.time() - 6 * 3600, time.time()) == 0.0

    # Force never quiet → full wall hours.
    monkeypatch.setattr("desire_engine.is_quiet_hours", lambda now_ts=None: False)
    hours = awake_absence_hours(time.time() - 3 * 3600, time.time())
    assert 2.9 < hours < 3.1


def test_pulse_attachment_gated_when_longing_low(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    state = engine.store.load_state()
    # Just messaged → longing ~0 → gate 0.30
    state.last_user_message_at = time.time()
    state.drives["attachment"] = DRIVE_BASELINES["attachment"]
    engine.store.save_state(state)

    before = engine.store.load_state().drives["attachment"]
    result = engine.pulse("attachment", delta=0.20)
    after = result["new_value"]
    assert result["longing_gate"] == 0.30
    # Full 0.20 would move more; gated path should move less than ungated pulse_gain max.
    assert after - before < 0.12
    assert after > before


def test_pulse_attachment_fuller_when_longing_high(tmp_path, monkeypatch):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    state = engine.store.load_state()
    state.last_user_message_at = time.time() - 48 * 3600
    state.drives["attachment"] = 0.55
    engine.store.save_state(state)
    monkeypatch.setattr("desire_engine.is_quiet_hours", lambda now_ts=None: False)

    ctx = engine._longing_context(engine.store.load_state())
    assert ctx["longing"] >= 0.15
    assert ctx["attachment_gain_scale"] >= 0.55

    low_engine = DesireEngine(db_path=str(tmp_path / "desire_low.db"))
    low_state = low_engine.store.load_state()
    low_state.last_user_message_at = time.time()
    low_state.drives["attachment"] = 0.55
    low_engine.store.save_state(low_state)

    high = engine.pulse("attachment", delta=0.18)
    low = low_engine.pulse("attachment", delta=0.18)
    assert high["longing_gate"] > low["longing_gate"]
    assert high["new_value"] >= low["new_value"]
