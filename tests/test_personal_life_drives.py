"""Private-life triad: curiosity / stewardship / reflection stay daily-active."""

import time

from desire_engine import (
    DRIVE_BASELINES,
    PERSONAL_LIFE_DRIVES,
    DriveState,
    drive_activation,
    pick_intent,
    tick_drives,
)


def test_curiosity_baseline_no_longer_dead_zone():
    # Old 0.40 baseline made raw≈0.40 look "high" but act=0 forever.
    assert DRIVE_BASELINES["curiosity"] <= 0.25
    assert drive_activation("curiosity", 0.34) > 0.25


def test_ambient_lifts_personal_drives_within_an_hour():
    state = DriveState(drives=dict(DRIVE_BASELINES), last_ts=time.time() - 900)
    now = time.time()
    for _ in range(4):
        state = tick_drives(state, now_ts=now, idle_seconds=900)
        state.last_ts = now
        now += 900

    for key in PERSONAL_LIFE_DRIVES:
        assert state.drives[key] > DRIVE_BASELINES[key] + 0.05
        assert drive_activation(key, state.drives[key]) >= 0.28


def test_personal_intent_can_fire_without_heat_spike():
    state = DriveState(drives=dict(DRIVE_BASELINES), last_ts=time.time() - 900)
    now = time.time()
    for _ in range(4):
        state = tick_drives(state, now_ts=now, idle_seconds=900)
        state.last_ts = now
        now += 900

    intent = pick_intent(state, refractory={})
    assert intent is not None
    assert intent["drive_key"] in PERSONAL_LIFE_DRIVES


def test_heat_still_outranks_personal_when_loud():
    state = DriveState(drives=dict(DRIVE_BASELINES), last_ts=time.time() - 900)
    now = time.time()
    for _ in range(4):
        state = tick_drives(state, now_ts=now, idle_seconds=900)
        state.last_ts = now
        now += 900
    state.drives["libido"] = 0.55

    intent = pick_intent(state, refractory={})
    assert intent is not None
    assert intent["drive_key"] == "libido"
