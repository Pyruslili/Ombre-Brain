import importlib
import os

import pytest
import yaml

pytest.importorskip("mcp.server.fastmcp")


def test_wander_only_filter_checks_domain_and_tags(tmp_path, monkeypatch, test_config):
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

    importlib.reload(server)

    assert server._is_wander_only_bucket({"metadata": {"domain": ["writing"], "tags": []}})
    assert server._is_wander_only_bucket({"metadata": {"domain": [], "tags": ["letter"]}})
    assert server._is_wander_only_bucket({"metadata": {"domain": [], "tags": ["letter_jiajia"]}})
    assert not server._is_wander_only_bucket({"metadata": {"domain": ["memory"], "tags": ["daily"]}})
    assert not server._is_wander_only_bucket({"metadata": {"domain": ["unresolved"], "tags": []}})
    assert server._breath_memory_candidates([
        {"id": "u1", "content": "悬而未决", "metadata": {"type": "dynamic", "domain": ["unresolved"], "tags": []}}
    ])[0]["id"] == "u1"
