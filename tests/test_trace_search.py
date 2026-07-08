import importlib
import os

import pytest
import yaml

pytest.importorskip("mcp.server.fastmcp")


class FakeBucketManager:
    def __init__(self, buckets):
        self._buckets = buckets

    async def list_all(self, include_archive=False):
        return list(self._buckets)


def make_bucket(bucket_id, content, *, tags=None, domain=None, bucket_type="dynamic", resolved=False, digested=False, created="2026-07-08T00:00:00"):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "created": created,
            "name": bucket_id,
            "type": bucket_type,
            "tags": tags or [],
            "domain": domain or [],
            "resolved": resolved,
            "digested": digested,
        },
    }


@pytest.fixture
def server_module(tmp_path, monkeypatch, test_config):
    buckets_dir = tmp_path / "buckets"
    for subdir in ("permanent", "dynamic", "archive", "feel"):
        os.makedirs(buckets_dir / subdir, exist_ok=True)

    config_path = tmp_path / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(test_config | {"buckets_dir": str(buckets_dir)}, f)

    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(buckets_dir))
    monkeypatch.setenv("OMBRE_HOOK_SKIP", "1")

    import server

    return importlib.reload(server)


@pytest.mark.asyncio
async def test_trace_matches_content_or_exact_tag_only(server_module, monkeypatch):
    buckets = [
        make_bucket("content-hit", "这里有火星猫。"),
        make_bucket("tag-hit", "正文没有目标词。", tags=["火星猫"]),
        make_bucket("name-only", "正文没有。", tags=["别的"]),
        make_bucket("domain-only", "正文没有。", domain=["火星猫"]),
        make_bucket("fuzzy-tag", "正文没有。", tags=["火星猫尾巴"]),
    ]
    monkeypatch.setattr(server_module, "bucket_mgr", FakeBucketManager(buckets))
    monkeypatch.setattr(server_module, "_load_all_marks", lambda: {})

    result = await server_module.wander(mode="trace", query="火星猫", limit=15)

    assert "content-hit" in result
    assert "tag-hit" in result
    assert "name-only" not in result
    assert "domain-only" not in result
    assert "fuzzy-tag" not in result


@pytest.mark.asyncio
async def test_trace_excludes_settled_and_caps_at_15(server_module, monkeypatch):
    buckets = [
        make_bucket(f"active-{i:02d}", "骨头", created=f"2026-07-08T00:{i:02d}:00")
        for i in range(20)
    ]
    buckets.extend([
        make_bucket("resolved-hit", "骨头", resolved=True),
        make_bucket("digested-hit", "骨头", digested=True),
    ])
    monkeypatch.setattr(server_module, "bucket_mgr", FakeBucketManager(buckets))
    monkeypatch.setattr(server_module, "_load_all_marks", lambda: {})

    result = await server_module.wander(mode="trace", query="骨头", limit=50)

    assert result.count("---") == 14
    assert "active-00" in result
    assert "active-14" in result
    assert "active-15" not in result
    assert "resolved-hit" not in result
    assert "digested-hit" not in result


@pytest.mark.asyncio
async def test_trace_returns_null_without_literal_match(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "bucket_mgr", FakeBucketManager([
        make_bucket("semantic-neighbor", "这里写的是相似意思，但没有那个词。"),
    ]))
    monkeypatch.setattr(server_module, "_load_all_marks", lambda: {})

    result = await server_module.wander(mode="trace", query="火星猫", limit=15)

    assert result == "null"
