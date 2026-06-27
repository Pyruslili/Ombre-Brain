from desire_engine import (
    DesireEngine,
    DRIVE_BASELINES,
    chord_chemistry_snapshot,
    chord_event_tint_from_drive_events,
    classify_chord_situation,
    current_weather_chord,
    pa_na_snapshot,
)


def test_weather_delta_adds_to_effective_pa_na(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    result = engine.apply_weather_delta(warmth_delta=0.05, source="keyword")
    weather = engine.weather_state()
    base = pa_na_snapshot(DRIVE_BASELINES)

    assert result["warmth_residue"] == 0.05
    assert weather["effective_PA"] == round(base["PA"] + 0.05, 3)
    assert weather["effective_NA"] == round(base["NA"], 3)


def test_weather_delta_can_cool_existing_warmth(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    engine.apply_weather_delta(warmth_delta=0.08, source="feel")
    cooled = engine.apply_weather_delta(warmth_delta=-0.03, shadow_delta=0.02, source="feel")

    assert cooled["warmth_residue"] == 0.05
    assert cooled["shadow_residue"] == 0.02


def test_chord_echo_routes_by_source_and_chord(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    warm = engine.apply_chord_echo("Fmaj7", source="feel")
    shadow = engine.apply_chord_echo("Dm7", source="thought")

    assert warm["kind"] == "warmth"
    assert warm["active_chord"] == "Fmaj7"
    assert warm["active_chord_source"] == "feel"
    assert warm["warmth_residue"] > 0
    assert shadow["kind"] == "shadow"
    assert shadow["active_chord"] in {"Fmaj7", "Dm7"}
    assert shadow["active_chord_source"] in {"feel", "thought"}
    assert shadow["shadow_residue"] > 0


def test_pulse_returns_compact_chord_change_signal(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    result = engine.pulse("reflection", 0.1, chord="Dmaj7")
    repeated = engine.pulse("reflection", 0.1, chord="Dmaj7")

    assert result["drive_key"] == "reflection"
    assert result["new_value"] > DRIVE_BASELINES["reflection"]
    assert "local_fatigue" in result
    assert result["chord_changed"] == "Dmaj7"
    assert "chord_echo" not in result
    assert "source_stack" not in result
    assert "chord_changed" not in repeated


def test_soma_chord_is_short_strong_weather_impulse(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    result = engine.apply_chord_echo("Gmaj7", source="soma")
    weather = engine.weather_state()

    assert result["kind"] == "warmth"
    assert result["warmth_residue"] == 0.08
    assert weather["active_chord"] == "Gmaj7"
    assert weather["active_chord_source"] == "soma"
    assert weather["active_chord_weight"] > 0
    assert weather["source_stack"][0]["source"] == "soma"
    assert weather["current_chord"] == current_weather_chord(
        weather["effective_PA"], weather["effective_NA"]
    )


def test_chord_impulse_active_selection_uses_decayed_weight(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    start = 1000.0

    engine.weather.apply_chord("Dm7", source="thought", now=start)
    state = engine.weather.apply_chord("Gmaj7", source="soma", now=start + 3 * 3600)

    assert state["active_chord"] == "Gmaj7"
    assert state["active_chord_source"] == "soma"

    later = engine.weather.load(now=start + 6 * 3600, decay=True)
    assert later["active_chord"] == "Dm7"
    assert later["active_chord_source"] == "thought"


def test_chord_impulse_below_threshold_does_not_display_active(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    start = 1000.0

    engine.weather.apply_chord("Gmaj7", source="soma", now=start)
    later = engine.weather.load(now=start + 5 * 3600, decay=True)

    assert later["active_chord"] == ""
    assert later["active_chord_source"] == ""


def test_expanded_chord_vocabulary_routes_liminal_and_color(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    lydian = engine.apply_chord_echo("Fmaj7#11", source="thought")
    liminal = engine.apply_chord_echo("Gsus4", source="soma")
    shadow = engine.apply_chord_echo("Bm7b5", source="feel")

    assert lydian["kind"] == "warmth"
    assert liminal["kind"] == "liminal"
    assert shadow["kind"] == "shadow"
    assert liminal["warmth_residue"] > lydian["warmth_residue"]
    assert liminal["shadow_residue"] > 0


def test_current_weather_chord_uses_expanded_regions():
    assert current_weather_chord(0.7, 0.2) == "Dmaj7"
    assert current_weather_chord(0.62, 0.42) == "Fmaj7#11"
    assert current_weather_chord(0.44, 0.4) == "Am7"
    assert current_weather_chord(0.3, 0.66) == "F#dim"
    assert current_weather_chord(0.32, 0.43) == "Gsus4"


def test_chord_chemistry_keeps_vector_as_route():
    drives = {
        **DRIVE_BASELINES,
        "attachment": 0.74,
        "possessiveness": 0.62,
        "stewardship": 0.50,
        "stress": 0.55,
        "curiosity": 0.22,
        "social": 0.18,
        "fatigue": 0.20,
    }

    chemistry = chord_chemistry_snapshot(drives, warmth=0.62, shadow=0.42)

    assert set(chemistry["core"]) == {"charge", "clutch", "strain"}
    assert chemistry["route"]["vector"] in {
        "toward_jiajia",
        "toward_house",
        "outward",
        "inward",
        "guard",
        "hover",
    }
    assert "scores" in chemistry["route"]
    assert chemistry["core"]["clutch"] > chemistry["core"]["charge"]
    assert chemistry["route"]["vector"] == "guard"
    assert chemistry["situation"] == "guard"
    assert chemistry["derived_texture"]["guard"] > 0.55
    assert chemistry["gravity"]


def test_chord_chemistry_uses_interactions_not_plain_drive_aliases():
    warm_attachment = {
        **DRIVE_BASELINES,
        "attachment": 0.74,
        "possessiveness": 0.04,
        "stewardship": 0.16,
        "stress": 0.12,
    }
    anchored_attachment = {
        **warm_attachment,
        "possessiveness": 0.62,
        "stewardship": 0.50,
        "stress": 0.55,
    }

    loose = chord_chemistry_snapshot(warm_attachment, warmth=0.60, shadow=0.22)
    anchored = chord_chemistry_snapshot(anchored_attachment, warmth=0.60, shadow=0.42)

    assert anchored["core"]["clutch"] - loose["core"]["clutch"] > 0.25
    assert anchored["core"]["strain"] > loose["core"]["strain"]
    assert anchored["derived_texture"]["pull"] >= loose["derived_texture"]["pull"]


def test_chord_situation_keeps_pull_from_swallowing_high_strain():
    core = {"charge": 0.42, "clutch": 0.72, "strain": 0.72}
    route = {"vector": "toward_jiajia"}
    derived = {"pull": 0.80, "depth": 0.52, "drift": 0.04}

    assert classify_chord_situation(core, route, derived) == "clamp"


def test_chord_situation_splits_scout_from_spark():
    core = {"charge": 0.72, "clutch": 0.30, "strain": 0.42}
    derived = {"pull": 0.10, "depth": 0.15, "drift": 0.18}

    assert classify_chord_situation(core, {"vector": "outward"}, derived) == "scout"
    assert classify_chord_situation(core, {"vector": "hover"}, derived) == "spark"


def test_chord_gravity_uses_force_line_not_instruction():
    chemistry = chord_chemistry_snapshot(
        {
            **DRIVE_BASELINES,
            "curiosity": 0.80,
            "social": 0.62,
            "fatigue": 0.05,
            "stress": 0.16,
        },
        warmth=0.58,
        shadow=0.18,
        recent_gravity=[],
        now=1000,
    )

    assert chemistry["gravity"]
    assert "句子" not in chemistry["gravity"]
    assert "语速" not in chemistry["gravity"]
    assert "应该" not in chemistry["gravity"]


def test_chord_gravity_stays_stable_until_reaction_changes():
    drives = {
        **DRIVE_BASELINES,
        "curiosity": 0.80,
        "social": 0.62,
        "fatigue": 0.05,
        "stress": 0.16,
    }

    first = chord_chemistry_snapshot(drives, warmth=0.58, shadow=0.18, recent_gravity=[], now=1000)
    later = chord_chemistry_snapshot(drives, warmth=0.58, shadow=0.18, recent_gravity=[], now=9000)

    assert first["situation"] == later["situation"]
    assert first["route"]["vector"] == later["route"]["vector"]
    assert first["gravity"] == later["gravity"]


def test_drive_event_brain_tints_chord_chemistry_without_changing_chord():
    drives = {**DRIVE_BASELINES, "fatigue": 0.12}
    event_tint = chord_event_tint_from_drive_events([
        {
            "id": 7,
            "source": "dialogue_residue",
            "event_label": "outside_hook",
            "suppressed": False,
            "brain": {
                "release_pressure": 0.9,
                "anchor_target": "outside",
                "novelty_pull": 0.8,
                "tension_load": 0.1,
            },
        }
    ])
    chemistry = chord_chemistry_snapshot(
        drives, warmth=0.52, shadow=0.18, event_tint=event_tint, now=1000
    )

    assert current_weather_chord(0.52, 0.18) == "Fmaj7"
    assert event_tint["brain"]["release_pressure"] == 0.9
    assert chemistry["route"]["event_vector"] == "outward"
    assert chemistry["route"]["vector"] == "outward"
    assert chemistry["event_tint"]["source"] == "dialogue_residue"
    assert chemistry["situation"] in {"scout", "spark"}


def test_drive_event_tint_ignores_retired_speech_event_source():
    drives = {**DRIVE_BASELINES, "curiosity": 0.62, "social": 0.50}
    baseline = chord_chemistry_snapshot(drives, warmth=0.60, shadow=0.18, now=1000)
    event_tint = chord_event_tint_from_drive_events([
        {
            "id": 8,
            "source": "speech_event",
            "event_label": "old_speech_event",
            "suppressed": False,
            "brain": {"expression_pressure": 0.05, "closeness_pull": 0.02},
        }
    ])
    tinted = chord_chemistry_snapshot(
        drives, warmth=0.60, shadow=0.18, event_tint=event_tint, now=1000
    )

    assert event_tint == {}
    for key in ("charge", "clutch", "strain"):
        assert tinted["core"][key] >= baseline["core"][key]


def test_drive_event_tint_uses_dialogue_after_retired_speech_event():
    event_tint = chord_event_tint_from_drive_events([
        {
            "id": 9,
            "source": "speech_event",
            "event_label": "old_speech_event",
            "suppressed": False,
            "brain": {"expression_pressure": 0.5},
        },
        {
            "id": 8,
            "source": "dialogue_residue",
            "event_label": "boundary_signal",
            "suppressed": False,
            "brain": {"territorial_alarm": 0.7, "tension_load": 0.2, "anchor_target": "boundary"},
        },
    ])

    assert event_tint["source"] == "dialogue_residue"
    assert event_tint["event_label"] == "boundary_signal"
    assert event_tint["route"]["vector"] == "guard"


def test_soothe_needs_shadow_context(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    calm = engine.apply_weather_delta(soothe=True, source="keyword")
    assert calm["soothe_active"] is False

    engine.apply_chord_echo("F#dim", source="thought")
    soothed = engine.apply_weather_delta(soothe=True, source="keyword")
    assert soothed["soothe_active"] is True
    assert soothed["warmth_residue"] > calm["warmth_residue"]
