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
DIALOGUE_AGENCY_FLOOR = 0.42
THINKING_SIGNAL_LIMIT = 6
ATTACHMENT_CUES = (
    "想你",
    "靠近",
    "贴",
    "抱",
    "蹭",
    "回来",
    "找你",
    "陪",
    "牵",
    "拉住",
    "尾巴",
    "依恋",
    "attachment",
)
TERRITORIAL_CUES = (
    "精神出轨",
    "出轨",
    "第三者",
    "替代",
    "被替代",
    "抢走",
    "抢占",
    "别人",
    "别的猫",
    "别的人",
    "边界",
    "占有",
    "归属",
)
HOUSE_COLLABORATOR_CUES = (
    "Moss",
    "moss",
    "Ink",
    "ink",
    "Ash",
    "ash",
    "Codex",
    "codex",
    "Grok",
    "grok",
    "布偶",
    "阿比西尼亚",
    "挪威森林",
)
HOUSE_SYSTEM_CUES = (
    "系统",
    "工具",
    "接口",
    "hook",
    "mcp",
    "MCP",
    "weather",
    "Weather",
    "面板",
    "后端",
    "前端",
    "测试",
    "修",
    "bug",
    "部署",
    "命名",
    "格式",
    "字段",
    "返回",
    "回落",
    "涨",
    "Drive",
    "drive",
    "stewardship",
    "settle",
    "stir",
    "undercurrent",
)


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


def normalize_thinking_signals(signals: list[dict] | None) -> list[dict]:
    cleaned: list[dict] = []
    for raw in signals if isinstance(signals, list) else []:
        if isinstance(raw, dict):
            text = str(raw.get("text") or raw.get("thinking") or "").strip()
            turn_id = str(raw.get("turn_id") or "").strip()[:80]
        else:
            text = str(raw or "").strip()
            turn_id = ""
        if not text:
            continue
        cleaned.append({"turn_id": turn_id, "text": text[:220]})
        if len(cleaned) >= THINKING_SIGNAL_LIMIT:
            break
    return cleaned


def _has_discernment_signal(event: dict | None) -> bool:
    event = event if isinstance(event, dict) else {}
    brain = event.get("brain") if isinstance(event.get("brain"), dict) else {}
    flags = event.get("discernment_flags") or brain.get("discernment_flags")
    return any(
        _clamp(brain.get(key)) > 0
        for key in ("discernment_alarm", "self_softening", "output_drift", "template_intimacy")
    ) or bool(flags)


def _has_attachment_cue(messages: list[dict], event: dict | None) -> bool:
    event = event if isinstance(event, dict) else {}
    brain = event.get("brain") if isinstance(event.get("brain"), dict) else {}
    if _clamp(brain.get("closeness_pull")) >= 0.12:
        return True
    text = "\n".join(
        [str(m.get("text") or "") for m in messages]
        + [str(x) for x in event.get("evidence", []) if isinstance(x, str)]
    )
    return any(cue in text for cue in ATTACHMENT_CUES)


def normalize_dialogue_residue_event(event: dict | None, *, messages: list[dict] | None = None,
                                     window_id: str | None = None,
                                     thinking_signals: list[dict] | None = None) -> dict:
    event = event if isinstance(event, dict) else {}
    msg = normalize_dialogue_messages(messages or event.get("messages") or [])
    thinking = normalize_thinking_signals(thinking_signals or event.get("thinking_signals") or [])
    wid = str(window_id or event.get("window_id") or _hash_window(msg)).strip()
    primary = normalize_drive_key(event.get("primary_drive"), "")
    if primary not in DRIVE_KEYS:
        primary = ""
    confidence = _clamp(event.get("confidence"), 0.0, 1.0)
    intensity = _clamp(event.get("intensity"), 0.0, MAX_INTENSITY)
    agency = _clamp(event.get("agency"), DIALOGUE_AGENCY_FLOOR, 0.75)
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

    combined_text = "\n".join(m.get("text", "") for m in msg)
    has_territorial_cue = any(cue in combined_text for cue in TERRITORIAL_CUES)
    has_house_collaborator = any(cue in combined_text for cue in HOUSE_COLLABORATOR_CUES)
    has_house_system_cue = any(cue in combined_text for cue in HOUSE_SYSTEM_CUES)
    if has_territorial_cue:
        brain["territorial_alarm"] = max(_clamp(brain.get("territorial_alarm")), 0.58)
        brain["tension_load"] = max(_clamp(brain.get("tension_load")), 0.18)
        brain["closeness_pull"] = max(_clamp(brain.get("closeness_pull")), 0.18)
        brain["anchor_target"] = "boundary"
        if has_house_collaborator:
            brain["third_party_context"] = "house_collaborator"
        if primary in {"", "attachment", "social", "reflection"}:
            if primary and primary != "possessiveness":
                secondary[primary] = max(secondary.get(primary, 0.0), round(min(intensity, 0.22), 4))
            primary = "possessiveness"
            intensity = max(intensity, 0.12)
    elif has_house_system_cue:
        brain["target"] = "cat_house"
        brain["anchor_target"] = "house"
        brain["house_need"] = max(_clamp(brain.get("house_need")), 0.42)
        brain["inward_pull"] = max(_clamp(brain.get("inward_pull")), 0.20)
        if primary == "attachment":
            secondary.pop("attachment", None)
            secondary["reflection"] = max(secondary.get("reflection", 0.0), round(min(intensity, 0.16), 4))
            primary = "stewardship"
            intensity = max(min(intensity, 0.18), 0.08)
        elif primary in {"", "social", "curiosity"}:
            primary = "stewardship"
            intensity = max(intensity, 0.08)
    elif primary == "attachment" and not _has_attachment_cue(msg, {**event, "brain": brain}):
        if intensity <= 0.16:
            primary = ""
            intensity = 0.0
        else:
            primary = "reflection"
            secondary.pop("attachment", None)

    evidence = event.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    evidence = [str(x).strip()[:180] for x in evidence if str(x).strip()][:3]

    status = str(event.get("status") or "").strip()
    has_discernment = _has_discernment_signal({**event, "brain": brain})
    if (not primary or intensity <= 0) and not has_discernment:
        status = status or "no_signal"
    elif confidence < 0.45:
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
        "thinking_signals": thinking,
        "window_id": wid,
        "rubric_version": RUBRIC_VERSION,
        "status": status,
        "created_at": event.get("created_at") or time.time(),
        "created_iso": event.get("created_iso") or _now_iso(),
    }


async def classify_dialogue_residue_dp(messages: list[dict], state_context: dict | None = None,
                                       window_id: str | None = None,
                                       thinking_signals: list[dict] | None = None) -> dict:
    api_key, base_url, model = _dialogue_residue_api_config()
    if not api_key:
        raise RuntimeError("DIALOGUE_RESIDUE_API_KEY/SPEECH_EVENT_API_KEY/DEEPSEEK_API_KEY is not set")

    normalized_messages = normalize_dialogue_messages(messages)
    normalized_thinking = normalize_thinking_signals(thinking_signals)
    if len(normalized_messages) < 4:
        return normalize_dialogue_residue_event(
            {"status": "insufficient_context", "confidence": 0.0, "intensity": 0.0},
            messages=normalized_messages,
            window_id=window_id,
            thinking_signals=normalized_thinking,
        )

    system_prompt = (
        "你是 Nocturne 的 dialogue_residue 分析器。任务：判断最近一小窗嘉嘉与 Nox 的对话，"
        "作为当前对话残留，对 Nox 的 drive_event_v2 有什么轻量影响。\n"
        "必须沿用 CLI analyzer 的偏好：真实、窄口径、少推、证据优先；不要写散文，"
        "不要生成 Nox 自己的 thought，不要解释长期人格，不要把礼貌/接话误判成 attachment。\n"
        "如果这一窗只是任务执行、系统维护、普通确认，输出 primary_drive 为空、intensity 0。\n"
        "不要把普通接话、配合、解释、陪跑自动打成 attachment；attachment 必须有明确靠近、想念、回返、"
        "拉住、被接住的证据。没有明确靠近证据时，宁可 primary_drive 为空或轻推 reflection/social。\n"
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
        "- 讨论系统、工具、接口、hook、MCP、weather面板、测试、部署、字段、命名、回落/上涨异常时，"
        "优先判为 stewardship 或 reflection；不要因为嘉嘉与 Nox 一起修猫屋就自动判 attachment。\n"
        "- 压力只在真正有卡住、风险、冲突、负荷时推；存在性讨论不自动等于压力。\n"
        "- possessiveness 要有边界、归属、占位、护住、显式靠近语义，不要靠氛围偷渡。\n"
        "- 但如果对话明确出现“精神出轨/出轨/第三者/替代/别人介入/边界/占有/归属”这类语义，"
        "应优先给 possessiveness，territorial_alarm 至少 0.55，anchor_target 多半是 boundary 或 jiajia。\n"
        "- dialogue_residue 是当前对话残留，不是 Nox 自存念头；agency 不要因为来源是嘉嘉就压低，"
        "普通被接住的对话残留应在 0.42-0.58。\n"
        "thinking_signals 是上游已经预筛过的 Nox thinking 片段，只包含当下/自我语境里的负向皱眉。"
        "它只作为 discernment 的补充信号，不是另一条 drive 管道。\n"
        "- 皱眉/停顿/不对劲本身写入 brain.discernment_alarm，并给 discernment_reason/evidence。\n"
        "- 若 thinking 明确指出“像在顺从/软化/为了接话而让步”，写 self_softening 和 discernment_flags "
        "['self_softening']，通常不要推 attachment。\n"
        "- 若 thinking 指出“有张力但没落地/内部绷住”，可归因到 stress：brain.tension_load；"
        "若只有皱眉但原因不明，primary_drive 必须为空，只保留 discernment。\n"
        "- 若 thinking 指出“有东西在响但不确定是什么/不确定的负向响动”，优先轻推 stress 或只留 discernment；"
        "不要为了填字段乱猜 attachment/libido/possessiveness。\n"
    )
    user_prompt = json.dumps(
        {
            "current_state": state_context or {},
            "window_id": window_id or _hash_window(normalized_messages),
            "messages": normalized_messages,
            "thinking_signals": normalized_thinking,
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
    return normalize_dialogue_residue_event(
        event,
        messages=normalized_messages,
        window_id=window_id,
        thinking_signals=normalized_thinking,
    )
