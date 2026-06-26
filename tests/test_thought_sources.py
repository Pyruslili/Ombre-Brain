import time

from desire_engine import Thought, tick_thoughts
from desire_engine import DesireEngine


def test_tick_thoughts_does_not_generate_collision_thoughts(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    now = time.time()
    thoughts = [
        Thought(tid="a", text="想靠近一点", drive="attachment", kind="flit", strength=0.7, born_at=now),
        Thought(tid="b", text="想拆开看看", drive="curiosity", kind="flit", strength=0.7, born_at=now),
    ]

    new_thoughts, _ = tick_thoughts(thoughts)

    assert not any(t.source == "collision" for t in new_thoughts)


def test_thought_source_metadata_roundtrips(tmp_path):
    engine = DesireEngine(str(tmp_path / "desire.db"))
    engine.add_thought(
        "source-aware thought",
        "reflection",
        strength=0.6,
        source="analyze_nocturne_entry",
        source_bucket="bucket-123",
        source_type="writing",
        source_created="2026-06-25T01:02:03Z",
    )

    thought = next(t for t in engine.store.load_thoughts() if t.text == "source-aware thought")
    assert thought.source == "analyze_nocturne_entry"
    assert thought.source_bucket == "bucket-123"
    assert thought.source_type == "writing"
    assert thought.source_created == "2026-06-25T01:02:03Z"
