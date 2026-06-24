import time

from desire_engine import Thought, _collision_today, _last_collision, tick_thoughts


def test_collision_thought_source_is_marked_collision(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    _last_collision.clear()
    _collision_today["date"] = ""
    _collision_today["count"] = 0
    now = time.time()
    thoughts = [
        Thought(tid="a", text="想靠近一点", drive="attachment", kind="flit", strength=0.7, born_at=now),
        Thought(tid="b", text="想拆开看看", drive="curiosity", kind="flit", strength=0.7, born_at=now),
    ]

    new_thoughts, _ = tick_thoughts(thoughts)

    assert any(t.source == "collision" for t in new_thoughts)
