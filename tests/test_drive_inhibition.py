# ============================================================
# Test: ESM软互抑 + 逃逸阀 + PA/NA展示层（P3）
# 作用在已有9维drive上，不另起PA/NA持久状态。
# ============================================================

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
)


def _baseline_drives():
    return dict(DRIVE_BASELINES)


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


def test_reunion_boost_is_one_shot_on_next_state(tmp_path):
    import time

    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    now = time.time()

    state = engine.store.load_state()
    state.drives["attachment"] = 1.0
    state.last_user_message_at = now - 600 * 3600
    engine.store.save_state(state)

    engine.mark_user_signal(now)
    engine.pulse("attachment", 0.01)
    boosted = engine.store.load_state()
    expected_longing = longing_value(600, 1.0)
    expected_boost = reunion_boost_for_return(600, expected_longing, "detachment")
    assert abs(boosted.reunion_pa_boost - expected_boost) < 1e-6
    assert boosted.last_user_message_at == now

    first_read = engine.state()
    assert first_read["longing_phase"] == "content"
    assert first_read["hours_since_last_message"] >= 0
    assert engine.store.load_state().reunion_pa_boost == 0.0

    second_read = engine.state()
    assert second_read["pa_na"]["PA"] <= first_read["pa_na"]["PA"]


def test_attachment_rebound_after_absence(tmp_path):
    import time

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
