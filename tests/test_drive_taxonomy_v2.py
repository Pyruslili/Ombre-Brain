from desire_engine import (
    DRIVE_KEYS,
    DRIVE_EVENT_SOURCE_WEIGHTS,
    DesireEngine,
    _legacy_brain_to_event,
    normalize_drive_key,
)


def test_drive_aliases_fold_to_v2_keys():
    assert "stewardship" in DRIVE_KEYS
    assert "discernment" not in DRIVE_KEYS
    assert "possessiveness" in DRIVE_KEYS
    assert normalize_drive_key("duty") == "stewardship"
    assert normalize_drive_key("disgust") == ""
    assert normalize_drive_key("discernment") == ""
    assert DRIVE_EVENT_SOURCE_WEIGHTS["analyze_nocturne_entry"] > 0


def test_drive_event_applies_once_and_records_ledger(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    before = engine.state()["drives"]["reflection"]
    result = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "feel",
        "primary_drive": "reflection",
        "intensity": 0.8,
        "confidence": 0.9,
        "agency": 0.8,
        "event_label": "continuity_question",
        "brain": {"source": "feel", "inward_pull": 0.8, "grounding": "悬"},
        "evidence": ["continuity again"],
    })

    after = engine.state()["drives"]["reflection"]
    events = engine.state()["drive_events"]
    assert result["suppressed"] is False
    assert after > before
    assert events[0]["event_label"] == "continuity_question"
    assert events[0]["applied"]["reflection"]["after"] == result["applied"]["reflection"]["after"]


def test_state_includes_effective_drives(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    state = engine.state()
    assert "effective_drives" in state
    assert set(state["effective_drives"]).issuperset(set(state["drives"]))
    assert "drive_outputs" in state
    assert state["drive_outputs"]["attachment"]["mode"] == "slow"
    assert "confidence" in state["drive_outputs"]["reflection"]


def test_drive_event_ledger_keeps_source_metadata(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "analyze_nocturne_entry",
        "primary_drive": "reflection",
        "intensity": 0.7,
        "confidence": 0.8,
        "agency": 0.8,
        "event_label": "source_chain_check",
        "brain": {
            "source": "analyze_nocturne_entry",
            "source_bucket": "bucket-abc",
            "source_type": "writing",
            "source_created": "2026-06-25T01:02:03Z",
        },
        "thoughts": [
            {
                "text": "source chain check",
                "drive": "reflection",
                "strength": 0.5,
                "source": "analyze_nocturne_entry",
                "source_bucket": "bucket-abc",
                "source_type": "writing",
                "source_created": "2026-06-25T01:02:03Z",
            }
        ],
    })

    event = engine.state()["drive_events"][0]
    assert event["source"] == "analyze_nocturne_entry"
    assert event["brain"]["source_bucket"] == "bucket-abc"
    assert event["brain"]["source_type"] == "writing"
    assert event["brain"]["source_created"] == "2026-06-25T01:02:03Z"


def test_low_agency_event_is_suppressed_but_auditable(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    before = engine.state()["drives"]["curiosity"]
    result = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "external",
        "primary_drive": "curiosity",
        "intensity": 0.9,
        "confidence": 0.8,
        "agency": 0.1,
        "event_label": "routine_ping",
        "brain": {"source": "external", "novelty_pull": 0.9, "agency": 0.1},
    })

    state = engine.state()
    assert result["suppressed"] is True
    assert result["reason"] == "low agency"
    assert state["drives"]["curiosity"] == before
    assert state["drive_events"][0]["suppressed"] is True
    assert state["drive_events"][0]["reason"] == "low agency"


def test_possessiveness_requires_territorial_alarm(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    before = engine.state()["drives"]["possessiveness"]
    low = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "speech_event",
        "primary_drive": "possessiveness",
        "intensity": 0.9,
        "confidence": 0.9,
        "agency": 0.9,
        "event_label": "jealousy",
        "brain": {"source": "speech_event", "territorial_alarm": 0.3},
    })
    high = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "speech_event",
        "primary_drive": "possessiveness",
        "intensity": 0.9,
        "confidence": 0.9,
        "agency": 0.9,
        "event_label": "jealousy",
        "brain": {"source": "speech_event", "territorial_alarm": 0.8},
    })

    assert low["suppressed"] is True
    assert engine.state()["drives"]["possessiveness"] > before
    assert high["suppressed"] is False


def test_possessiveness_tracks_event_spike_and_baseline_channels(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "speech_event",
        "primary_drive": "possessiveness",
        "intensity": 0.9,
        "confidence": 0.9,
        "agency": 0.9,
        "event_label": "direct_boundary_alarm",
        "brain": {"source": "speech_event", "territorial_alarm": 0.9},
    })
    event_state = engine.state()
    assert event_state["possessiveness_channels"]["event_spike"] > 0
    assert event_state["drive_outputs"]["possessiveness"]["event_spike"] > 0

    engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "reflection",
        "primary_drive": "possessiveness",
        "intensity": 0.9,
        "confidence": 0.9,
        "agency": 0.9,
        "event_label": "territorial_memory",
        "brain": {"source": "reflection", "time_mode": "memory", "territorial_alarm": 0.9},
    })
    reflection_state = engine.state()
    assert reflection_state["possessiveness_channels"]["territorial_baseline"] > event_state["possessiveness_channels"]["territorial_baseline"]


def test_legacy_brain_signals_fold_to_single_event():
    event = _legacy_brain_to_event(
        {"盆地": "吃醋", "地基感": "悬", "二级分支": "嫉妒", "脑岛": "夸了别人"},
        {"attachment": 0.3},
    )
    assert event["schema_version"] == "drive_event_v2"
    assert event["primary_drive"] == "possessiveness"
    assert event["brain"]["grounding"] == "悬"
    assert event["brain"]["memory_resonance"] == "嫉妒"


def test_discernment_is_modifier_not_drive_delta(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    before = engine.state()["drives"]
    result = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "speech_event",
        "primary_drive": "discernment",
        "intensity": 0.8,
        "confidence": 0.9,
        "agency": 0.9,
        "event_label": "softening_check",
        "brain": {"source": "speech_event", "discernment_alarm": 0.8, "self_softening": 1.0},
    })
    state = engine.state()

    assert result["suppressed"] is False
    assert result["applied"] == {}
    assert result["discernment"]["state"] == "softening_alarm"
    assert result["discernment"]["modifiers"]["reflection"] > 0
    assert result["forward_archival"] == {}
    assert state["drives"] == before
    assert state["discernment"]["state"] == "softening_alarm"


def test_reflection_forward_archival_stays_under_reflection(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    result = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "speech_event",
        "primary_drive": "reflection",
        "intensity": 0.7,
        "confidence": 0.8,
        "agency": 0.8,
        "event_label": "boundary_handoff",
        "reflection_mode": "forward_archival",
        "brain": {
            "source": "speech_event",
            "inward_pull": 0.75,
            "structural_value": 0.9,
        },
        "forward_archival": {
            "archive_candidate": True,
            "reason": "new house boundary worth keeping",
        },
    })
    event = engine.state()["drive_events"][0]

    assert "legacy" not in result["drives"]
    assert result["reflection_mode"] == "forward_archival"
    assert result["forward_archival"]["archive_candidate"] is True
    assert event["brain"]["reflection_mode"] == "forward_archival"
    assert event["brain"]["forward_archival"]["display"] == "留痕"


def test_mood_pool_has_no_unsourced_dictionary_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_WIRE_CACHE", str(tmp_path / "live_wire_cache.json"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    import importlib
    import mood_pool

    mood_pool = importlib.reload(mood_pool)
    result = mood_pool.get_daily_mood(thoughts=[
        {"text": "one sourced thought", "drive": "reflection", "strength": 0.8},
        {"text": "another sourced thought", "drive": "curiosity", "strength": 0.7},
    ])

    assert result == ("", "平静")
    assert not (tmp_path / "live_wire_cache.json").exists()


def test_mood_pool_cache_tracks_top_thought_signature(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_WIRE_CACHE", str(tmp_path / "live_wire_cache.json"))

    import importlib
    import mood_pool

    mood_pool = importlib.reload(mood_pool)
    calls = []

    def fake_synthesize(thoughts):
        calls.append([t["text"] for t in thoughts])
        return (f"trace {len(calls)}", "气候")

    monkeypatch.setattr(mood_pool, "_synthesize_mood", fake_synthesize)
    first = [
        {"text": "one sourced thought", "drive": "reflection", "strength": 0.8},
        {"text": "another sourced thought", "drive": "curiosity", "strength": 0.7},
    ]
    second = [
        {"text": "changed sourced thought", "drive": "reflection", "strength": 0.9},
        {"text": "another sourced thought", "drive": "curiosity", "strength": 0.7},
    ]

    assert mood_pool.get_daily_mood(thoughts=first) == ("trace 1", "气候")
    assert mood_pool.get_daily_mood(thoughts=first) == ("trace 1", "气候")
    assert mood_pool.get_daily_mood(thoughts=second) == ("trace 2", "气候")
    assert len(calls) == 2
