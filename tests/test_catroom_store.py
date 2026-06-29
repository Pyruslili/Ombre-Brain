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
