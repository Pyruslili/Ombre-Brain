from __future__ import annotations

import time
from pathlib import Path

from rhythm_store import RhythmStore, normalize_bark_key


def test_normalize_bark_key_accepts_plain_key():
    assert normalize_bark_key("deviceKey123") == "deviceKey123"


def test_normalize_bark_key_unwraps_api_day_url():
    url = "https://api.day.app/deviceKey123/Title?icon=https://example.com/avatar.jpg"
    assert normalize_bark_key(url) == "deviceKey123"


def test_normalize_bark_key_does_not_unwrap_other_hosts():
    url = "https://example.com/deviceKey123/Title"
    assert normalize_bark_key(url) == url


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


def test_skip_empty_and_noise_phone(tmp_path: Path):
    store = RhythmStore(tmp_path / "rhythm.json")
    assert store.append(source="phone", app="").get("skipped")
    assert store.append(source="phone", app="Bark").get("skipped")
    assert store.append(source="phone", app="快捷指令").get("skipped")
    store.append(source="phone", app="微信")
    snap = store.read(limit=5)
    assert snap["count"] == 1
    assert snap["phone"]["last_app"] == "微信"


def test_merge_consecutive_same_app(tmp_path: Path):
    store = RhythmStore(tmp_path / "rhythm.json")
    store.append(source="phone", app="小红书")
    r = store.append(source="phone", app="小红书")
    assert r.get("merged") is True
    data = store._load()
    assert len(data["events"]) == 1


def test_default_read_limit_five(tmp_path: Path):
    store = RhythmStore(tmp_path / "rhythm.json")
    for i in range(8):
        store.append(source="phone", app=f"app{i}")
    snap = store.read()  # default limit 5
    assert snap["count"] == 5
    assert len(snap["phone"]["recent"]) == 5
