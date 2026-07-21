# ============================================================
# Test: ESM软互抑 + 逃逸阀 + PA/NA展示层（P3）
# 作用在已有9维drive上，不另起PA/NA持久状态。
# ============================================================

import pytest

from desire_engine import (
    DRIVE_BASELINES,
    POSITIVE_GROUP,
    NEGATIVE_GROUP,
    ESM_K,
    ESCAPE_VALVE_STREAK_TRIGGER,
    ESCAPE_VALVE_EXCESS_GAP,
    apply_esm_inhibition,
    apply_escape_valve,
    apply_longing_adjustment,
    pa_na_snapshot,
    DriveState,
    DesireEngine,
    LONGING_REUNION_THRESHOLD_HOURS,
    tick_drives,
    longing_value,
    longing_phase,
    reunion_boost_for_return,
    refuse_intent,
    satisfy,
    pulse_attachment_nonlinear,
    drive_activation,
    effective_drive_activation,
    pick_intent,
    _feature_value,
    GriefState,
    LEGACY_RETURN_RUMINATION_PREFIX,
)


def _baseline_drives():
    return dict(DRIVE_BASELINES)


def test_activation_uses_each_drive_own_baseline():
    assert drive_activation("curiosity", DRIVE_BASELINES["curiosity"]) == 0.0
    assert drive_activation("possessiveness", DRIVE_BASELINES["possessiveness"]) == 0.0
    assert drive_activation("possessiveness", 0.50) > drive_activation("attachment", 0.50)
    assert effective_drive_activation("stewardship", 0.60, 0.25) < drive_activation("stewardship", 0.60)


def test_intent_threshold_uses_activation_not_raw_value():
    from desire_engine import INTENT_THRESHOLD

    drives = _baseline_drives()
    drives["stewardship"] = 0.52  # sqrt headroom act ≈ 0.63
    state = DriveState(drives=drives)

    intent = pick_intent(state, refractory={})

    assert intent is not None
    assert intent["drive_key"] == "stewardship"
    assert intent["score"] >= INTENT_THRESHOLD


def test_delta_coupling_scales_with_actual_tick_movement():
    # Start above personal ambient floor so ambient lift does not mask coupling.
    drives = _baseline_drives()
    drives["curiosity"] = 0.55
    drives["reflection"] = 0.55
    state = DriveState(drives=drives, last_ts=1000.0)
    before_r = state.drives["reflection"]

    ticked = tick_drives(state, now_ts=1900.0, idle_seconds=900)

    # Delta coupling is tiny vs damping; reflection should not jump +0.04 style.
    # Allow ambient-free path: net move stays modest.
    assert abs(ticked.drives["reflection"] - before_r) < 0.08


def test_level_coupling_and_damping_are_nearly_tick_rate_invariant():
    drives = {**DRIVE_BASELINES, "stress": 0.50}
    one_hour = tick_drives(DriveState(drives=drives, last_ts=1000.0), now_ts=4600.0)
    two_halves = tick_drives(DriveState(drives=drives, last_ts=1000.0), now_ts=2800.0)
    two_halves = tick_drives(two_halves, now_ts=4600.0)

    # 15m reference tick_scale + ambient on personal drives; stress/attachment
    # should still be near rate-invariant within a small band.
    assert abs(one_hour.drives["attachment"] - two_halves.drives["attachment"]) < 0.01
    assert abs(one_hour.drives["stress"] - two_halves.drives["stress"]) < 0.01


def test_refuse_lowers_target_drive_but_less_than_satisfy():
    drives = _baseline_drives()
    drives["attachment"] = 0.80
    state = DriveState(drives=drives)

    refused = refuse_intent(state, "attachment")
    satisfied = satisfy(state, "attachment")

    assert abs(refused.drives["attachment"] - 0.60) < 1e-9
    assert abs(satisfied.drives["attachment"] - 0.48) < 1e-9
    assert satisfied.drives["attachment"] < refused.drives["attachment"]


# ─── ESM互抑 ──────────────────────────────────────────────────────────────

def test_esm_inhibition_bittersweet_coexist():
    """甜蜜又心疼：正向和负向都高于baseline时，互相压一点但都还在线以上。"""
    drives = _baseline_drives()
    drives["attachment"] = 0.70   # excess 0.40
    drives["stress"] = 0.60       # excess 0.45

    result = apply_esm_inhibition(drives)

    att_excess = result["attachment"] - DRIVE_BASELINES["attachment"]
    stress_excess = result["stress"] - DRIVE_BASELINES["stress"]

    # 两边都被压了，但都还在baseline之上——不是清零
    assert 0 < att_excess < 0.40
    assert 0 < stress_excess < 0.45

    # 跟手算的k=0.3公式对得上——互抑系数乘的是"对方分组的平均excess"
    neg_group_excess = 0.45 / len(NEGATIVE_GROUP)  # 只有stress有excess
    pos_group_excess = 0.40 / len(POSITIVE_GROUP)  # 只有attachment有excess
    expected_att_excess = 0.40 * (1 - ESM_K * neg_group_excess)
    expected_stress_excess = 0.45 * (1 - ESM_K * pos_group_excess)
    assert abs(att_excess - expected_att_excess) < 1e-6
    assert abs(stress_excess - expected_stress_excess) < 1e-6


def test_esm_inhibition_no_excess_untouched():
    """没人超过baseline时，互抑不改变任何值。"""
    drives = _baseline_drives()
    result = apply_esm_inhibition(drives)
    for k, v in drives.items():
        assert abs(result[k] - v) < 1e-9


def test_esm_inhibition_never_below_baseline():
    """互抑只压"超出部分"，不会把drive压到baseline以下。"""
    drives = _baseline_drives()
    drives["attachment"] = 0.90
    drives["stress"] = 0.90
    drives["fatigue"] = 0.90
    result = apply_esm_inhibition(drives)
    for k in POSITIVE_GROUP + NEGATIVE_GROUP:
        assert result[k] >= DRIVE_BASELINES[k] - 1e-9


# ─── 逃逸阀 ──────────────────────────────────────────────────────────────

def test_escape_valve_single_imbalance_does_not_trigger():
    """单次失衡不触发——逃逸阀用streak计数，防止单次评分误判。"""
    drives = _baseline_drives()
    drives["stress"] = 0.80
    drives["fatigue"] = 0.70

    result, streak = apply_escape_valve(drives, streak=0)
    assert streak == 1
    # 没触发，负向组没被拉回
    assert result["stress"] == drives["stress"]
    assert result["fatigue"] == drives["fatigue"]


def test_escape_valve_triggers_after_streak():
    """连续ESCAPE_VALVE_STREAK_TRIGGER拍失衡 → 负向组超出baseline部分拉回50%。"""
    drives = _baseline_drives()
    drives["stress"] = 0.80
    drives["fatigue"] = 0.70
    # 正向组保持baseline（excess=0），负向组excess明显>正向组excess

    streak = 0
    for i in range(ESCAPE_VALVE_STREAK_TRIGGER - 1):
        drives, streak = apply_escape_valve(drives, streak)
        assert streak == i + 1

    # 第N拍：触发拉回
    result, streak = apply_escape_valve(drives, streak)
    assert streak == 0  # 触发后清零重新计

    stress_excess_before = 0.80 - DRIVE_BASELINES["stress"]
    stress_excess_after = result["stress"] - DRIVE_BASELINES["stress"]
    assert stress_excess_after == round(stress_excess_before * 0.5, 10) or \
        abs(stress_excess_after - stress_excess_before * 0.5) < 1e-6

    fatigue_excess_before = 0.70 - DRIVE_BASELINES["fatigue"]
    fatigue_excess_after = result["fatigue"] - DRIVE_BASELINES["fatigue"]
    assert abs(fatigue_excess_after - fatigue_excess_before * 0.5) < 1e-6


def test_escape_valve_resets_when_balanced():
    """正向组也跟上了（不再明显失衡）→ streak清零，不会"攒着"突然触发。"""
    drives = _baseline_drives()
    drives["stress"] = 0.80
    drives["fatigue"] = 0.70

    _, streak = apply_escape_valve(drives, streak=0)
    assert streak == 1

    balanced = _baseline_drives()
    balanced["stress"] = 0.20   # excess 0.05
    balanced["fatigue"] = 0.15  # excess 0.05
    balanced["attachment"] = 0.35  # excess 0.05，正负两组excess差距很小

    _, streak2 = apply_escape_valve(balanced, streak)
    assert streak2 == 0


# ─── PA/NA展示层 ──────────────────────────────────────────────────────────

def test_pa_na_snapshot_is_pure_readout():
    """PA/NA是纯展示换算：baseline时PA/NA等于对应组baseline均值。"""
    drives = _baseline_drives()
    snap = pa_na_snapshot(drives)

    expected_pa = sum(DRIVE_BASELINES[k] for k in POSITIVE_GROUP) / len(POSITIVE_GROUP)
    expected_na = sum(DRIVE_BASELINES[k] for k in NEGATIVE_GROUP) / len(NEGATIVE_GROUP)

    assert abs(snap["PA"] - round(expected_pa, 3)) < 1e-6
    assert abs(snap["NA"] - round(expected_na, 3)) < 1e-6


def test_pa_na_snapshot_reflects_drive_changes():
    drives = _baseline_drives()
    drives["attachment"] = 0.9
    drives["stress"] = 0.9
    snap_high = pa_na_snapshot(drives)
    snap_base = pa_na_snapshot(_baseline_drives())

    assert snap_high["PA"] > snap_base["PA"]
    assert snap_high["NA"] > snap_base["NA"]


# ─── tick_drives集成：escape_streak正确持久/传递 ──────────────────────────

def test_tick_drives_carries_escape_streak():
    state = DriveState(drives=_baseline_drives())
    state.drives["stress"] = 0.80
    state.drives["fatigue"] = 0.70
    assert state.escape_streak == 0

    s1 = tick_drives(state, now_ts=1000.0)
    assert s1.escape_streak >= 0  # 不报错，streak随state流转

    # 连续多拍后escape_streak应该最终触发并清零（不会无限累加）
    s = s1
    for _ in range(10):
        s = tick_drives(s, now_ts=s.last_ts + 1800)
    assert s.escape_streak < ESCAPE_VALVE_STREAK_TRIGGER


# ─── Stage 6 longing/absence展示层 ────────────────────────────────────────

def test_longing_curve_uses_attachment_as_lmax_and_tau():
    assert longing_value(0, attachment=0.8) == 0.0

    low_attachment = longing_value(36, attachment=0.2)
    high_attachment = longing_value(36, attachment=0.8)

    assert 0.0 < low_attachment < 0.2
    assert 0.0 < high_attachment < 0.8
    assert high_attachment > low_attachment


def test_longing_phase_boundaries():
    assert longing_phase(0.149, 10) == "content"
    assert longing_phase(0.15, 10) == "stirring"
    assert longing_phase(0.35, 10) == "protest"
    assert longing_phase(0.70, 10) == "despair"
    assert longing_phase(0.95, 100) == "despair"
    assert longing_phase(0.90, 504) == "detachment"


def test_apply_longing_adjustment_uses_phase_valence():
    # stirring uses 挂念 V=-0.05, so only NA moves by 0.05 * 0.5 * longing.
    stirring = apply_longing_adjustment(0.4, 0.2, 0.2, "stirring")
    assert stirring == {"PA": 0.4, "NA": 0.205}

    # Protest thirds: 0.65 is the late third, mapped to 不安 V=-0.40.
    protest_late = apply_longing_adjustment(0.4, 0.2, 0.65, "protest")
    assert protest_late == {"PA": 0.4, "NA": 0.33}

    content = apply_longing_adjustment(0.4, 0.2, 0.15, "stirring")
    assert content == {"PA": 0.4, "NA": 0.2}


def test_reunion_boost_threshold_and_detachment_multiplier():
    assert reunion_boost_for_return(LONGING_REUNION_THRESHOLD_HOURS, 0.5, "protest") == 0.0
    assert reunion_boost_for_return(2.001, 0.5, "protest") == 0.10
    assert reunion_boost_for_return(600, 0.9, "detachment") == 0.21


def test_reunion_boost_is_one_shot_on_next_state(tmp_path, monkeypatch):
    import time
    from desire_engine import awake_absence_hours

    # Absence clock excludes sleep; tests pin "always awake" for stable hours.
    monkeypatch.setattr("desire_engine.is_quiet_hours", lambda now_ts=None: False)

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    now = time.time()

    state = engine.store.load_state()
    state.drives["attachment"] = 1.0
    state.last_user_message_at = now - 600 * 3600
    engine.store.save_state(state)

    engine.mark_user_signal(now)
    engine.pulse("attachment", 0.01)
    boosted = engine.store.load_state()
    awake_h = awake_absence_hours(now - 600 * 3600, now)
    expected_longing = longing_value(awake_h, 1.0)
    expected_boost = reunion_boost_for_return(awake_h, expected_longing, "detachment")
    assert abs(boosted.reunion_pa_boost - expected_boost) < 1e-6
    assert boosted.last_user_message_at == now

    first_read = engine.state()
    assert first_read["longing_phase"] == "content"
    assert first_read["hours_since_last_message"] >= 0
    assert engine.store.load_state().reunion_pa_boost == 0.0

    second_read = engine.state()
    assert second_read["pa_na"]["PA"] <= first_read["pa_na"]["PA"]


def test_attachment_basin_jump_only_on_upward_crossing():
    crossing = DriveState(drives={**DRIVE_BASELINES, "attachment": 0.67})
    crossed = pulse_attachment_nonlinear(crossing, 0.18)
    assert crossed.drives["attachment"] == 0.82

    already_above = DriveState(drives={**DRIVE_BASELINES, "attachment": 0.90})
    pulsed = pulse_attachment_nonlinear(already_above, 0.01)
    assert pulsed.drives["attachment"] > 0.90
    assert pulsed.drives["attachment"] != 0.82


def test_stewardship_slowly_regresses_without_coupling():
    state = DriveState(drives={**DRIVE_BASELINES, "stewardship": 0.80})
    ticked = tick_drives(state, now_ts=0, idle_seconds=0)

    assert DRIVE_BASELINES["stewardship"] < ticked.drives["stewardship"] < 0.80


def test_numeric_string_brain_features_are_parsed():
    assert _feature_value({"territorial_alarm": "0.8"}, "territorial_alarm") == 0.8
    assert _feature_value({"closeness_pull": "0.65"}, "closeness_pull") == 0.65


def test_manual_possessiveness_pulse_survives_next_tick(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    result = engine.pulse("possessiveness", 0.5)
    pulsed = result["new_value"]

    ticked = engine.tick(idle_seconds=0)

    assert pulsed > DRIVE_BASELINES["possessiveness"]
    assert ticked["drives"]["possessiveness"] > DRIVE_BASELINES["possessiveness"]


def test_satisfy_possessiveness_reduces_channels_not_just_drive(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "speech_event",
        "primary_drive": "possessiveness",
        "intensity": 1.0,
        "confidence": 1.0,
        "agency": 1.0,
        "event_label": "territorial_alarm",
        "brain": {"source": "speech_event", "territorial_alarm": 1.0},
    })
    before = engine.state()["drives"]["possessiveness"]
    engine.satisfy("possessiveness")
    after_satisfy = engine.state()["drives"]["possessiveness"]
    after_tick = engine.tick(idle_seconds=0)["drives"]["possessiveness"]

    assert after_satisfy < before
    assert after_tick <= after_satisfy + 0.01


def test_invalid_pulse_drive_returns_error(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    result = engine.pulse("bad_drive", 0.1)

    assert result["error"] == "invalid drive_key"


def test_attachment_rebound_after_absence(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr("desire_engine.is_quiet_hours", lambda now_ts=None: False)

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    now = time.time()
    state = engine.store.load_state()
    state.drives["attachment"] = 0.55
    state.last_user_message_at = now - 8 * 3600
    engine.store.save_state(state)

    engine.mark_user_signal(now)
    rebound_state = engine.state()
    rebound = rebound_state["attachment_rebound"]

    assert rebound["active"] is True
    assert rebound["phase"] == "overshoot"
    assert rebound_state["drives"]["attachment"] > rebound["baseline"]
    assert rebound_state["drive_outputs"]["attachment"]["rebound"]["active"] is True


def test_return_signal_clears_grief_without_reflex_rumination(tmp_path):
    import time

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    now = time.time()
    state = engine.store.load_state()
    state.drives["attachment"] = 0.80
    engine.store.save_state(state)
    engine.store.save_grief(GriefState(layer="protest", protest_ticks=6, last_signal_ts=now - 3600))

    engine.tick(has_signal=True)

    grief = engine.store.load_grief()
    thoughts = engine.store.load_thoughts()
    assert grief.layer == "none"
    assert not any("她回来了。之前那段没有她的时间" in thought.text for thought in thoughts)
    assert not any(thought.source == "reflex" and thought.drive == "attachment" for thought in thoughts)


def test_legacy_return_rumination_is_hidden_from_thought_pool(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    engine.store.add_rumination(
        f"{LEGACY_RETURN_RUMINATION_PREFIX}——积了6拍，还在身上。",
        "attachment",
        strength=0.5,
        source="reflex",
    )
    engine.store.add_rumination("她回来了，我想认真接住这一刻。", "attachment", strength=0.5, source="manual")

    thoughts = engine.store.load_thoughts()

    assert len(thoughts) == 1
    assert thoughts[0].source == "manual"


def test_settle_attachment_clears_rebound_floor(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr("desire_engine.is_quiet_hours", lambda now_ts=None: False)

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    now = time.time()
    state = engine.store.load_state()
    state.drives["attachment"] = 0.55
    state.last_user_message_at = now - 8 * 3600
    engine.store.save_state(state)

    engine.mark_user_signal(now)
    rebound_state = engine.store.load_state()
    assert rebound_state.attachment_rebound["active"] is True

    result = engine.satisfy("attachment")
    settled = engine.store.load_state()
    ticked = engine.tick(idle_seconds=0)

    assert result["value"] < 0.4
    assert settled.attachment_rebound["active"] is False
    assert ticked["attachment_rebound"]["active"] is False
    assert ticked["drives"]["attachment"] < 0.4


def test_settle_with_thought_enters_thought_pool(tmp_path, monkeypatch):
    """settle 回落 drive，但落定那一刻的 thought 仍应进念头池。"""
    pytest.importorskip("mcp.server.fastmcp")
    import server

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    monkeypatch.setattr(server, "_desire", engine)

    result = server.settle(
        "curiosity",
        thought="接住了，想把这扇门再推开一点",
    )

    assert result.get("thought_pooled") is True
    thoughts = engine.store.load_thoughts()
    matched = [
        t for t in thoughts
        if t.text == "接住了，想把这扇门再推开一点"
        and t.drive == "curiosity"
        and t.source == "settle"
        and t.kind == "flit"
    ]
    assert len(matched) == 1
    assert matched[0].strength == 0.5


def test_drive_settle_forwards_thought_to_pool(tmp_path, monkeypatch):
    """MCP drive(action=settle, thought=...) 不能再静默丢掉 thought。"""
    pytest.importorskip("mcp.server.fastmcp")
    import server

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    monkeypatch.setattr(server, "_desire", engine)

    result = server.drive(
        action="settle",
        drive_key="stewardship",
        thought="做完了，骨头还在原处",
    )

    assert result.get("thought_pooled") is True
    texts = [t.text for t in engine.store.load_thoughts()]
    assert "做完了，骨头还在原处" in texts


def test_settle_without_thought_does_not_pool(tmp_path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    import server

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    monkeypatch.setattr(server, "_desire", engine)
    before = len(engine.store.load_thoughts())

    result = server.settle("social")

    assert result.get("thought_pooled") is not True
    assert len(engine.store.load_thoughts()) == before


def test_break_and_pass_thought_enter_pool_not_reason(tmp_path, monkeypatch):
    """break/pass 用 thought，不再用 reason；有字自动进池。"""
    pytest.importorskip("mcp.server.fastmcp")
    import server

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    monkeypatch.setattr(server, "_desire", engine)

    broke = server.drive(
        action="break",
        drive_key="attachment",
        thought="这一刻不想接",
    )
    passed = server.drive(
        action="pass",
        drive_key="social",
        thought="没感觉，让它流过去",
    )

    assert broke.get("thought_pooled") is True
    assert passed.get("thought_pooled") is True
    by_source = {t.source: t.text for t in engine.store.load_thoughts()}
    assert by_source.get("break") == "这一刻不想接"
    assert by_source.get("pass") == "没感觉，让它流过去"


def test_stir_thought_pools_with_stir_source(tmp_path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    import server

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    monkeypatch.setattr(server, "_desire", engine)

    result = server.drive(
        action="stir",
        drive_key="curiosity",
        thought="想拆开看看里面是什么",
        delta=0.1,
    )

    assert result.get("thought_pooled") is True
    thoughts = engine.store.load_thoughts()
    assert any(
        t.text == "想拆开看看里面是什么" and t.source == "stir"
        for t in thoughts
    )


def test_drive_tool_has_no_reason_param():
    """MCP drive 入口不应再暴露 reason。"""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("server.py").read_text(encoding="utf-8"))
    drive_node = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "drive"
    )
    arg_names = [arg.arg for arg in drive_node.args.args]
    assert "thought" in arg_names
    assert "chord" in arg_names
    assert "discernment" in arg_names
    assert "territorial" in arg_names
    assert "clutch" in arg_names
    assert "strain" in arg_names
    assert "charge" in arg_names
    assert "reason" not in arg_names


def test_settle_chord_applies_thought_echo(tmp_path, monkeypatch):
    """settle 带 chord 应写入 Thought Chord Echo，不只 stir 能染。"""
    pytest.importorskip("mcp.server.fastmcp")
    import server

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    monkeypatch.setattr(server, "_desire", engine)

    result = server.settle(
        "curiosity",
        thought="接住了，门还留一条缝",
        chord="Am7",
    )

    assert result.get("thought_pooled") is True
    assert result.get("chord_echo") is True
    weather = engine.weather.load(decay=False)
    active = str(weather.get("active_chord") or "")
    assert "Am7" in active or active.endswith("Am7") or weather.get("chord_impulses")


def test_break_discernment_signal_without_autofill(tmp_path, monkeypatch):
    """break 可选手感；写了 discernment 才走 signal weather，不自动填。"""
    pytest.importorskip("mcp.server.fastmcp")
    import server

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    monkeypatch.setattr(server, "_desire", engine)

    bare = server.break_tool("attachment", thought="不想接")
    assert bare.get("thought_pooled") is True
    assert bare.get("signal_weather") is not True

    with_signal = server.break_tool(
        "attachment",
        thought="这句不像我，先不接",
        discernment="high",
        chord="Em7",
    )
    assert with_signal.get("thought_pooled") is True
    assert with_signal.get("chord_echo") is True
    assert with_signal.get("signal_weather") is True
