"""Subcurrent selection: approved pool only, exact drive_tag, no silent river."""

import pytest

pytest.importorskip("mcp.server.fastmcp")

from server import _select_approved_latent_note


def _note(note_id: str, drive_tag: str, line: str, status: str = "approved") -> dict:
    return {
        "id": note_id,
        "status": status,
        "pinned": False,
        "note_type": "inward",
        "drive_tag": drive_tag,
        "dream_line": line,
        "created_at": "2026-07-19T00:00:00",
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
