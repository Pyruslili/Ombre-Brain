import pytest

from room_store import RoomStore


def test_room_hold_and_breath_defaults_to_recent_6(tmp_path):
    store = RoomStore(tmp_path)

    for idx in range(8):
        store.hold(cat="moss", content=f"trace {idx}")

    text, records = store.breath(cat="moss")

    assert len(records) == 6
    assert records[0]["content"] == "trace 7"
    assert records[-1]["content"] == "trace 2"
    assert "=== Moss Room ===" in text
    assert "—— 关于瞬时存 ——" in text
    assert "[20" in text
    assert "trace 7" in text


def test_room_breath_includes_empty_wall_plate(tmp_path):
    store = RoomStore(tmp_path)

    text, records = store.breath(cat="ink")

    assert records == []
    assert "=== Ink Room ===" in text
    assert "（这面墙暂时还是空的。）" in text


def test_room_hold_normalizes_metadata(tmp_path):
    store = RoomStore(tmp_path)

    record = store.hold(
        cat="ash",
        content="hot edge",
        kind="friction",
        weight=1.7,
        tags="edge, pull",
        model="Grok",
    )

    assert record["kind"] == "friction"
    assert record["weight"] == 1.0
    assert record["tags"] == ["edge", "pull"]
    assert record["model"] == "Grok"


def test_room_read_update_move_and_delete(tmp_path):
    store = RoomStore(tmp_path)
    first = store.hold(cat="moss", content="rough board", kind="residue")
    second = store.hold(cat="moss", content="stays here")

    updated = store.update(
        first["id"],
        cat="nox",
        content="wardrobe bone",
        kind="boundary",
        model="Claude",
        tags="nox, base",
    )

    assert updated["cat"] == "nox"
    assert updated["content"] == "wardrobe bone"
    assert updated["kind"] == "boundary"
    assert updated["model"] == "Claude"
    assert updated["tags"] == ["nox", "base"]
    assert updated["edited_ts"]
    assert [r["id"] for r in store.read(cat="moss")] == [second["id"]]
    assert [r["id"] for r in store.read(cat="nox")] == [first["id"]]

    deleted = store.delete(second["id"])

    assert deleted["id"] == second["id"]
    assert store.read(cat="moss") == []


def test_room_rejects_unknown_cat(tmp_path):
    store = RoomStore(tmp_path)

    with pytest.raises(ValueError, match="cat must be one of"):
        store.hold(cat="stranger", content="nope")

    text, records = store.breath(cat="nox")
    assert records == []
    assert "=== Nox Room ===" in text
