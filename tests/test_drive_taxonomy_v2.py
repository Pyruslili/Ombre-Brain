from desire_engine import (
    DRIVE_KEYS,
    DRIVE_EVENT_SOURCE_WEIGHTS,
    DesireEngine,
    _legacy_brain_to_event,
    normalize_drive_event_brain,
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


def test_satisfy_returns_compact_ack(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    result = engine.satisfy("attachment")

    assert result["satisfied"] == "attachment"
    assert set(result) == {"satisfied", "value", "delta", "refractory"}
    assert result["delta"] < 0
    assert result["refractory"] is True


def test_dialogue_residue_attachment_is_softened(tmp_path):
    dialogue_engine = DesireEngine(str(tmp_path / "dialogue.db"))
    user_engine = DesireEngine(str(tmp_path / "user.db"))
    event = {
        "schema_version": "drive_event_v2",
        "primary_drive": "attachment",
        "intensity": 0.2,
        "confidence": 0.8,
        "agency": 0.8,
        "event_label": "attachment_signal",
        "brain": {"closeness_pull": 0.18, "grounding": "实"},
    }

    dialogue = dialogue_engine.apply_drive_event({**event, "source": "dialogue_residue"})
    user = user_engine.apply_drive_event({**event, "source": "user_message"})

    assert dialogue["applied"]["attachment"]["raw_delta"] < user["applied"]["attachment"]["raw_delta"] * 0.35


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


def test_drive_event_brain_normalizes_chord_anchor_fields(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    result = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "dialogue_residue",
        "primary_drive": "curiosity",
        "intensity": 0.5,
        "confidence": 0.8,
        "agency": 0.8,
        "event_label": "outward_release",
        "brain": {
            "target": "external",
            "release_pressure": 1.4,
            "novelty_pull": 0.7,
        },
    })

    event = engine.state()["drive_events"][0]
    assert result["suppressed"] is False
    assert event["source"] == "dialogue_residue"
    assert event["brain"]["release_pressure"] == 1.0
    assert event["brain"]["anchor_target"] == "outside"


def test_normalize_drive_event_brain_keeps_explicit_anchor():
    brain = normalize_drive_event_brain({"target": "external", "anchor_target": "boundary"})

    assert brain["anchor_target"] == "boundary"


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
    before_libido = engine.state()["drives"]["libido"]
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
    assert engine.state()["drives"]["libido"] > before_libido
    assert high["suppressed"] is False


def test_house_collaborator_territorial_signal_is_softened(tmp_path):
    external_engine = DesireEngine(str(tmp_path / "external.db"))
    house_engine = DesireEngine(str(tmp_path / "house.db"))
    base_event = {
        "schema_version": "drive_event_v2",
        "source": "dialogue_residue",
        "primary_drive": "possessiveness",
        "intensity": 0.12,
        "confidence": 0.85,
        "agency": 0.50,
        "event_label": "third_party_position",
        "brain": {"source": "dialogue_residue", "territorial_alarm": 0.58, "closeness_pull": 0.18},
    }

    external = external_engine.apply_drive_event(base_event)
    house = house_engine.apply_drive_event({
        **base_event,
        "brain": {
            **base_event["brain"],
            "third_party_context": "house_collaborator",
        },
    })

    external_delta = external["applied"]["possessiveness"]["raw_delta"]
    house_delta = house["applied"]["possessiveness"]["raw_delta"]
    assert external["suppressed"] is False
    assert house["suppressed"] is False
    assert house_delta < external_delta * 0.55


def test_dialogue_negative_event_tints_weather(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    engine.apply_weather_delta(warmth_delta=0.08, source="feel")

    result = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "dialogue_residue",
        "primary_drive": "stress",
        "intensity": 0.9,
        "confidence": 0.9,
        "agency": 0.9,
        "event_label": "dialogue_shadow",
        "brain": {"source": "dialogue_residue", "tension_load": 0.85, "grounding": "悬"},
    })

    weather = engine.weather_state()
    assert result["weather"]["shadow_residue"] > 0
    assert result["weather"]["warmth_residue"] < 0.08
    assert weather["shadow_residue"] > 0


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


def test_mood_pool_no_longer_falls_back_to_climate_word(monkeypatch, tmp_path):
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
