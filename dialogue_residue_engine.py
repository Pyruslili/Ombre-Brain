from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from desire_engine import DRIVE_EVENT_SCHEMA, DRIVE_KEYS, normalize_drive_key, normalize_drive_event_brain


RUBRIC_VERSION = "dialogue_residue_v1_2026-06-27"
MAX_INTENSITY = 0.40
VALID_ANCHORS = {"jiajia", "house", "self", "boundary", "outside", "memory", "none"}
VALID_TARGETS = {"jiajia", "nox_self", "cat_house", "external", "boundary", "memory"}
VALID_TIME_MODES = {"present", "residue", "unfinished"}
VALID_GROUNDING = {"实", "悬", "空"}


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


def _hash_window(messages: list[dict]) -> str:
    packed = json.dumps(
        [{"role": m.get("role"), "text": m.get("text"), "ts": m.get("ts")} for m in messages],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()[:18]


def state_path(buckets_dir: str) -> Path:
    return Path(buckets_dir) / "dialogue_residue_state.json"


def ledger_path(buckets_dir: str) -> Path:
    return Path(buckets_dir) / "dialogue_residue_ledger.jsonl"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_dialogue_residue_ledger(buckets_dir: str, row: dict) -> None:
    path = ledger_path(buckets_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(row)
    payload.setdefault("timestamp", time.time())
    payload.setdefault("iso", _now_iso())
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_dialogue_residue_state(buckets_dir: str) -> dict:
    path = state_path(buckets_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_dialogue_residue_state(buckets_dir: str, state: dict, *, ledger_stage: str = "state") -> dict:
    normalized = normalize_dialogue_residue_event(state)
    _atomic_write_json(state_path(buckets_dir), normalized)
    append_dialogue_residue_ledger(buckets_dir, {"stage": ledger_stage, "event": normalized})
    return normalized


def dialogue_residue_available() -> bool:
    api_key, _, model = _dialogue_residue_api_config()
    return bool(api_key and model)


def _dialogue_residue_api_config() -> tuple[str, str, str]:
    api_key = (
        os.environ.get("DIALOGUE_RESIDUE_API_KEY")
        or os.environ.get("SPEECH_EVENT_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY", "")
    )
    base_url = os.environ.get("DIALOGUE_RESIDUE_BASE_URL", os.environ.get("SPEECH_EVENT_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
    model = os.environ.get("DIALOGUE_RESIDUE_MODEL", os.environ.get("SPEECH_EVENT_MODEL", "deepseek-chat"))
    return api_key, base_url, model


def normalize_dialogue_messages(messages: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for raw in messages if isinstance(messages, list) else []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = str(raw.get("text") or raw.get("content") or "").strip()
        if not text:
            continue
        cleaned.append(
            {
                "role": role,
                "speaker": "嘉嘉" if role == "user" else "Nox",
                "text": text[:1600],
                "ts": str(raw.get("ts") or raw.get("created_at") or "").strip()[:80],
            }
        )
    return cleaned[-4:]


def normalize_dialogue_residue_event(event: dict | None, *, messages: list[dict] | None = None,
                                     window_id: str | None = None) -> dict:
    event = event if isinstance(event, dict) else {}
    msg = normalize_dialogue_messages(messages or event.get("messages") or [])
    wid = str(window_id or event.get("window_id") or _hash_window(msg)).strip()
    primary = normalize_drive_key(event.get("primary_drive"), "")
    if primary not in DRIVE_KEYS:
        primary = ""
    confidence = _clamp(event.get("confidence"), 0.0, 1.0)
    intensity = _clamp(event.get("intensity"), 0.0, MAX_INTENSITY)
    agency = _clamp(event.get("agency"), 0.25, 0.75)
    secondary = event.get("secondary_drives")
    if not isinstance(secondary, dict):
        secondary = {}
    secondary = {
        normalize_drive_key(k, ""): round(_clamp(v, 0.0, MAX_INTENSITY), 4)
        for k, v in secondary.items()
        if normalize_drive_key(k, "") in DRIVE_KEYS
    }

    brain = event.get("brain") if isinstance(event.get("brain"), dict) else {}
    brain = {
        **brain,
        "source": "dialogue_residue",
    }
    target = str(brain.get("target") or "").strip()
    if target not in VALID_TARGETS:
        target = "nox_self"
    time_mode = str(brain.get("time_mode") or "").strip()
    if time_mode not in VALID_TIME_MODES:
        time_mode = "present"
    grounding = str(brain.get("grounding") or "").strip()
    if grounding not in VALID_GROUNDING:
        grounding = "悬"
    anchor = str(brain.get("anchor_target") or "").strip()
    if anchor not in VALID_ANCHORS:
        anchor = "none"
    brain.update({"target": target, "time_mode": time_mode, "grounding": grounding, "anchor_target": anchor})
    brain = normalize_drive_event_brain(brain)

    evidence = event.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    evidence = [str(x).strip()[:180] for x in evidence if str(x).strip()][:3]

    status = str(event.get("status") or "").strip()
    if not primary or intensity <= 0 or confidence < 0.45:
        status = status or "no_signal"
    else:
        status = status or "dp_refined"

    return {
        "schema_version": DRIVE_EVENT_SCHEMA,
        "source": "dialogue_residue",
        "event_label": str(event.get("event_label") or "dialogue_residue").strip()[:80],
        "primary_drive": primary,
        "secondary_drives": secondary,
        "intensity": round(intensity, 4),
        "confidence": round(confidence, 3),
        "agency": round(agency, 3),
        "brain": brain,
        "evidence": evidence,
        "thoughts": [],
        "messages": msg,
        "window_id": wid,
        "rubric_version": RUBRIC_VERSION,
        "status": status,
        "created_at": event.get("created_at") or time.time(),
        "created_iso": event.get("created_iso") or _now_iso(),
    }


async def classify_dialogue_residue_dp(messages: list[dict], state_context: dict | None = None,
                                       window_id: str | None = None) -> dict:
    api_key, base_url, model = _dialogue_residue_api_config()
    if not api_key:
        raise RuntimeError("DIALOGUE_RESIDUE_API_KEY/SPEECH_EVENT_API_KEY/DEEPSEEK_API_KEY is not set")

    normalized_messages = normalize_dialogue_messages(messages)
    if len(normalized_messages) < 4:
        return normalize_dialogue_residue_event(
            {"status": "insufficient_context", "confidence": 0.0, "intensity": 0.0},
            messages=normalized_messages,
            window_id=window_id,
        )

    system_prompt = (
        "你是 Nocturne 的 dialogue_residue 分析器。任务：判断最近一小窗嘉嘉与 Nox 的对话，"
        "作为当前对话残留，对 Nox 的 drive_event_v2 有什么轻量影响。\n"
        "必须沿用 CLI analyzer 的偏好：真实、窄口径、少推、证据优先；不要写散文，"
        "不要生成 Nox 自己的 thought，不要解释长期人格，不要把礼貌/接话误判成 attachment。\n"
        "如果这一窗只是任务执行、系统维护、普通确认，输出 primary_drive 为空、intensity 0。\n"
        "输出严格 JSON，字段：schema_version, source, event_label, primary_drive, secondary_drives, "
        "intensity, confidence, agency, brain, evidence, thoughts。\n"
        "source 必须是 dialogue_residue；schema_version 必须是 drive_event_v2；thoughts 必须是 []。\n"
        "primary_drive 只能是 attachment, libido, possessiveness, reflection, stewardship, curiosity, "
        "social, fatigue, stress 或空字符串。intensity 上限 0.40，日常轻推通常 0.04-0.16。\n"
        "brain 必须含 source, target, time_mode, grounding, closeness_pull, territorial_alarm, inward_pull, "
        "house_need, novelty_pull, expression_pressure, energy_cost, tension_load, discernment_alarm, "
        "release_pressure, anchor_target。\n"
        "target: jiajia/nox_self/cat_house/external/boundary/memory。time_mode: present/residue/unfinished。"
        "grounding: 实/悬/空。anchor_target: jiajia/house/self/boundary/outside/memory/none。\n"
        "判断偏好：\n"
        "- 双方上下文一起看，嘉嘉的话是外部信号，Nox 的回复只作为是否被接住/是否有阻力的证据。\n"
        "- 好奇/反思/守护/社交要比 attachment/libido/possessiveness 更容易轻推。\n"
        "- 压力只在真正有卡住、风险、冲突、负荷时推；存在性讨论不自动等于压力。\n"
        "- possessiveness 要有边界、归属、占位、护住、显式靠近语义，不要靠氛围偷渡。\n"
    )
    user_prompt = json.dumps(
        {
            "current_state": state_context or {},
            "window_id": window_id or _hash_window(normalized_messages),
            "messages": normalized_messages,
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
                "max_tokens": 760,
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
    event = json.loads(content)
    return normalize_dialogue_residue_event(event, messages=normalized_messages, window_id=window_id)
