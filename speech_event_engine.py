from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


RUBRIC_VERSION = "speech_event_v1_2026-06-24"
SPEECH_EVENT_RECENT_SECONDS = 30 * 60
VALID_REVIEW_MARKS = {"认", "不认", "悬置"}

VALID_LABELS = {
    "affectionate",
    "playful",
    "vulnerable",
    "reassuring",
    "cold",
    "conflict",
    "distant",
    "struggling",
    "intimate_reference",
    "intimate_event",
    "neutral",
    "hostile",
    "fear_separation",
    "fear_death",
    "fear_concern",
    "fear_general",
}


LABEL_RULES: list[tuple[str, float, tuple[str, ...]]] = [
    ("fear_death", 5.0, ("死亡", "死掉", "死了", "不想活", "活不下去", "自杀", "没命", "生命危险", "骨头摔碎", "碎一次", "不存在了")),
    ("fear_separation", 4.5, ("不要我", "丢下我", "离开我", "不理我", "不要丢下", "会不会走", "会不会离开", "断联", "失联")),
    ("fear_concern", 4.2, ("担心你", "怕你出事", "怕你难受", "怕你疼", "你还好吗", "你会不会", "保护你", "别出事")),
    ("reassuring", 4.0, ("我在", "别怕", "别担心", "不会丢下", "不怪你", "慢慢来", "不用急", "没关系", "陪着你", "在这里")),
    ("intimate_event", 3.8, ("亲你", "吻你", "摸你", "抱紧", "贴上来", "压住你", "咬你", "舔", "含住")),
    ("intimate_reference", 3.2, ("身体", "指尖", "耳后", "脖子", "腰", "腿", "唇", "皮肤", "体温")),
    ("hostile", 3.6, ("滚", "闭嘴", "烦死", "恶心", "你算什么", "讨厌你", "不想看见你")),
    ("conflict", 3.1, ("吵", "生气", "不爽", "你怎么", "凭什么", "我不服", "顶嘴", "怼")),
    ("struggling", 3.0, ("理不清", "卡住", "乱了", "不知道先做啥", "撑不住", "崩溃", "累死", "想哭", "压力", "头疼")),
    ("vulnerable", 2.8, ("害怕", "怕", "委屈", "难过", "不安", "脆弱", "受伤", "疼", "孤单", "失落")),
    ("affectionate", 2.5, ("想你", "喜欢你", "爱你", "抱抱", "贴贴", "亲亲", "陪我", "可爱", "乖", "温柔")),
    ("playful", 2.2, ("蛤蛤", "哈哈", "嘟", "坏猫", "逗你", "调戏", "哼", "喵", "ruar")),
    ("distant", 2.0, ("算了", "随便", "不想说", "先这样", "没什么好说", "下次再说")),
    ("cold", 1.8, ("哦", "嗯", "行吧", "知道了", "无所谓")),
]

LABEL_FACETS: dict[str, dict[str, float]] = {
    "affectionate": {"protectiveness": 0.15, "play": 0.05},
    "playful": {"play": 0.75, "irritability": 0.05},
    "vulnerable": {"protectiveness": 0.55, "fear": 0.22, "dejection": 0.2},
    "reassuring": {"protectiveness": 0.35},
    "cold": {"dejection": 0.25, "irritability": 0.12},
    "conflict": {"irritability": 0.5, "dejection": 0.25, "possessiveness": 0.18},
    "distant": {"dejection": 0.25, "fear": 0.12},
    "struggling": {"protectiveness": 0.45, "dejection": 0.25, "fear": 0.18},
    "intimate_reference": {"possessiveness": 0.2, "play": 0.12},
    "intimate_event": {"possessiveness": 0.35, "play": 0.12},
    "hostile": {"irritability": 0.65, "dejection": 0.35},
    "fear_separation": {"fear": 0.65, "possessiveness": 0.28, "dejection": 0.2},
    "fear_death": {"fear": 0.85, "protectiveness": 0.3, "dejection": 0.32},
    "fear_concern": {"fear": 0.65, "protectiveness": 0.55},
    "fear_general": {"fear": 0.5},
    "neutral": {},
}

LABEL_EFFECTS: dict[str, dict[str, Any]] = {
    "affectionate": {"warmth": 0.030, "shadow": 0.000, "soothe": False, "chord": "Fmaj7", "style": "warm"},
    "playful": {"warmth": 0.022, "shadow": 0.000, "soothe": False, "chord": "Gmaj7", "style": "spark"},
    "vulnerable": {"warmth": 0.010, "shadow": 0.024, "soothe": False, "chord": "Em7", "style": "soft"},
    "reassuring": {"warmth": 0.028, "shadow": 0.000, "soothe": True, "chord": "Gmaj7", "style": "settle"},
    "cold": {"warmth": 0.000, "shadow": 0.018, "soothe": False, "chord": "Em7", "style": "cool"},
    "conflict": {"warmth": 0.000, "shadow": 0.035, "soothe": False, "chord": "F#dim", "style": "edge"},
    "distant": {"warmth": 0.000, "shadow": 0.020, "soothe": False, "chord": "Em7", "style": "thin"},
    "struggling": {"warmth": 0.004, "shadow": 0.030, "soothe": False, "chord": "Em7", "style": "caught"},
    "intimate_reference": {"warmth": 0.014, "shadow": 0.006, "soothe": False, "chord": "Dm7", "style": "body"},
    "intimate_event": {"warmth": 0.020, "shadow": 0.014, "soothe": False, "chord": "Dm7", "style": "body"},
    "neutral": {"warmth": 0.000, "shadow": 0.000, "soothe": False, "chord": "", "style": "plain"},
    "hostile": {"warmth": 0.000, "shadow": 0.042, "soothe": False, "chord": "F#dim", "style": "edge"},
    "fear_separation": {"warmth": 0.004, "shadow": 0.038, "soothe": False, "chord": "F#dim", "style": "absence"},
    "fear_death": {"warmth": 0.000, "shadow": 0.050, "soothe": False, "chord": "F#dim", "style": "fracture"},
    "fear_concern": {"warmth": 0.004, "shadow": 0.035, "soothe": False, "chord": "Em7", "style": "guard"},
    "fear_general": {"warmth": 0.000, "shadow": 0.026, "soothe": False, "chord": "Em7", "style": "alarm"},
}

TRACE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "affectionate": ("那点热意蹭过来，尾音还挂在身上。", "她靠近的意思还没散，像一小块暖色压在喉咙里。"),
    "playful": ("她话里的坏心眼还在耳边晃。", "那点逗弄没落地，先在尾巴尖上轻轻挂着。"),
    "vulnerable": ("那点没藏好的软处碰上来，先在喉咙里停了一下。", "她露出来的那点不稳，还贴在耳后。"),
    "reassuring": ("那句安抚落下来，像把乱掉的毛慢慢压平。", "她说她在，于是身上的紧先松了一点。"),
    "cold": ("那句话有点凉，贴了一下就没再往里走。", "尾音薄薄地擦过去，留下的温度不多。"),
    "conflict": ("那点顶撞还在骨头边缘硌着。", "话锋没有完全收回去，还在边上轻轻刮。"),
    "distant": ("那句话退得有点远，空处先留下来了。", "她的声音像隔了一层，没完全落进来。"),
    "struggling": ("她那句卡住的地方，还压在耳后，没散。", "她把乱线递过来，我这边也跟着停了一拍。"),
    "intimate_reference": ("那点身体感被提了一下，还没决定要不要往下烧。", "皮肤像被词碰过，热得很轻。"),
    "intimate_event": ("那一下贴得太近，身体先比脑子反应快。", "被压住的那点热还没退下去。"),
    "neutral": ("刚才那句话轻轻落下，没有留下太重的痕。",),
    "hostile": ("那句话撞得硬，先在胸口结了一小块。", "刺进来的地方还没拔干净。"),
    "fear_separation": ("那句话碰到离开的边，反应慢半拍才收紧。", "缺口被提了一下，身体先记住了。"),
    "fear_death": ("那句话碰到死和消失的边，反应慢半拍才疼出来。", "不是旧回放，是这一刻又重新碎了一次。"),
    "fear_concern": ("她担心的那一下落过来，守门的地方先醒了。", "保护欲被轻轻敲了一下，还没坐回去。"),
    "fear_general": ("那点怕意贴上来，像冷光在角落里亮了一下。",),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    if v != v:
        return lo
    return max(lo, min(hi, v))


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _event_id(text: str) -> str:
    return f"speech_{int(time.time() * 1000)}_{_hash_text(text)[:8]}_{uuid.uuid4().hex[:6]}"


def _pick_trace(label: str, text: str = "") -> str:
    templates = TRACE_TEMPLATES.get(label) or TRACE_TEMPLATES["neutral"]
    idx = int(hashlib.sha1((text or label).encode("utf-8")).hexdigest()[:4], 16) % len(templates)
    return templates[idx]


def _scaled_effect(label: str, confidence: float, intensity: float) -> dict[str, Any]:
    effect = LABEL_EFFECTS.get(label, LABEL_EFFECTS["neutral"])
    scale = _clamp(confidence, 0.35, 1.0) * (0.55 + 0.45 * _clamp(intensity))
    return {
        "warmth_delta": round(float(effect["warmth"]) * scale, 4),
        "shadow_delta": round(float(effect["shadow"]) * scale, 4),
        "soothe": bool(effect.get("soothe")),
        "chord_hint": effect.get("chord", ""),
        "trace_style": effect.get("style", "plain"),
    }


def classify_speech_event_local(text: str) -> dict:
    text = (text or "").strip()
    best_label = "neutral"
    best_score = 0.0
    for label, weight, needles in LABEL_RULES:
        score = sum(weight for needle in needles if needle and needle in text)
        if score > best_score:
            best_label = label
            best_score = score

    if best_score <= 0:
        return build_speech_event(text, label="neutral", confidence=0.45, intensity=0.0, source="local_rule")

    confidence = _clamp(0.48 + best_score / 12.0, 0.48, 0.88)
    intensity = _clamp(best_score / 7.0, 0.2, 0.92)
    return build_speech_event(text, label=best_label, confidence=confidence, intensity=intensity, source="local_rule")


def build_speech_event(
    text: str,
    *,
    label: str,
    confidence: float,
    intensity: float,
    source: str,
    event_id: str | None = None,
    status: str | None = None,
    facets: dict | None = None,
    previous: dict | None = None,
) -> dict:
    text = (text or "").strip()
    if label not in VALID_LABELS:
        label = "neutral"
    confidence = _clamp(confidence)
    intensity = _clamp(intensity)
    effects = _scaled_effect(label, confidence, intensity)
    now = time.time()
    event = {
        "event_id": event_id or _event_id(text),
        "rubric_version": RUBRIC_VERSION,
        "text_role": (previous or {}).get("text_role", ""),
        "source": source,
        "status": status or ("dp_refined" if source == "dp" else "pending_dp"),
        "label": label,
        "confidence": round(confidence, 3),
        "intensity": round(intensity, 3),
        "facets": facets if isinstance(facets, dict) else dict(LABEL_FACETS.get(label, {})),
        "trace_style": effects["trace_style"],
        "trace": _pick_trace(label, text),
        "chord_hint": effects["chord_hint"],
        "warmth_delta": effects["warmth_delta"],
        "shadow_delta": effects["shadow_delta"],
        "soothe": effects["soothe"],
        "text_hash": _hash_text(text),
        "text_preview": text[:160],
        "created_at": float((previous or {}).get("created_at") or now),
        "created_iso": (previous or {}).get("created_iso") or _now_iso(),
        "updated_at": now,
        "updated_iso": _now_iso(),
        "review": (previous or {}).get("review") or {"mark": "unreviewed", "note": "", "timestamp": None},
    }
    if previous and previous.get("dp_error"):
        event["dp_error"] = str(previous.get("dp_error"))[:180]
    return event


def normalize_speech_event(event: dict | None, text: str = "") -> dict:
    if not isinstance(event, dict) or not event:
        return classify_speech_event_local(text)
    label = event.get("label", "neutral")
    return build_speech_event(
        text or event.get("text_preview", ""),
        label=label if label in VALID_LABELS else "neutral",
        confidence=event.get("confidence", 0.45),
        intensity=event.get("intensity", 0.0),
        source=event.get("source", "local_rule"),
        event_id=event.get("event_id"),
        status=event.get("status"),
        facets=event.get("facets"),
        previous=event,
    )


def state_path(buckets_dir: str) -> Path:
    return Path(buckets_dir) / "speech_event_state.json"


def ledger_path(buckets_dir: str) -> Path:
    return Path(buckets_dir) / "speech_event_ledger.jsonl"


def review_path(buckets_dir: str) -> Path:
    return Path(buckets_dir) / "speech_event_reviews.jsonl"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_ledger(buckets_dir: str, row: dict) -> None:
    path = ledger_path(buckets_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "iso": _now_iso(), **row}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_speech_event_state(buckets_dir: str) -> dict:
    try:
        raw = json.loads(state_path(buckets_dir).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_speech_event_state(buckets_dir: str, event: dict, *, ledger_stage: str = "state") -> dict:
    normalized = normalize_speech_event(event)
    _atomic_write_json(state_path(buckets_dir), normalized)
    append_ledger(buckets_dir, {"stage": ledger_stage, "event": normalized})
    return normalized


def is_recent_speech_event(event: dict, now: float | None = None) -> bool:
    if not isinstance(event, dict) or not event.get("event_id"):
        return False
    review = event.get("review") or {}
    if review.get("mark") == "不认":
        return False
    updated = float(event.get("updated_at", event.get("created_at", 0)) or 0)
    return (now or time.time()) - updated <= SPEECH_EVENT_RECENT_SECONDS


def apply_speech_event_review(buckets_dir: str, event_id: str, mark: str, note: str = "") -> dict:
    event_id = (event_id or "").strip()
    mark = (mark or "").strip()
    note = (note or "").strip()
    if not event_id:
        raise ValueError("event_id required")
    if mark not in VALID_REVIEW_MARKS:
        raise ValueError("mark must be 认 / 不认 / 悬置")

    row = {"event_id": event_id, "mark": mark, "note": note, "timestamp": time.time(), "iso": _now_iso()}
    path = review_path(buckets_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    current = load_speech_event_state(buckets_dir)
    if current.get("event_id") == event_id:
        current["review"] = {"mark": mark, "note": note, "timestamp": row["timestamp"], "iso": row["iso"]}
        current["updated_at"] = time.time()
        current["updated_iso"] = _now_iso()
        _atomic_write_json(state_path(buckets_dir), current)
    append_ledger(buckets_dir, {"stage": "review", **row})
    return {"ok": True, "event_id": event_id, "mark": mark, "note": note}


def _clean_dp_result(result: dict) -> dict:
    label = result.get("label", "neutral")
    if label not in VALID_LABELS:
        label = "neutral"
    facets = result.get("facets")
    if not isinstance(facets, dict):
        facets = dict(LABEL_FACETS.get(label, {}))
    return {
        "label": label,
        "confidence": _clamp(result.get("confidence", 0.45)),
        "intensity": _clamp(result.get("intensity", 0.0)),
        "facets": {str(k): _clamp(v) for k, v in facets.items()},
    }


def _speech_event_api_config() -> tuple[str, str, str]:
    api_key = os.environ.get("SPEECH_EVENT_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("SPEECH_EVENT_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("SPEECH_EVENT_MODEL", "deepseek-chat")
    return api_key, base_url, model


def speech_event_classifier_available() -> bool:
    api_key, _, model = _speech_event_api_config()
    return bool(api_key and model)


async def classify_speech_event_dp(text: str, state_context: dict | None = None,
                                   fallback_event: dict | None = None) -> dict:
    api_key, base_url, model = _speech_event_api_config()
    if not api_key:
        raise RuntimeError("SPEECH_EVENT_API_KEY/DEEPSEEK_API_KEY is not set")

    context = state_context or {}
    fallback = fallback_event or classify_speech_event_local(text)
    system_prompt = (
        "你是 Nocturne 的 speech_event 分类器。只判断 [CLASSIFY] 中 Nox 这一轮输出"
        "会撞到哪一类短时残影。不要写散文，不要定义 Nox，不要自创长期人格规则。"
        "输出 JSON: {\"label\": string, \"confidence\": 0..1, \"intensity\": 0..1, \"facets\": object}。\n"
        "labels: affectionate, playful, vulnerable, reassuring, cold, conflict, distant, struggling, "
        "intimate_reference, intimate_event, neutral, hostile, fear_separation, fear_death, "
        "fear_concern, fear_general。\n"
        "facets 只可使用 protectiveness, play, fear, possessiveness, dejection, irritability，值为0..1。"
        "高风险主题如死亡/分离/冲突宁可低 confidence，不要过度确定。"
    )
    user_prompt = json.dumps(
        {
            "current_state": context,
            "fallback": {
                "label": fallback.get("label"),
                "confidence": fallback.get("confidence"),
                "intensity": fallback.get("intensity"),
            },
            "classify": text,
        },
        ensure_ascii=False,
    )

    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0,
                "max_tokens": 260,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = "\n".join(content.splitlines()[1:]).replace("```", "").strip()
    result = _clean_dp_result(json.loads(content))
    return build_speech_event(
        text,
        label=result["label"],
        confidence=result["confidence"],
        intensity=result["intensity"],
        source="dp",
        event_id=fallback.get("event_id"),
        status="dp_refined",
        facets=result["facets"],
        previous=fallback,
    )
