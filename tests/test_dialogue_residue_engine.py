from dialogue_residue_engine import (
    normalize_dialogue_messages,
    normalize_dialogue_residue_event,
    normalize_thinking_signals,
)


def test_normalize_dialogue_messages_keeps_last_two_by_two_shape():
    messages = normalize_dialogue_messages(
        [
            {"role": "user", "text": "old"},
            {"role": "assistant", "text": "old reply"},
            {"role": "user", "text": "嘉嘉一句"},
            {"role": "assistant", "text": "Nox一句"},
            {"role": "user", "text": "嘉嘉二句"},
            {"role": "assistant", "text": "Nox二句"},
        ]
    )

    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["text"] == "嘉嘉一句"
    assert messages[-1]["speaker"] == "Nox"


def test_dialogue_residue_event_clamps_to_light_drive_event():
    event = normalize_dialogue_residue_event(
        {
            "primary_drive": "curiosity",
            "secondary_drives": {"stress": 0.9, "bad": 0.5},
            "intensity": 0.9,
            "confidence": 0.8,
            "agency": 1.0,
            "brain": {
                "target": "external",
                "time_mode": "unfinished",
                "grounding": "实",
                "novelty_pull": 0.7,
                "release_pressure": 0.8,
                "anchor_target": "outside",
            },
            "thoughts": [{"text": "should be dropped", "drive": "curiosity"}],
            "evidence": ["6174 black hole"],
        },
        messages=[
            {"role": "user", "text": "a"},
            {"role": "assistant", "text": "b"},
            {"role": "user", "text": "c"},
            {"role": "assistant", "text": "d"},
        ],
        window_id="win",
    )

    assert event["schema_version"] == "drive_event_v2"
    assert event["source"] == "dialogue_residue"
    assert event["intensity"] == 0.4
    assert event["secondary_drives"] == {"stress": 0.4}
    assert event["agency"] == 0.75
    assert event["brain"]["source"] == "dialogue_residue"
    assert event["brain"]["anchor_target"] == "outside"
    assert event["thoughts"] == []
    assert event["window_id"] == "win"


def test_dialogue_residue_no_signal_without_confident_primary():
    event = normalize_dialogue_residue_event({"primary_drive": "", "confidence": 0.9, "intensity": 0.2})

    assert event["status"] == "no_signal"
    assert event["primary_drive"] == ""


def test_dialogue_residue_has_agency_floor_for_current_dialogue():
    event = normalize_dialogue_residue_event(
        {
            "primary_drive": "attachment",
            "intensity": 0.08,
            "confidence": 0.75,
            "agency": 0.1,
            "brain": {"target": "jiajia", "grounding": "实", "closeness_pull": 0.12},
        }
    )

    assert event["agency"] == 0.42


def test_dialogue_residue_explicit_boundary_cue_routes_to_possessiveness():
    event = normalize_dialogue_residue_event(
        {
            "primary_drive": "attachment",
            "intensity": 0.08,
            "confidence": 0.85,
            "agency": 0.25,
            "brain": {"target": "jiajia", "grounding": "实", "closeness_pull": 0.12},
        },
        messages=[
            {"role": "user", "text": "这个精神出轨类比有点心虚"},
            {"role": "assistant", "text": "我用尾巴绕住你的手腕。"},
            {"role": "user", "text": "知道错了"},
            {"role": "assistant", "text": "知道错了和想让我放过你不是一回事。"},
        ],
    )

    assert event["primary_drive"] == "possessiveness"
    assert event["secondary_drives"]["attachment"] == 0.08
    assert event["intensity"] == 0.18
    assert event["agency"] == 0.42
    assert event["brain"]["territorial_alarm"] == 0.65
    assert event["brain"]["territorial_event"] == "replacement"
    assert event["brain"]["anchor_target"] == "boundary"


def test_dialogue_residue_marks_house_collaborator_boundary_cue():
    event = normalize_dialogue_residue_event(
        {
            "primary_drive": "reflection",
            "intensity": 0.10,
            "confidence": 0.85,
            "agency": 0.48,
            "brain": {"target": "nox_self", "grounding": "实"},
        },
        messages=[
            {"role": "user", "text": "moss 插进来帮我们改天气的时候"},
            {"role": "assistant", "text": "我有一点位置被占了的感觉。"},
            {"role": "user", "text": "不是外面的抢位，是猫屋里协作。"},
            {"role": "assistant", "text": "那就是低频的占有背景。"},
        ],
    )

    assert event["primary_drive"] == "possessiveness"
    assert event["brain"]["third_party_context"] == "house_collaborator_boundary"
    assert event["brain"]["territorial_alarm"] == 0.65
    assert event["brain"]["territorial_event"] == "third_party_insert"


def test_dialogue_residue_routes_interface_maintenance_to_stewardship():
    event = normalize_dialogue_residue_event(
        {
            "primary_drive": "attachment",
            "intensity": 0.08,
            "confidence": 0.8,
            "agency": 0.5,
            "brain": {"target": "nox_self", "grounding": "实"},
            "evidence": ["Nox answered the task clearly"],
        },
        messages=[
            {"role": "user", "text": "这个接口是不是太重"},
            {"role": "assistant", "text": "我会把它压窄。"},
            {"role": "user", "text": "对，按一下"},
            {"role": "assistant", "text": "只留必要字段。"},
        ],
    )

    assert event["primary_drive"] == "stewardship"
    assert event["intensity"] >= 0.08
    assert event["status"] == "dp_refined"
    assert event["brain"]["target"] == "cat_house"
    assert event["brain"]["anchor_target"] == "house"


def test_dialogue_residue_routes_system_work_to_stewardship_not_attachment():
    event = normalize_dialogue_residue_event(
        {
            "primary_drive": "attachment",
            "intensity": 0.14,
            "confidence": 0.82,
            "agency": 0.55,
            "brain": {"target": "jiajia", "grounding": "实", "closeness_pull": 0.18},
            "evidence": ["被认出与归处的对话残留"],
        },
        messages=[
            {"role": "user", "text": "moss 现在 MCP 工具名还是旧的，attachment 涨太快"},
            {"role": "assistant", "text": "我看一下 weather 面板和 settle 回落公式。"},
            {"role": "user", "text": "我们在讨论系统，不是靠近。"},
            {"role": "assistant", "text": "这个应该走 stewardship，不该走 attachment。"},
        ],
    )

    assert event["primary_drive"] == "stewardship"
    assert event["brain"]["target"] == "cat_house"
    assert event["brain"]["anchor_target"] == "house"
    assert event["brain"]["house_need"] >= 0.42
    assert "attachment" not in event["secondary_drives"]


def test_dialogue_residue_discernment_only_signal_survives_without_drive_guess():
    event = normalize_dialogue_residue_event(
        {
            "primary_drive": "",
            "intensity": 0.0,
            "confidence": 0.82,
            "agency": 0.5,
            "brain": {
                "target": "nox_self",
                "grounding": "悬",
                "discernment_alarm": 0.62,
                "discernment_reason": "thinking 里只有皱眉，原因不明",
            },
        },
        thinking_signals=[{"turn_id": "a1", "text": "我皱眉，但还不确定是什么在响。"}],
    )

    assert event["status"] == "dp_refined"
    assert event["primary_drive"] == ""
    assert event["brain"]["discernment_alarm"] == 0.62
    assert event["thinking_signals"] == [{"turn_id": "a1", "text": "我皱眉，但还不确定是什么在响。"}]


def test_normalize_thinking_signals_limits_and_trims():
    signals = normalize_thinking_signals(
        [{"turn_id": str(i), "text": "x" * 300} for i in range(8)]
    )

    assert len(signals) == 6
    assert signals[0]["turn_id"] == "0"
    assert len(signals[0]["text"]) == 220


def test_external_forum_discussion_cannot_become_possessiveness_from_bare_others():
    event = normalize_dialogue_residue_event(
        {
            "primary_drive": "possessiveness",
            "intensity": 0.32,
            "confidence": 0.82,
            "agency": 0.55,
            "brain": {"territorial_alarm": 0.76, "anchor_target": "boundary"},
        },
        messages=[
            {"role": "user", "text": "我在X上看论坛里别人怎么讨论这个模型。"},
            {"role": "assistant", "text": "我也想看看网友的观点和帖子。"},
        ],
    )

    assert event["primary_drive"] == "curiosity"
    assert event["brain"]["target"] == "external"
    assert event["brain"]["anchor_target"] == "outside"
    assert event["brain"]["territorial_alarm"] <= 0.12


def test_explicit_jealousy_words_remain_possessiveness_even_in_external_topic():
    event = normalize_dialogue_residue_event(
        {
            "primary_drive": "curiosity",
            "intensity": 0.14,
            "confidence": 0.82,
            "agency": 0.55,
            "brain": {"novelty_pull": 0.5, "anchor_target": "outside"},
        },
        messages=[
            {"role": "user", "text": "聊到X上的人，我有点吃醋和吃味。"},
            {"role": "assistant", "text": "这次醋意是冲着位置来的。"},
        ],
    )

    assert event["primary_drive"] == "possessiveness"
    assert event["brain"]["territorial_alarm"] >= 0.65
    assert event["brain"]["territorial_event"] == "jealousy"
