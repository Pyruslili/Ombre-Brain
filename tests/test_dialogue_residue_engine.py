from dialogue_residue_engine import normalize_dialogue_messages, normalize_dialogue_residue_event


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
