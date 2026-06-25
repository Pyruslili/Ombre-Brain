from desire_engine import (
    DRIVE_KEYS,
    DesireEngine,
    _legacy_brain_to_event,
    normalize_drive_key,
)


def test_drive_aliases_fold_to_v2_keys():
    assert "stewardship" in DRIVE_KEYS
    assert "discernment" in DRIVE_KEYS
    assert "possessiveness" in DRIVE_KEYS
    assert normalize_drive_key("duty") == "stewardship"
    assert normalize_drive_key("disgust") == "discernment"


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


def test_legacy_brain_signals_fold_to_single_event():
    event = _legacy_brain_to_event(
        {"盆地": "吃醋", "地基感": "悬", "二级分支": "嫉妒", "脑岛": "夸了别人"},
        {"attachment": 0.3},
    )
    assert event["schema_version"] == "drive_event_v2"
    assert event["primary_drive"] == "possessiveness"
    assert event["brain"]["grounding"] == "悬"
    assert event["brain"]["memory_resonance"] == "嫉妒"


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

    assert result == ("", "")
    assert not (tmp_path / "live_wire_cache.json").exists()
