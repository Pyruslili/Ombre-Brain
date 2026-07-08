import pytest

from bucket_manager import BucketManager


@pytest.mark.asyncio
async def test_weight_update_can_preserve_last_active(test_config):
    manager = BucketManager(test_config)
    bucket_id = await manager.create(
        content="一条会被手动降权的记忆",
        tags=[],
        importance=8,
        domain=["memory"],
        valence=0.5,
        arousal=0.8,
        name="降权测试",
    )

    before = await manager.get(bucket_id)
    original_last_active = before["metadata"].get("last_active")

    updated = await manager.update(
        bucket_id,
        importance=3,
        arousal=0.25,
        activation_count=1,
        _preserve_last_active=True,
    )

    assert updated
    after = await manager.get(bucket_id)
    meta = after["metadata"]
    assert meta["importance"] == 3
    assert meta["arousal"] == 0.25
    assert meta["activation_count"] == 1
    assert meta.get("last_active") == original_last_active
