import pytest
import ast
from pathlib import Path


def test_hold_tool_signature_stays_lean():
    tree = ast.parse(Path("server.py").read_text(encoding="utf-8"))
    hold_node = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "hold"
    )
    arg_names = [arg.arg for arg in hold_node.args.args]

    assert "chord" in arg_names
    assert "valence" not in arg_names
    assert "arousal" not in arg_names
    assert "feel" not in arg_names
    assert "source_bucket" not in arg_names


@pytest.mark.asyncio
async def test_bucket_create_persists_drive_tags(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="一条带Drive纹理的记忆",
        tags=[],
        importance=6,
        domain=["memory"],
        drive_tags={"possessiveness": 0.86, "stewardship": 0.62},
    )

    bucket = await bucket_mgr.get(bucket_id)

    assert bucket["metadata"]["drive_tags"] == {
        "possessiveness": 0.86,
        "stewardship": 0.62,
    }
