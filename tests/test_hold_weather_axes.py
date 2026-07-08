import pytest


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
