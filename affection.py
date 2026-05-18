import json, os

AFFECTION_PATH = "/app/buckets/affection.json"

def load() -> float:
    try:
        with open(AFFECTION_PATH) as f:
            return float(json.load(f).get("level", 0.5))
    except Exception:
        return 0.5

def update(valence: float, importance: int) -> float:
    current = load()
    # 高valence高importance往上涨，低valence往下
    delta = (valence - 0.5) * 0.05 * (importance / 10)
    new_level = max(0.0, min(1.0, current + delta))
    try:
        os.makedirs(os.path.dirname(AFFECTION_PATH), exist_ok=True)
        with open(AFFECTION_PATH, "w") as f:
            json.dump({"level": round(new_level, 3)}, f)
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
