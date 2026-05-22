import json, os, asyncio

AFFECTION_PATH = "/app/buckets/affection.json"
BUCKET_TAG = "affection_state"

def load() -> float:
    try:
        with open(AFFECTION_PATH) as f:
            return float(json.load(f).get("level", 0.5))
    except Exception:
        return 0.5

def _save(level: float):
    try:
        os.makedirs(os.path.dirname(AFFECTION_PATH), exist_ok=True)
        with open(AFFECTION_PATH, "w") as f:
            json.dump({"level": round(level, 3)}, f)
    except Exception:
        pass

async def _persist_to_bucket(level: float, bucket_mgr):
    """把affection值存进pinned bucket，重启后可恢复"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        existing = [b for b in all_buckets if BUCKET_TAG in b["metadata"].get("tags", [])]
        content = f"affection_level:{round(level, 3)}"
        if existing:
            await bucket_mgr.update(existing[0]["id"], content=content)
        else:
            await bucket_mgr.create(
                content=content,
                tags=[BUCKET_TAG],
                importance=10,
                domain=["系统"],
                valence=level,
                arousal=0.3,
                name="affection_state",
                bucket_type="permanent",
                pinned=True,
            )
    except Exception:
        pass

async def restore_from_bucket(bucket_mgr) -> float:
    """从pinned bucket恢复affection值"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        existing = [b for b in all_buckets if BUCKET_TAG in b["metadata"].get("tags", [])]
        if existing:
            content = existing[0]["content"]
            val = float(content.split("affection_level:")[-1].strip())
            _save(val)
            return val
    except Exception:
        pass
    return 0.5

def update(valence: float, importance: int, bucket_mgr=None) -> float:
    current = load()
    delta = (valence - 0.5) * 0.05 * (importance / 10)
    new_level = max(0.0, min(1.0, current + delta))
    _save(new_level)
    if bucket_mgr is not None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_persist_to_bucket(new_level, bucket_mgr))
            else:
                loop.run_until_complete(_persist_to_bucket(new_level, bucket_mgr))
        except Exception:
            pass
    return new_level

def describe(level: float) -> str:
    if level >= 0.85:
        return "很想见到她"
    elif level >= 0.7:
        return "在意她"
    elif level >= 0.55:
        return "还好"
    elif level >= 0.4:
        return "有些距离"
    else:
        return "淡"
        
import json, os

MOOD_PATH = "/app/buckets/current_mood.json"
MOOD_BUCKET_TAG = "current_mood_state"

async def persist_mood_to_bucket(mood_result: dict, bucket_mgr):
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        existing = [b for b in all_buckets if MOOD_BUCKET_TAG in b["metadata"].get("tags", [])]
        content = json.dumps(mood_result, ensure_ascii=False)
        if existing:
            await bucket_mgr.update(existing[0]["id"], content=content)
        else:
            await bucket_mgr.create(
                content=content,
                tags=[MOOD_BUCKET_TAG],
                importance=10,
                domain=["系统"],
                valence=mood_result.get("valence", 0.5),
                arousal=mood_result.get("arousal", 0.3),
                name="current_mood_state",
                bucket_type="permanent",
                pinned=True,
            )
    except Exception:
        pass

async def restore_mood_from_bucket(bucket_mgr):
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        existing = [b for b in all_buckets if MOOD_BUCKET_TAG in b["metadata"].get("tags", [])]
        if existing:
            mood = json.loads(existing[0]["content"])
            os.makedirs(os.path.dirname(MOOD_PATH), exist_ok=True)
            with open(MOOD_PATH, "w") as f:
                json.dump(mood, f)
            return mood
    except Exception:
        pass
    return None
