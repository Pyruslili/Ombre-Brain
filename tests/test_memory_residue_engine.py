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
