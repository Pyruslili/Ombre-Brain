"""Subcurrent selection: approved pool only, exact drive_tag, no silent river."""

import pytest

pytest.importorskip("mcp.server.fastmcp")

from server import (
    _latent_low_stock_drives,
    _latent_select_weight,
    _select_approved_latent_note,
)


def _note(
    note_id: str,
    drive_tag: str,
    line: str,
    status: str = "approved",
    *,
    pinned: bool = False,
    delivered_count: int = 0,
) -> dict:
    return {
        "id": note_id,
        "status": status,
        "pinned": pinned,
        "note_type": "inward",
        "drive_tag": drive_tag,
        "dream_line": line,
        "created_at": "2026-07-19T00:00:00",
        "delivered_count": delivered_count,
    }


def test_exact_drive_tag_preferred(monkeypatch):
    data = {
        "notes": [
            _note("a1", "stewardship", "边界清楚了，房间和身体分开。"),
            _note("a2", "curiosity", "今日歌单还空着。让我想想。"),
            _note("a3", "general", "随便一张纸。"),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)

    picks = {_select_approved_latent_note(set(), drive_key="stewardship")["line"] for _ in range(12)}
    assert picks == {"边界清楚了，房间和身体分开。"}
    hit = _select_approved_latent_note(set(), drive_key="stewardship")
    assert hit["pool_match"] == "exact"
    assert hit["drive_tag"] == "stewardship"


def test_no_silent_general_or_cross_drive_fallback(monkeypatch):
    data = {
        "notes": [
            _note("c1", "curiosity", "今日歌单还空着。让我想想。"),
            _note("g1", "general", "流浪句。"),
            _note("r1", "reflection", "尺子量自己。"),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)

    assert _select_approved_latent_note(set(), drive_key="stewardship") is None
    assert _select_approved_latent_note(set(), drive_key="libido") is None


def test_relaxed_exclude_reuses_same_tag_only(monkeypatch):
    data = {
        "notes": [
            _note("s1", "stewardship", "映射有问题，明天得修。"),
            _note("c1", "curiosity", "今日歌单还空着。"),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)

    hit = _select_approved_latent_note({"s1"}, drive_key="stewardship")
    assert hit is not None
    assert hit["note_id"] == "s1"
    assert hit["pool_match"] == "relaxed_exclude"
    assert "歌单" not in hit["line"]


def test_draft_notes_never_deliver(monkeypatch):
    data = {
        "notes": [
            _note("d1", "stewardship", "还在草稿里。", status="draft"),
            _note("a1", "stewardship", "已确认的修屋。", status="approved"),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)

    hit = _select_approved_latent_note(set(), drive_key="stewardship")
    assert hit["line"] == "已确认的修屋。"


def test_pinned_is_reusable_not_vip_weight():
    fresh = {"pinned": False, "delivered_count": 0}
    pinned = {"pinned": True, "delivered_count": 0}
    assert _latent_select_weight(fresh) > _latent_select_weight(pinned)


def test_fresh_one_shot_preferred_over_pinned(monkeypatch):
    data = {
        "notes": [
            _note("p1", "curiosity", "常驻那句。", pinned=True),
            _note("f1", "curiosity", "新鲜一次。", pinned=False),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)
    picks = [_select_approved_latent_note(set(), drive_key="curiosity")["line"] for _ in range(40)]
    # Weighted: fresh should dominate; allow rare pin draws
    assert picks.count("新鲜一次。") > picks.count("常驻那句。")


def test_low_stock_lists_empty_drives_first(monkeypatch):
    data = {
        "notes": [
            *[_note(f"c{i}", "curiosity", f"c{i}") for i in range(8)],
            _note("s1", "stewardship", "只一条"),
            _note("soc1", "social", "社1"),
            _note("soc2", "social", "社2"),
            *[_note(f"f{i}", "fatigue", f"乏{i}") for i in range(3)],
            # general 再空也不该进自动补货
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)
    rows = _latent_low_stock_drives(data)
    tags = [r[0] for r in rows]
    assert "general" not in tags
    assert "social" in tags
    assert "fatigue" in tags
    # 绝对最少优先：social(2)/fatigue(3) 应排在 curiosity(8) 前
    assert tags.index("social") < tags.index("curiosity") if "curiosity" in tags else True
    assert tags.index("fatigue") < tags.index("curiosity") if "curiosity" in tags else True
