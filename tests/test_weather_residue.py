from desire_engine import DesireEngine, DRIVE_BASELINES, pa_na_snapshot


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
    assert warm["warmth_residue"] > 0
    assert shadow["kind"] == "shadow"
    assert shadow["shadow_residue"] > 0


def test_soothe_needs_shadow_context(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    calm = engine.apply_weather_delta(soothe=True, source="keyword")
    assert calm["soothe_active"] is False

    engine.apply_chord_echo("F#dim", source="thought")
    soothed = engine.apply_weather_delta(soothe=True, source="keyword")
    assert soothed["soothe_active"] is True
    assert soothed["warmth_residue"] > calm["warmth_residue"]
