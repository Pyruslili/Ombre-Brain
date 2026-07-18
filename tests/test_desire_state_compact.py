import time

import pytest

pytest.importorskip("mcp.server.fastmcp")

from server import (
    _compact_desire_state,
    _gravity_is_decisive,
    _undercurrent_state,
    _undertow_snapshot,
    _weather_panel_lines,
)


def test_compact_desire_state_keeps_hook_fields_without_full_internal_state():
    state = {
        "drives": {"attachment": 0.4, "stress": 0.2},
        "effective_drives": {"attachment": 0.38, "stress": 0.19},
        "drive_activations": {"attachment": 0.143, "stress": 0.059},
        "effective_activations": {"attachment": 0.139, "stress": 0.055},
        "local_fatigue": {"attachment": 0.01},
        "drive_outputs": {"attachment": {"mode": "slow"}},
        "pulse_weather": {
            "undertow": "attachment",
            "undertow_value": 0.4,
            "warmth": 0.61,
            "shadow": 0.22,
            "climate_display": "Overcast",
            "chord_display": "Gmaj7→Fmaj7",
            "mood_trace": "first thought",
            "warmth_residue": 0.04,
            "shadow_residue": 0.02,
            "component_shadow_residue": 0.01,
            "crystal_shadow": 0.01,
            "shadow_crystal": {"kind": "possessiveness", "heat": 0.12, "hardness": 0.3},
            "base_warmth": 0.57,
            "base_shadow": 0.20,
            "current_chord": "Fmaj7#11",
            "gravity": "重心往屋里坠，手还没松。",
            "gravity_decisive": True,
            "chemistry_core": {"depth": 0.7},
            "chemistry_route": {"pull": 0.6, "vector": "toward_house"},
            "gravity_pool": "pull",
            "derived_texture": {"primary": "depth"},
            "source_stack": [{"debug": True}],
        },
        "now_playing": {"title": "Light Song", "artist": "haruka nakamura"},
        "thoughts": [
            {
                "tid": str(i),
                "drive": "attachment",
                "kind": "flit",
                "strength": 0.4,
                "text": f"thought {i}",
                "born_at": time.time() - i,
            }
            for i in range(12)
        ],
        "drive_events": [
            {
                "id": i,
                "ts": 1000 + i,
                "event_label": str(i),
                "primary_drive": "attachment",
                "reason": "ok",
                "applied": {"attachment": {"delta": 0.01}},
                "brain": {"source_stack": ["debug"], "source": "dialogue_residue"},
            }
            for i in range(9)
        ],
    }

    compact = _compact_desire_state(state)

    assert len(compact["thoughts"]) == 8
    assert compact["thoughts"] == compact["recent_thoughts"]
    assert len(compact["drive_events"]) == 5
    assert len(compact["recent_drive_events"]) == 5
    assert compact["drive_events"] == compact["recent_drive_events"]
    assert compact["drive_events"][0]["ts"] == 1000
    assert compact["drive_events"][0]["reason"] == "ok"
    assert "source_stack" not in compact["drive_events"][0]["brain"]
    assert "source_stack" not in compact["pulse_weather"]
    assert compact["weather_panel"]["atmosphere"] == "Overcast"
    assert compact["effective_activations"]["attachment"] == 0.139
    assert compact["weather_panel"]["chord"] == "Gmaj7→Fmaj7"
    assert compact["weather_panel"]["gravity"] == "重心往屋里坠，手还没松。"
    assert compact["weather_panel"]["now_playing"] == "Light Song - haruka nakamura"
    assert _weather_panel_lines(compact["weather_panel"])[-1] == "♪ On Air：Light Song - haruka nakamura"
    assert not any(line.startswith("Warmth/Shadow") for line in _weather_panel_lines(compact["weather_panel"]))
    assert compact["pulse_weather"]["warmth_residue"] == 0.04
    assert compact["pulse_weather"]["shadow_residue"] == 0.02
    assert compact["pulse_weather"]["crystal_shadow"] == 0.01
    assert compact["weather_residue"]["shadow_crystal"]["kind"] == "possessiveness"
    assert compact["weather_residue"]["base_warmth"] == 0.57
    assert compact["weather_residue"]["base_shadow"] == 0.20
    assert compact["pulse_weather"]["chemistry_core"] == {"depth": 0.7}
    assert compact["pulse_weather"]["gravity_pool"] == "pull"
    assert compact["pulse_weather"]["gravity"] == "重心往屋里坠，手还没松。"

    undercurrent = _undercurrent_state(state)
    assert undercurrent["Drive"] == {"attachment": 0.4, "stress": 0.2}
    assert undercurrent["Activation"] == {"attachment": 0.139, "stress": 0.055}
    assert undercurrent["Affect"] == {"Warmth": 0.61, "Shadow": 0.22, "Longing": 0.0}
    assert undercurrent["Chemistry"]["Vector"] == "toward_house"
    assert undercurrent["Thought Pool"][0]["index"] == 2
    assert undercurrent["Thought Pool"][0]["text"] == "thought 1"


def test_undertow_compares_pressure_above_each_own_baseline():
    drive, pressure, raw = _undertow_snapshot(
        {
            "drives": {"attachment": 0.482, "stewardship": 0.453, "fatigue": 0.9},
            "intent": None,
        }
    )

    assert drive == "stewardship"
    assert pressure == 0.253
    assert raw == 0.453


def test_gravity_requires_non_hover_route_with_clear_margin():
    assert not _gravity_is_decisive(
        {"chord_chemistry": {"route": {"vector": "hover", "scores": {"hover": 0.51, "toward_house": 0.44}}}}
    )
    assert not _gravity_is_decisive(
        {"chord_chemistry": {"route": {"vector": "toward_house", "scores": {"toward_house": 0.51, "hover": 0.46}}}}
    )
    assert _gravity_is_decisive(
        {"chord_chemistry": {"route": {"vector": "toward_house", "scores": {"toward_house": 0.70, "hover": 0.46}}}}
    )


def test_thought_pool_keeps_full_text_in_compact_readouts():
    long_text = "现场留下的骨头" * 80
    state = {
        "thoughts": [
            {"tid": "newest", "drive": "curiosity", "kind": "flit", "strength": 0.8, "text": "top"},
            {"tid": "long", "drive": "curiosity", "kind": "flit", "strength": 0.7, "text": long_text},
        ]
    }

    compact = _compact_desire_state(state)
    undercurrent = _undercurrent_state(state)

    assert compact["thoughts"][1]["text"] == long_text
    assert undercurrent["Thought Pool"][0]["text"] == long_text
