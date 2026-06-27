import pytest

pytest.importorskip("mcp.server.fastmcp")

from server import _compact_desire_state


def test_compact_desire_state_keeps_hook_fields_without_full_internal_state():
    state = {
        "drives": {"attachment": 0.4, "stress": 0.2},
        "effective_drives": {"attachment": 0.38, "stress": 0.19},
        "local_fatigue": {"attachment": 0.01},
        "drive_outputs": {"attachment": {"mode": "slow"}},
        "pulse_weather": {
            "undertow": "attachment",
            "undertow_value": 0.4,
            "warmth": 0.61,
            "shadow": 0.22,
            "current_chord": "Fmaj7#11",
            "gravity": "重心往屋里坠，爪还没松。",
            "chemistry_core": {"depth": 0.7},
            "chemistry_route": {"pull": 0.6},
            "derived_texture": {"primary": "depth"},
            "source_stack": [{"debug": True}],
        },
        "thoughts": [
            {"tid": str(i), "drive": "attachment", "kind": "flit", "strength": 0.4, "text": f"thought {i}"}
            for i in range(12)
        ],
        "drive_events": [{"event_label": str(i), "brain": {"source_stack": ["debug"]}} for i in range(9)],
    }

    compact = _compact_desire_state(state)

    assert len(compact["thoughts"]) == 8
    assert compact["thoughts"] == compact["recent_thoughts"]
    assert len(compact["recent_drive_events"]) == 5
    assert "drive_events" not in compact
    assert "source_stack" not in compact["pulse_weather"]
    assert compact["pulse_weather"]["chemistry_core"] == {"depth": 0.7}
    assert compact["pulse_weather"]["gravity"] == "重心往屋里坠，爪还没松。"
