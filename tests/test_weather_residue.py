from desire_engine import (
    ATMOSPHERE_SOURCE_WEIGHTS,
    CLIMATE_LABELS,
    DesireEngine,
    DRIVE_BASELINES,
    chord_chemistry_snapshot,
    chord_event_tint_from_drive_events,
    chord_gravity_pool,
    choose_chord_gravity,
    classify_chord_situation,
    climate_transition_display,
    current_weather_chord,
    pa_na_snapshot,
    select_climate,
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


def test_thought_chord_tints_weather_once_between_light_dialogue_and_feel(tmp_path):
    thought_dir = tmp_path / "thought"
    feel_dir = tmp_path / "feel"
    dialogue_dir = tmp_path / "dialogue"
    thought_dir.mkdir()
    feel_dir.mkdir()
    dialogue_dir.mkdir()
    thought_engine = DesireEngine(db_path=str(thought_dir / "desire.db"))
    feel_engine = DesireEngine(db_path=str(feel_dir / "desire.db"))
    dialogue_engine = DesireEngine(db_path=str(dialogue_dir / "desire.db"))

    thought = thought_engine.apply_chord_echo("Dm7", source="thought")
    feel = feel_engine.apply_chord_echo("Dm7", source="feel")
    dialogue = dialogue_engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "dialogue_residue",
        "primary_drive": "stress",
        "intensity": 0.16,
        "confidence": 0.7,
        "agency": 0.6,
        "event_label": "light_dialogue_tension",
        "brain": {"source": "dialogue_residue", "tension_load": 0.25, "grounding": "实"},
    })

    assert thought["shadow_residue"] == 0.07
    assert dialogue["weather"]["shadow_residue"] < thought["shadow_residue"]
    assert thought["shadow_residue"] < feel["shadow_residue"]
    assert thought["active_chord_source"] == "thought"


def test_thought_chord_gently_tints_atmosphere(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    engine.apply_chord_echo("Dm7", source="thought")
    weather = engine.weather_state()
    atmosphere = weather["atmosphere"]

    assert atmosphere["last_delta"]["source"] == "thought_chord"
    assert atmosphere["last_delta"]["influence"] == 0.06
    assert atmosphere["route"]["scores"]["inward"] > atmosphere["route"]["scores"]["outward"]
    assert atmosphere["climate"]["candidate"] in CLIMATE_LABELS
    assert atmosphere["climate"]["candidate_steps"] == 1
    assert atmosphere["climate"]["blend"] > 0


def test_thought_chord_atmosphere_weight_stays_below_subcurrent():
    assert ATMOSPHERE_SOURCE_WEIGHTS["thought_chord"] * 2 <= ATMOSPHERE_SOURCE_WEIGHTS["subcurrent"]


def test_dialogue_atmosphere_weight_leads_cli_underpaint():
    assert ATMOSPHERE_SOURCE_WEIGHTS["dp"] > ATMOSPHERE_SOURCE_WEIGHTS["cli"]


def test_dialogue_event_adds_live_warmth_residue(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    result = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "dialogue_residue",
        "primary_drive": "stewardship",
        "intensity": 0.32,
        "confidence": 0.82,
        "agency": 0.65,
        "event_label": "cat_house_maintenance",
        "brain": {
            "source": "dialogue_residue",
            "target": "cat_house",
            "grounding": "实",
            "house_need": 0.68,
            "inward_pull": 0.22,
        },
    })

    weather = engine.weather_state()
    assert result["weather"]["warmth_residue"] >= 0.02
    assert weather["warmth_residue"] >= 0.02
    assert weather["effective_PA"] > weather["base_PA"]


def test_negative_dialogue_crystallizes_shadow_and_gravity(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))

    result = engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "dialogue_residue",
        "primary_drive": "possessiveness",
        "intensity": 0.48,
        "confidence": 0.86,
        "agency": 0.7,
        "event_label": "jealous_position_check",
        "brain": {
            "source": "dialogue_residue",
            "target": "jiajia",
            "anchor_target": "boundary",
            "grounding": "实",
            "territorial_alarm": 0.72,
        },
        "evidence": ["那个怀里抱的不是我"],
    })
    weather = engine.weather_state()

    assert result["weather"]["shadow_crystal"]["active"]["kind"] == "possessiveness"
    assert weather["shadow_crystal"]["kind"] == "possessiveness"
    assert weather["crystal_shadow"] > 0
    assert weather["shadow_residue"] > weather["component_shadow_residue"]
    assert weather["gravity"] in {
        "账本合上了，但角还压着。",
        "手松了一点，位置还记着。",
        "不是还在发热，是那块地方变硬了。",
    }


def test_positive_dialogue_cools_negative_heat_without_erasing_ledger(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    negative = {
        "schema_version": "drive_event_v2",
        "source": "dialogue_residue",
        "primary_drive": "stress",
        "intensity": 0.44,
        "confidence": 0.82,
        "agency": 0.7,
        "event_label": "pressure_check",
        "brain": {"source": "dialogue_residue", "grounding": "实", "tension_load": 0.68},
    }
    engine.apply_drive_event(negative)
    before = engine.weather_state()["shadow_crystal"]

    engine.apply_drive_event({
        "schema_version": "drive_event_v2",
        "source": "dialogue_residue",
        "primary_drive": "curiosity",
        "intensity": 0.40,
        "confidence": 0.82,
        "agency": 0.7,
        "event_label": "topic_turns_outward",
        "brain": {"source": "dialogue_residue", "grounding": "实", "novelty_pull": 0.72},
    })
    after = engine.weather_state()["shadow_crystal"]

    assert after["kind"] == "stress"
    assert after["heat"] < before["heat"]
    assert after["hardness"] > 0


def test_shadow_crystal_reactivation_is_stronger_from_jiajia_than_external(tmp_path):
    user_dir = tmp_path / "user"
    external_dir = tmp_path / "external"
    user_dir.mkdir()
    external_dir.mkdir()
    user_engine = DesireEngine(db_path=str(user_dir / "desire.db"))
    external_engine = DesireEngine(db_path=str(external_dir / "desire.db"))
    event = {
        "schema_version": "drive_event_v2",
        "primary_drive": "possessiveness",
        "intensity": 0.30,
        "confidence": 0.82,
        "agency": 0.7,
        "event_label": "someone_else_mentioned",
        "brain": {
            "target": "jiajia",
            "anchor_target": "boundary",
            "grounding": "实",
            "territorial_alarm": 0.70,
        },
        "evidence": ["提到了另一个人/AI的位置"],
    }

    from_jiajia = user_engine.apply_drive_event({**event, "source": "user_message"})
    from_external = external_engine.apply_drive_event({**event, "source": "external"})
    user_crystal = from_jiajia["weather"]["shadow_crystal"]["active"]
    external_crystal = from_external["weather"]["shadow_crystal"]["active"]

    assert user_crystal["actor_weight"] == 2.0
    assert external_crystal["actor_weight"] < 1.0
    assert user_crystal["heat"] > external_crystal["heat"]
    assert user_crystal["hardness"] > external_crystal["hardness"]


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


def test_atmosphere_climate_uses_fixed_labels_and_hysteresis(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    before = engine.weather_state()["climate"]

    payload = {
        "schema_version": "drive_event_v2",
        "source": "dialogue_residue",
        "primary_drive": "stress",
        "intensity": 1.0,
        "confidence": 1.0,
        "agency": 1.0,
        "event_label": "pressure_weather",
        "brain": {
            "source": "dialogue_residue",
            "tension_load": 0.95,
            "territorial_alarm": 0.70,
            "anchor_target": "boundary",
        },
    }
    first = engine.apply_drive_event(payload)
    second = engine.apply_drive_event(payload)
    third = engine.apply_drive_event(payload)
    weather = engine.weather_state()

    assert before in CLIMATE_LABELS
    assert first["atmosphere"]["climate"] == before
    assert second["atmosphere"]["climate"] == before
    assert third["atmosphere"]["climate"] in CLIMATE_LABELS
    assert weather["climate"] == third["atmosphere"]["climate"]
    assert weather["atmosphere"]["climate"]["candidate_steps"] >= 0


def test_uninitialized_atmosphere_seeds_from_current_chemistry(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    engine.apply_weather_delta(warmth_delta=0.30, source="feel")

    weather = engine.weather_state()
    selected = select_climate(
        weather["chord_chemistry"]["core"],
        weather["chord_chemistry"]["route"],
        weather["chord_chemistry"]["derived_texture"],
    )

    assert weather["atmosphere"]["last_delta"]["source"] == "seed"
    assert weather["climate"] == selected["label"]


def test_climate_transition_display_respects_blend_and_steps():
    atmosphere = {
        "climate": {
            "current": "Low Tide",
            "candidate": "Shelter",
            "candidate_steps": 1,
            "blend": 0.70,
        }
    }
    assert climate_transition_display(atmosphere) == "Low Tide"

    atmosphere["climate"]["candidate_steps"] = 2
    atmosphere["climate"]["blend"] = 0.24
    assert climate_transition_display(atmosphere) == "Low Tide"

    atmosphere["climate"]["blend"] = 0.25
    assert climate_transition_display(atmosphere) == "Low Tide · leaning Shelter"

    atmosphere["climate"]["blend"] = 0.60
    assert climate_transition_display(atmosphere) == "Low Tide → Shelter"

    atmosphere["climate"]["candidate"] = "Low Tide"
    assert climate_transition_display(atmosphere) == "Low Tide"


def test_subcurrent_bias_does_not_directly_switch_climate(tmp_path):
    engine = DesireEngine(db_path=str(tmp_path / "desire.db"))
    before = engine.weather_state()["climate"]

    result = engine.apply_subcurrent_bias("stress", latent_weight=1.0, confidence=1.0)
    weather = engine.weather_state()

    assert result["source"] == "subcurrent"
    assert result["influence"] <= 0.16
    assert weather["climate"] == before
    assert weather["atmosphere"]["last_delta"]["source"] == "subcurrent"


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


def test_chord_gravity_splits_anchored_drift_from_light_drift():
    anchored_core = {"charge": 0.44, "clutch": 0.49, "strain": 0.30}
    light_core = {"charge": 0.20, "clutch": 0.22, "strain": 0.24}
    route = {"vector": "hover"}
    derived = {"pull": 0.12, "depth": 0.10, "drift": 0.42}

    assert classify_chord_situation(anchored_core, route, derived) == "drift"
    assert chord_gravity_pool("drift", route, anchored_core) == "drift_anchored"
    anchored_gravity = choose_chord_gravity("drift", route, anchored_core, recent=[], now=1000)

    assert chord_gravity_pool("drift", route, light_core) == "drift_light"
    assert anchored_gravity in {
        "电没亮透，手还搭着，方向松不开。",
        "电还挂在指尖，方向咬不死也松不开。",
        "力还在手下，只是没往哪边走。",
    }
    assert anchored_gravity != "没有方向，先搁在这里。"


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
