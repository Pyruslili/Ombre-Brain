from __future__ import annotations

import time
from pathlib import Path

from rhythm_store import RhythmStore


def test_append_and_read_phone_watch(tmp_path: Path):
    store = RhythmStore(tmp_path / "rhythm.json", max_events=50)
    store.append(source="phone", app="小红书", event="open")
    store.append(source="watch", kind="hr", value=72, event="sample")
    snap = store.read(minutes=60, limit=20)
    assert snap["ok"] is True
    assert snap["phone"]["last_app"] == "小红书"
    assert snap["watch"]["hr"] == 72
    assert "小红书" in (snap["note"] or "")
    assert snap["count"] >= 2


def test_window_filters_old_events(tmp_path: Path):
    store = RhythmStore(tmp_path / "rhythm.json")
    old = time.time() - 5 * 3600
    store.append(source="phone", app="微信", event="open", at=old)
    store.append(source="phone", app="抖音", event="open")
    snap = store.read(minutes=30, limit=10)
    apps = [e.get("app") for e in snap["phone"]["recent"]]
    assert "抖音" in apps
    assert "微信" not in apps


def test_max_events_trim(tmp_path: Path):
    store = RhythmStore(tmp_path / "rhythm.json", max_events=5)
    for i in range(12):
        store.append(source="phone", app=f"app{i}", event="open")
    data = store._load()
    assert len(data["events"]) == 5
