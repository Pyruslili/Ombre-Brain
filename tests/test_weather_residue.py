from desire_engine import (
    DesireEngine,
    DRIVE_BASELINES,
    chord_chemistry_snapshot,
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


def test_chord_echo_routes_by_source_and_chord(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    warm = engine.apply_chord_echo("Fmaj7", source="feel")
    shadow = engine.apply_chord_echo("Dm7", source="thought")

    assert warm["kind"] == "warmth"
    assert warm["active_chord"] == "Fmaj7"
    assert warm["active_chord_source"] == "feel"
    assert warm["warmth_residue"] > 0
    assert shadow["kind"] == "shadow"
    assert shadow["active_chord"] == "Dm7"
    assert shadow["active_chord_source"] == "thought"
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
    assert chemistry["derived_texture"]["guard"] > 0.55
    assert chemistry["gravity_line"]


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


def test_soothe_needs_shadow_context(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    calm = engine.apply_weather_delta(soothe=True, source="keyword")
    assert calm["soothe_active"] is False

    engine.apply_chord_echo("F#dim", source="thought")
    soothed = engine.apply_weather_delta(soothe=True, source="keyword")
    assert soothed["soothe_active"] is True
    assert soothed["warmth_residue"] > calm["warmth_residue"]
