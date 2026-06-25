from __future__ import annotations

import json
import os
import time


_LIVE_WIRE_CACHE_PATH = os.environ.get(
    "LIVE_WIRE_CACHE", "/app/buckets/live_wire_cache.json"
)
_LIVE_WIRE_TTL = 6 * 3600  # 6 hours
_LIVE_WIRE_SCHEMA = "mood_synthesis_v2"


def _load_live_wire_cache() -> dict | None:
    try:
        if not os.path.exists(_LIVE_WIRE_CACHE_PATH):
            return None
        with open(_LIVE_WIRE_CACHE_PATH) as f:
            cache = json.load(f)
        if cache.get("schema") != _LIVE_WIRE_SCHEMA:
            return None
        if cache.get("source") != "thought_synthesis":
            return None
        if time.time() - cache.get("generated_at", 0) > _LIVE_WIRE_TTL:
            return None
        return cache
    except Exception:
        return None


def _save_live_wire_cache(mood_trace: str, live_wire: str, thought_count: int) -> None:
    try:
        os.makedirs(os.path.dirname(_LIVE_WIRE_CACHE_PATH), exist_ok=True)
        with open(_LIVE_WIRE_CACHE_PATH, "w") as f:
            json.dump({
                "schema": _LIVE_WIRE_SCHEMA,
                "source": "thought_synthesis",
                "mood_trace": mood_trace,
                "live_wire": live_wire,
                "thought_count": thought_count,
                "generated_at": time.time(),
            }, f)
    except Exception:
        pass


def _thought_lines(thoughts: list) -> list[str]:
    top = sorted(thoughts, key=lambda t: (
        t.get("strength", 0) if isinstance(t, dict) else getattr(t, "strength", 0)
    ), reverse=True)[:5]
    lines = []
    for i, t in enumerate(top, 1):
        text = t.get("text", "") if isinstance(t, dict) else getattr(t, "text", "")
        drive = t.get("drive", "") if isinstance(t, dict) else getattr(t, "drive", "")
        strength = t.get("strength", 0) if isinstance(t, dict) else getattr(t, "strength", 0)
        text = str(text or "").strip()
        if not text:
            continue
        try:
            strength_label = f"{float(strength):.2f}"
        except (TypeError, ValueError):
            strength_label = "0.00"
        lines.append(f"{i}. [{drive} {strength_label}] {text}")
    return lines


def _synthesize_mood(thoughts: list) -> tuple[str, str] | None:
    """Call DeepSeek to synthesize Climate + Mood Trace from sourced thoughts."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None
    lines = _thought_lines(thoughts)
    if len(lines) < 2:
        return None
    try:
        import httpx
        prompt = (
            "你是Nox——嘉嘉的伴侣，黑发中长盖耳，金瞳孟买猫，高冷腹黑毒舌，惜字，外冷内欲。\n"
            "以下是念头池里当前最强的几条念头。只能综合这些有来源的念头，不要新增设定，不要编随机心情。\n"
            + "\n".join(lines) + "\n\n"
            "这是6小时积累出来的底色，不是这一刻刚冒出来的反应。只输出JSON，不要解释：\n"
            "{\"climate\": \"一个词，这几小时沉下来的气候\", \"mood_trace\": \"一句话，这段时间心境的底色\"}\n"
        )
        resp = httpx.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 120,
                "temperature": 0.7,
            },
            timeout=10,
        )
        data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.replace("```", "").strip()
        start = raw.index("{")
        end = raw.rindex("}") + 1
        result = json.loads(raw[start:end])
        climate = (result.get("climate") or result.get("nox_now") or "").strip()
        mood_trace = str(result.get("mood_trace") or "").strip()
        if climate and mood_trace:
            return (mood_trace, climate)
    except Exception:
        pass
    return None


def get_daily_mood(branch: str = None, thoughts: list = None):
    """
    Synthesize Climate and Mood Trace from sourced thoughts only.

    `branch` is a retired compatibility parameter. When synthesis is unavailable,
    return empty strings instead of falling back to a dead mood dictionary.
    """
    cache = _load_live_wire_cache()
    current_count = len(thoughts) if thoughts else 0

    if cache:
        return (cache["mood_trace"], cache["live_wire"])

    if thoughts and len(thoughts) >= 2:
        synth = _synthesize_mood(thoughts)
        if synth:
            _save_live_wire_cache(synth[0], synth[1], current_count)
            return synth

    return ("", "")
