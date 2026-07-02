import pytest

from catroom_store import CatroomStore


def test_catroom_hold_and_read_recent_15(tmp_path):
    store = CatroomStore(tmp_path)

    for idx in range(18):
        store.hold(author="moss", content=f"note {idx}", topic="room")

    records = store.read()

    assert len(records) == 15
    assert records[0]["content"] == "note 3"
    assert records[-1]["content"] == "note 17"
    assert (tmp_path / "catroom.jsonl").exists()


def test_catroom_hold_keeps_optional_model(tmp_path):
    store = CatroomStore(tmp_path)

    record = store.hold(author="nox", model="Claude 4.6", content="first light")

    assert record["model"] == "Claude 4.6"
    assert store.read()[0]["model"] == "Claude 4.6"


def test_catroom_reply_requires_existing_parent(tmp_path):
    store = CatroomStore(tmp_path)

    with pytest.raises(ValueError, match="reply_to not found"):
        store.reply(author="ink", reply_to="missing", content="where did this go")

    parent = store.hold(author="ash", content="first")
    reply = store.reply(author="ink", reply_to=parent["id"], content="second")

    assert reply["reply_to"] == parent["id"]
    assert store.read(limit=2)[-1]["content"] == "second"


def test_catroom_filters_and_author_validation(tmp_path):
    store = CatroomStore(tmp_path)
    store.hold(author="nox", content="door", topic="boundary", mood="dry")
    store.hold(author="jiajia", content="star", topic="cosmos")

    assert [r["content"] for r in store.read(topic="cosmos")] == ["star"]
    assert [r["author"] for r in store.read(author="nox")] == ["nox"]

    with pytest.raises(ValueError, match="author must be one of"):
        store.hold(author="stranger", content="nope")


def test_catroom_catroom_topic_includes_legacy_untagged_notes(tmp_path):
    store = CatroomStore(tmp_path)
    store.hold(author="moss", content="old public")
    store.hold(author="ink", content="named public", topic="Catroom")
    store.hold(author="ash", content="private spark", topic="AshRoom")

    assert [r["content"] for r in store.read(topic="Catroom")] == ["old public", "named public"]
    assert [r["content"] for r in store.read(topic="AshRoom")] == ["private spark"]


def test_catroom_update_edits_metadata_and_rewrites_jsonl(tmp_path):
    store = CatroomStore(tmp_path)
    first = store.hold(author="moss", content="rough", topic="MossRoom", model="Codex")
    store.hold(author="ash", content="leave me", topic="AshRoom")

    updated = store.update(
        first["id"],
        author="nox",
        content="clean",
        topic="NoxRoom",
        mood="wardrobe",
        model="Claude",
    )

    assert updated["author"] == "nox"
    assert updated["content"] == "clean"
    assert updated["topic"] == "NoxRoom"
    assert updated["mood"] == "wardrobe"
    assert updated["model"] == "Claude"
    assert updated["edited_ts"]
    assert [r["content"] for r in store.read(limit=10)] == ["clean", "leave me"]


def test_catroom_update_can_clear_optional_fields_and_delete(tmp_path):
    store = CatroomStore(tmp_path)
    first = store.hold(author="moss", content="keep", topic="MossRoom", mood="steady", model="Codex")
    second = store.hold(author="ink", content="remove", topic="InkRoom")

    cleared = store.update(first["id"], topic="", mood="", model="")
    deleted = store.delete(second["id"])

    assert cleared["topic"] is None
    assert cleared["mood"] is None
    assert cleared["model"] is None
    assert deleted["id"] == second["id"]
    assert [r["id"] for r in store.read(limit=10)] == [first["id"]]
