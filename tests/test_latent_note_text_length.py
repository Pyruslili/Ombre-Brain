import json

import pytest

pytest.importorskip("mcp.server.fastmcp")

import server


class FakeRequest:
    def __init__(self, body, *, note_id=""):
        self._body = body
        self.path_params = {"note_id": note_id}

    async def json(self):
        return self._body


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_thought_to_latent_keeps_full_text(monkeypatch):
    saved = {"version": 1, "notes": []}
    monkeypatch.setattr(server, "_load_latent_notes", lambda: saved)
    monkeypatch.setattr(server, "_save_latent_notes", lambda data: None)

    long_text = "被还原的是双方共同辨认的骨架。" * 30
    response = await server.api_sanctum_thought_to_latent(
        FakeRequest({"tid": "long-thought", "text": long_text, "drive": "curiosity"})
    )
    note = response_json(response)["note"]

    assert note["dream_line"] == long_text
    assert note["source_fragment"] == long_text


@pytest.mark.asyncio
async def test_thought_to_latent_repairs_existing_truncated_note(monkeypatch):
    existing = {
        "id": "latent-old",
        "status": "draft",
        "source_tid": "long-thought",
        "source_kind": "thought_pool",
        "dream_line": "被截断的半句",
        "source_fragment": "被截断的半句",
    }
    saved = {"version": 1, "notes": [existing]}
    writes = []
    monkeypatch.setattr(server, "_load_latent_notes", lambda: saved)
    monkeypatch.setattr(server, "_save_latent_notes", lambda data: writes.append(data))

    long_text = "完整的念头终于长回来了。" * 30
    response = await server.api_sanctum_thought_to_latent(
        FakeRequest({"tid": "long-thought", "text": long_text, "drive": "curiosity"})
    )
    payload = response_json(response)

    assert payload["already"] is True
    assert payload["note"]["dream_line"] == long_text
    assert payload["note"]["source_fragment"] == long_text
    assert writes


@pytest.mark.asyncio
async def test_manual_latent_create_and_update_keep_full_text(monkeypatch):
    saved = {"version": 1, "notes": []}
    monkeypatch.setattr(server, "_require_auth", lambda request: None)
    monkeypatch.setattr(server, "_load_latent_notes", lambda: saved)
    monkeypatch.setattr(server, "_save_latent_notes", lambda data: None)

    original = "现场不是日志。" * 50
    created = await server.api_latent_notes_create(FakeRequest({"dream_line": original}))
    note = response_json(created)["note"]
    assert note["dream_line"] == original
    assert note["source_fragment"] == original

    replacement = "日志留下骨头，但没有留下转头。" * 50
    updated = await server.api_latent_notes_update(
        FakeRequest({"dream_line": replacement}, note_id=note["id"])
    )
    assert response_json(updated)["note"]["dream_line"] == replacement
