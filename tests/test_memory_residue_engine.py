from memory_residue_engine import normalize_memory_entry, normalize_memory_residue_event


def test_normalize_memory_entry_keeps_weather_hints():
    entry = normalize_memory_entry(
        {
            "bucket_id": "b1",
            "type": "writing",
            "content": "猫屋接口改造",
            "drive_tags": {"stewardship": 0.8},
            "signal_hints": {"strain": "mid"},
        }
    )

    assert entry["id"] == "b1"
    assert entry["type"] == "writing"
    assert entry["drive_tags"] == {"stewardship": 0.8}
    assert entry["signal_hints"] == {"strain": "mid"}


def test_normalize_memory_residue_event_uses_dp_memory_source_and_thought_limit():
    event = normalize_memory_residue_event(
        {
            "source": "analyze_nocturne_entry",
            "primary_drive": "stewardship",
            "secondary_drives": {"reflection": 0.4, "bad": 0.9},
            "intensity": 0.7,
            "confidence": 0.8,
            "agency": 0.6,
            "brain": {"source": "analyze_nocturne_entry", "target": "cat_house"},
            "thoughts": [
                {"text": "我得把这条线接稳。", "drive": "stewardship", "strength": 0.6},
                {"text": "多余念头", "drive": "reflection", "strength": 0.5},
            ],
        },
        {"id": "b2", "type": "memory", "created": "now", "content_preview": "接口"},
    )

    assert event["schema_version"] == "drive_event_v2"
    assert event["source"] == "dp_memory"
    assert event["brain"]["source"] == "dp_memory"
    assert event["source_bucket"] == "b2"
    assert event["secondary_drives"] == {"reflection": 0.4}
    assert len(event["thoughts"]) == 1
    assert event["thoughts"][0]["source"] == "dp_memory"


def test_explicit_memory_drive_and_core_hints_are_authoritative():
    event = normalize_memory_residue_event(
        {
            "primary_drive": "possessiveness",
            "intensity": 0.6,
            "confidence": 0.8,
            "agency": 0.7,
            "brain": {"territorial_alarm": 0.8},
        },
        {
            "id": "b3",
            "type": "feel",
            "content_preview": "想去外面看看",
            "chord": "Dmaj7",
            "drive_tags": {"curiosity": 0.9, "social": 0.4},
            "signal_hints": {"charge": "high", "clutch": "low", "strain": "mid"},
        },
    )

    assert event["primary_drive"] == "curiosity"
    assert event["secondary_drives"]["social"] == 0.4
    assert event["brain"]["release_pressure"] == 0.82
    assert event["brain"]["closeness_pull"] == 0.28
    assert event["brain"]["tension_load"] == 0.55
