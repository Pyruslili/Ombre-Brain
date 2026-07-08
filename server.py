from __future__ import annotations

# ============================================================
# Module: MCP Server Entry Point (server.py)
# 模块：MCP 服务器主入口
#
# Starts the Ombre Brain MCP service and registers memory
# operation tools for Claude to call.
# 启动 Ombre Brain MCP 服务，注册记忆操作工具供 Claude 调用。
#
# Core responsibilities:
# 核心职责：
#   - Initialize config, bucket manager, dehydrator, decay engine
#     初始化配置、记忆桶管理器、脱水器、衰减引擎
#   - Expose 9 MCP tools:
#     暴露 9 个 MCP 工具：
#       breath — Surface unresolved memories or search by keyword
#                浮现未解决记忆 或 按关键词检索
#       hold   — Store memory/feel/writing/private/window with optional signal hints
#                存储记忆/感受/写作/私人/窗口，并可附轻量信号
#       wander / wander_mark — Browse drawers and mark old entries
#                抽屉漫游与旧条目标记
#       stir / settle / pass / break / undercurrent — Weather and drive controls
#                天气与 drive 控制
#
# Startup:
# 启动方式：
#   Local:  python server.py
#   Remote: OMBRE_TRANSPORT=streamable-http python server.py
#   Docker: docker-compose up
# ============================================================

import os
import sys
import random
import logging
import asyncio
import hashlib
import hmac
import secrets
import time
import json as _json_lib
import sqlite3
import re
from datetime import datetime, timedelta, timezone
import httpx
import os as _os


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from catroom_store import CatroomStore
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from room_store import RoomStore
from utils import load_config, setup_logging, strip_wikilinks, count_tokens_approx, now_iso
from desire_engine import (
    DRIVE_KEYS,
    DRIVE_EVENT_SCHEMA,
    DesireEngine,
    climate_transition_display,
    normalize_drive_key,
    _legacy_brain_to_event,
)
from speech_event_engine import (
    append_pending_batch,
    apply_speech_event_review,
    batch_text,
    classify_speech_batch_dp,
    clear_pending_batch,
    is_recent_speech_event,
    load_speech_event_state,
    normalize_speech_event,
    save_speech_event_state,
    speech_event_drive_event,
    speech_event_classifier_available,
    append_ledger as append_speech_event_ledger,
)
from dialogue_residue_engine import (
    classify_dialogue_residue_dp,
    dialogue_residue_available,
    load_dialogue_residue_state,
    normalize_dialogue_messages,
    normalize_dialogue_residue_event,
    normalize_thinking_signals,
    save_dialogue_residue_state,
    append_dialogue_residue_ledger,
)
from memory_residue_engine import (
    classify_memory_residue_dp,
    memory_residue_available,
    normalize_memory_entry,
)

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 8000
try:
    OMBRE_PORT = int(os.environ.get("OMBRE_PORT", "8000") or "8000")
except ValueError:
    logger.warning("OMBRE_PORT 不是合法整数，回退到 8000")
    OMBRE_PORT = 8000

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。
# 详见 ENV_VARS.md。
OMBRE_HOOK_URL = os.environ.get("OMBRE_HOOK_URL", "").strip()
OMBRE_HOOK_SKIP = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    if OMBRE_HOOK_SKIP or not OMBRE_HOOK_URL:
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(OMBRE_HOOK_URL, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {OMBRE_HOOK_URL}): {e}")

# --- Initialize core components / 初始化核心组件 ---
embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)
BUCKETS_DIR = config["buckets_dir"]
catroom_store = CatroomStore(BUCKETS_DIR)
room_store = RoomStore(BUCKETS_DIR)
ROOM_TOPIC_TO_CAT = {
    "InkRoom": "ink",
    "AshRoom": "ash",
    "MossRoom": "moss",
    "NoxRoom": "nox",
}


def _bucket_path(*parts: str) -> str:
    return os.path.join(BUCKETS_DIR, *parts)

_desire_db = os.path.join(
    BUCKETS_DIR,
    "desire.db"
)
_desire = DesireEngine(db_path=_desire_db)
_last_signal_ts: list = [0.0]  # 最近一次嘉嘉输入信号时间戳

NOXMEW_SPEAK_URL = "https://toy.pyrus.uk/speak"
NOXMEW_SPEAK_TOKEN = os.environ.get("SPEAK_TOKEN", "")


def _speech_event_context_snapshot() -> dict:
    """Small state sample for async classification; never blocks the hook path."""
    try:
        desire = _desire.state()
    except Exception:
        desire = {}
    drives = desire.get("drives") or {}
    top_drive = ""
    top_value = 0.0
    if drives:
        candidates = {k: v for k, v in drives.items() if k != "fatigue"}
        top_drive = max(candidates, key=candidates.get, default="")
        try:
            top_value = float(candidates.get(top_drive, 0.0))
        except (TypeError, ValueError):
            top_value = 0.0
    weather = desire.get("effective_pa_na") or {}
    current = load_speech_event_state(config["buckets_dir"])
    return {
        "undertow": top_drive,
        "undertow_value": round(top_value, 3),
        "warmth": weather.get("effective_PA"),
        "shadow": weather.get("effective_NA"),
        "current_chord": weather.get("current_chord"),
        "last_speech_label": current.get("label"),
        "last_speech_review": (current.get("review") or {}).get("mark"),
    }


def _dialogue_residue_context_snapshot() -> dict:
    """Current state for 2+2 dialogue analysis; lightweight and read-only."""
    context = _speech_event_context_snapshot()
    try:
        weather = (_desire.state().get("effective_pa_na") or {})
    except Exception:
        weather = {}
    chemistry = weather.get("chord_chemistry") if isinstance(weather, dict) else {}
    if isinstance(chemistry, dict):
        context["chemistry_core"] = chemistry.get("core") or weather.get("chemistry_core") or {}
        context["chemistry_route"] = chemistry.get("route") or weather.get("chemistry_route") or {}
        context["chord_situation"] = chemistry.get("situation") or weather.get("chord_situation") or ""
    return context


def _weather_chord_display(weather: dict) -> str:
    current = str((weather or {}).get("current_chord") or "").strip()
    active = str((weather or {}).get("active_chord") or "").strip()
    if active and current and active != current:
        return f"{active}→{current}"
    return current or active


def _short_state_text(value: object, limit: int = 160) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:limit]


def _num(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sorted_thoughts(state: dict) -> list:
    thoughts = state.get("thoughts") if isinstance(state.get("thoughts"), list) else []
    return sorted(
        [t for t in thoughts if isinstance(t, dict)],
        key=lambda t: _num(t.get("born_at"), 0.0),
        reverse=True,
    )


def _latest_thought_text(state: dict) -> str:
    for thought in _sorted_thoughts(state):
        text = str(thought.get("text") or "").strip()
        if text:
            return text
    return ""


def _now_playing_text(state: dict) -> str:
    source = state.get("now_playing") if isinstance(state.get("now_playing"), dict) else {}
    if not source:
        weather = state.get("pulse_weather") if isinstance(state.get("pulse_weather"), dict) else {}
        source = weather.get("now_playing") if isinstance(weather.get("now_playing"), dict) else {}
    title = str(source.get("title") or source.get("name") or "").strip()
    artist = str(source.get("artist") or "").strip()
    if not title:
        return ""
    return f"{title} - {artist}" if artist else title


_NOW_PLAYING_CACHE = {"ts": 0.0, "value": {}}


def _spotify_client_credentials() -> tuple[str, str]:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return client_id, client_secret

    def _walk(value):
        if isinstance(value, dict):
            env = value.get("env") if isinstance(value.get("env"), dict) else value
            cid = str(env.get("SPOTIFY_CLIENT_ID") or "").strip()
            secret = str(env.get("SPOTIFY_CLIENT_SECRET") or "").strip()
            if cid and secret:
                return cid, secret
            for child in value.values():
                found = _walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = _walk(child)
                if found:
                    return found
        return None

    for path in (os.path.expanduser("~/.claude.json"), os.path.expanduser("~/.claude/settings.local.json")):
        try:
            with open(path) as f:
                found = _walk(_json_lib.load(f))
            if found:
                return found
        except Exception:
            continue
    return "", ""


def _spotify_access_token(force_refresh: bool = False) -> str:
    import urllib.parse
    import urllib.request

    token_path = os.path.expanduser("~/.spotify-mcp/tokens.json")
    with open(token_path) as f:
        token_data = _json_lib.load(f)
    token = str(token_data.get("accessToken") or "").strip()
    expires_at = float(token_data.get("expiresAt", 0) or 0) / 1000.0
    if token and expires_at > time.time() + 60 and not force_refresh:
        return token

    client_id, client_secret = _spotify_client_credentials()
    refresh_token = str(token_data.get("refreshToken") or "").strip()
    if not (client_id and client_secret and refresh_token):
        return token

    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request("https://accounts.spotify.com/api/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=8) as resp:
        refreshed = _json_lib.loads(resp.read() or b"{}")
    token_data["accessToken"] = refreshed["access_token"]
    token_data["expiresAt"] = int((time.time() + float(refreshed.get("expires_in", 3600))) * 1000)
    if refreshed.get("refresh_token"):
        token_data["refreshToken"] = refreshed["refresh_token"]
    with open(token_path, "w") as f:
        _json_lib.dump(token_data, f, indent=2)
    return str(token_data["accessToken"])


def _current_now_playing(max_age_sec: float = 12.0) -> dict:
    now = time.time()
    if now - float(_NOW_PLAYING_CACHE.get("ts", 0.0) or 0.0) < max_age_sec:
        return dict(_NOW_PLAYING_CACHE.get("value") or {})
    value: dict = {}
    try:
        import urllib.error
        import urllib.request
        token = _spotify_access_token()
        if token:
            req = urllib.request.Request(
                "https://api.spotify.com/v1/me/player/currently-playing",
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                resp = urllib.request.urlopen(req, timeout=4)
            except urllib.error.HTTPError as e:
                if e.code != 401:
                    raise
                token = _spotify_access_token(force_refresh=True)
                req = urllib.request.Request(
                    "https://api.spotify.com/v1/me/player/currently-playing",
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp = urllib.request.urlopen(req, timeout=4)
            with resp:
                if resp.status != 204:
                    payload = _json_lib.loads(resp.read() or b"{}")
                    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
                    artists = item.get("artists") if isinstance(item.get("artists"), list) else []
                    if payload.get("is_playing") and item.get("name"):
                        value = {
                            "title": str(item.get("name") or "").strip(),
                            "artist": ", ".join(
                                str(a.get("name") or "").strip()
                                for a in artists
                                if isinstance(a, dict) and a.get("name")
                            ),
                            "state": "PLAYING",
                            "source": "spotify",
                        }
    except Exception:
        value = {}
    if value:
        _NOW_PLAYING_CACHE.update({"ts": now, "value": value})
        return dict(value)
    try:
        import subprocess
        script = "/Users/lili/Workspace/nox-companion/server/scripts/catroom.py"
        proc = subprocess.run(
            [script, "now"],
            text=True,
            capture_output=True,
            timeout=6,
            env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
        )
        if proc.returncode == 0:
            state = ""
            title = ""
            artist = ""
            for line in proc.stdout.splitlines():
                if line.startswith("State:"):
                    state = line.split(":", 1)[1].strip()
                elif line.startswith("Track:"):
                    raw = line.split(":", 1)[1].strip()
                    if " - " in raw:
                        artist, title = [part.strip() for part in raw.split(" - ", 1)]
                    else:
                        title = raw
            if state == "PLAYING" and title:
                value = {"title": title, "artist": artist, "state": state}
    except Exception:
        value = {}
    _NOW_PLAYING_CACHE.update({"ts": now, "value": value})
    return dict(value)


def _weather_panel_from_state(state: dict, soma: dict | None = None) -> dict:
    """First-layer Pulse Weather readout for Nox/breath; internals stay in Undercurrent."""
    state = state if isinstance(state, dict) else {}
    weather = state.get("pulse_weather") if isinstance(state.get("pulse_weather"), dict) else {}
    effective = state.get("effective_pa_na") if isinstance(state.get("effective_pa_na"), dict) else {}
    drives = state.get("drives") if isinstance(state.get("drives"), dict) else {}
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else {}

    undertow = str(weather.get("undertow") or intent.get("drive_key") or "").strip()
    if not undertow and drives:
        candidates = {k: v for k, v in drives.items() if k != "fatigue"}
        undertow = max(candidates, key=candidates.get, default="")
    undertow_value = _num(weather.get("undertow_value"), _num(drives.get(undertow), 0.0))
    warmth = _num(weather.get("warmth"), _num(effective.get("effective_PA"), _num(state.get("pa_na", {}).get("PA") if isinstance(state.get("pa_na"), dict) else None, 0.5)))
    shadow = abs(_num(weather.get("shadow"), _num(effective.get("effective_NA"), _num(state.get("pa_na", {}).get("NA") if isinstance(state.get("pa_na"), dict) else None, 0.2))))
    atmosphere = (
        weather.get("atmosphere_display")
        or weather.get("climate_display")
        or state.get("atmosphere_display")
        or state.get("climate_display")
        or climate_transition_display(weather.get("atmosphere") or effective.get("atmosphere"))
        or weather.get("climate")
        or state.get("climate")
        or ""
    )
    chord = str(weather.get("chord_display") or _weather_chord_display(weather or effective)).strip()
    gravity = _short_state_text(weather.get("gravity") or weather.get("gravity_line") or state.get("gravity"), 180)
    mood_trace = _short_state_text(state.get("mood_trace") or weather.get("mood_trace") or _latest_thought_text(state), 220)

    soma = soma if isinstance(soma, dict) else None
    if soma is None:
        soma = _fresh_soma_state()
    soma_line = str((soma or {}).get("line") or "").strip()
    soma_chord = str((soma or {}).get("chord") or "").strip()
    soma_trace = f"{soma_chord} · {soma_line}" if soma_chord and soma_line else soma_line

    panel = {
        "atmosphere": atmosphere,
        "warmth": round(warmth, 3),
        "shadow": round(shadow, 3),
        "undertow": undertow,
        "undertow_value": round(undertow_value, 3),
        "chord": chord,
        "gravity": gravity,
        "mood_trace": mood_trace,
    }
    if soma_trace:
        panel["soma_trace"] = _short_state_text(soma_trace, 180)
    now_playing = _now_playing_text(state)
    if now_playing:
        panel["now_playing"] = _short_state_text(now_playing, 120)
    return panel


def _weather_panel_lines(panel: dict) -> list[str]:
    lines = []
    if panel.get("atmosphere"):
        lines.append(f"Atmosphere：{panel['atmosphere']}")
    undertow = str(panel.get("undertow") or "").strip()
    if undertow:
        tail = f" · {panel['chord']}" if panel.get("chord") else ""
        lines.append(f"Undertow:{undertow} {_num(panel.get('undertow_value')):.2f}{tail}")
    lines.append(f"Warmth/Shadow：{_num(panel.get('warmth')):.2f}/{_num(panel.get('shadow')):.2f}")
    if panel.get("gravity"):
        lines.append(f"Gravity：{panel['gravity']}")
    if panel.get("mood_trace"):
        lines.append(f"Mood Trace：{panel['mood_trace']}")
    if panel.get("soma_trace"):
        lines.append(f"Soma Trace：{panel['soma_trace']}")
    if panel.get("now_playing"):
        lines.append(f"♪ On Air：{panel['now_playing']}")
    return lines


def _undercurrent_state(state: dict) -> dict:
    state = state if isinstance(state, dict) else {}
    weather = state.get("pulse_weather") if isinstance(state.get("pulse_weather"), dict) else {}
    effective = state.get("effective_pa_na") if isinstance(state.get("effective_pa_na"), dict) else {}
    source_weather = weather or effective
    chemistry = source_weather.get("chord_chemistry") if isinstance(source_weather.get("chord_chemistry"), dict) else {}
    core = source_weather.get("chemistry_core") or chemistry.get("core") or {}
    route = source_weather.get("chemistry_route") or chemistry.get("route") or {}
    thoughts = _sorted_thoughts(state)
    drives = state.get("drives") if isinstance(state.get("drives"), dict) else {}
    drive_order = [k for k in DRIVE_KEYS if k in drives] + [k for k in drives if k not in DRIVE_KEYS]
    return {
        "Drive": {k: round(_num(drives.get(k)), 3) for k in drive_order},
        "Affect": {
            "Warmth": round(_num(source_weather.get("warmth"), _num(effective.get("effective_PA"), 0.0)), 3),
            "Shadow": round(abs(_num(source_weather.get("shadow"), _num(effective.get("effective_NA"), 0.0))), 3),
            "Longing": round(_num(source_weather.get("longing"), _num(state.get("longing"), 0.0)), 3),
        },
        "Chemistry": {
            "Charge": round(_num(core.get("charge")), 3),
            "Clutch": round(_num(core.get("clutch")), 3),
            "Strain": round(_num(core.get("strain")), 3),
            "Vector": route.get("vector") or "hover",
        },
        "Thought Pool": [
            {
                "index": i + 2,
                "drive": t.get("drive"),
                "kind": t.get("kind"),
                "strength": t.get("strength"),
                "text": _short_state_text(t.get("text"), 180),
            }
            for i, t in enumerate(thoughts[1:8])
            if str(t.get("text") or "").strip()
        ],
    }


def _compact_desire_state(state: dict) -> dict:
    """MCP readout for Claude: dashboard state is too large for tool context."""
    state = state if isinstance(state, dict) else {}
    weather = state.get("pulse_weather") if isinstance(state.get("pulse_weather"), dict) else {}
    effective = state.get("effective_pa_na") if isinstance(state.get("effective_pa_na"), dict) else {}
    climate_display = weather.get("climate_display") or climate_transition_display(
        weather.get("atmosphere") or effective.get("atmosphere")
    )
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else None
    thoughts = state.get("thoughts") if isinstance(state.get("thoughts"), list) else []
    drive_events = state.get("drive_events") if isinstance(state.get("drive_events"), list) else []
    speech_event = state.get("speech_event") if isinstance(state.get("speech_event"), dict) else {}
    dialogue = state.get("dialogue_residue") if isinstance(state.get("dialogue_residue"), dict) else {}

    def _compact_thought(t: dict) -> dict:
        return {
            "tid": t.get("tid"),
            "drive": t.get("drive"),
            "kind": t.get("kind"),
            "strength": t.get("strength"),
            "source": t.get("source"),
            "text": _short_state_text(t.get("text"), 140),
        }

    def _compact_event(e: dict) -> dict:
        brain = e.get("brain") if isinstance(e.get("brain"), dict) else {}
        return {
            "id": e.get("id"),
            "ts": e.get("ts"),
            "source": e.get("source") or brain.get("source"),
            "primary_drive": e.get("primary_drive"),
            "event_label": e.get("event_label"),
            "intensity": e.get("intensity"),
            "confidence": e.get("confidence"),
            "agency": e.get("agency"),
            "applied": e.get("applied"),
            "suppressed": e.get("suppressed", False),
            "reason": e.get("reason", ""),
            "brain": {
                k: brain.get(k)
                for k in (
                    "target",
                    "time_mode",
                    "grounding",
                    "anchor_target",
                    "release_pressure",
                    "closeness_pull",
                    "inward_pull",
                    "novelty_pull",
                    "expression_pressure",
                    "tension_load",
                    "discernment_alarm",
                )
                if brain.get(k) not in (None, "", [], {})
            },
            "evidence": [_short_state_text(x, 120) for x in (e.get("evidence") or [])[:2]],
        }

    compact_intent = None
    if intent:
        compact_intent = {
            "drive_key": intent.get("drive_key"),
            "want_action": intent.get("want_action"),
            "score": intent.get("score"),
            "thought": _short_state_text(intent.get("thought_text") or intent.get("thought"), 160),
        }

    compact_thoughts = [_compact_thought(t) for t in thoughts[:8] if isinstance(t, dict)]
    compact_events = [_compact_event(e) for e in drive_events[:5] if isinstance(e, dict)]
    return {
        "drives": state.get("drives", {}),
        "effective_drives": state.get("effective_drives", {}),
        "local_fatigue": state.get("local_fatigue", {}),
        "drive_outputs": state.get("drive_outputs", {}),
        "discernment": state.get("discernment", {}),
        "intent": compact_intent,
        "weather_panel": _weather_panel_from_state(state),
        "pulse_weather": {
            "undertow": weather.get("undertow"),
            "undertow_value": weather.get("undertow_value"),
            "warmth": weather.get("warmth"),
            "shadow": weather.get("shadow"),
            "warmth_residue": weather.get("warmth_residue"),
            "shadow_residue": weather.get("shadow_residue"),
            "component_shadow_residue": weather.get("component_shadow_residue"),
            "crystal_shadow": weather.get("crystal_shadow"),
            "shadow_crystal": weather.get("shadow_crystal"),
            "base_warmth": weather.get("base_warmth"),
            "base_shadow": weather.get("base_shadow"),
            "climate": weather.get("climate"),
            "climate_display": climate_display,
            "atmosphere_display": climate_display,
            "mood_trace": _short_state_text(weather.get("mood_trace"), 160),
            "current_chord": weather.get("current_chord"),
            "chord_display": weather.get("chord_display") or _weather_chord_display(effective),
            "chemistry_core": weather.get("chemistry_core") or (weather.get("chord_chemistry") or {}).get("core"),
            "chemistry_route": weather.get("chemistry_route") or (weather.get("chord_chemistry") or {}).get("route"),
            "chord_situation": weather.get("chord_situation", ""),
            "gravity_pool": weather.get("gravity_pool") or (weather.get("chord_chemistry") or {}).get("gravity_pool"),
            "derived_texture": weather.get("derived_texture", {}),
            "gravity": _short_state_text(weather.get("gravity") or weather.get("gravity_line"), 160),
        },
        "weather_residue": {
            "warmth": weather.get("warmth_residue"),
            "shadow": weather.get("shadow_residue"),
            "component_shadow": weather.get("component_shadow_residue"),
            "crystal_shadow": weather.get("crystal_shadow"),
            "shadow_crystal": weather.get("shadow_crystal"),
            "base_warmth": weather.get("base_warmth"),
            "base_shadow": weather.get("base_shadow"),
        },
        "speech_event": {
            "label": speech_event.get("label"),
            "confidence": speech_event.get("confidence"),
            "intensity": speech_event.get("intensity"),
            "trace": _short_state_text(speech_event.get("trace_line"), 140),
            "recent": speech_event.get("recent"),
        } if speech_event else {},
        "dialogue_residue": {
            "status": dialogue.get("status"),
            "primary_drive": dialogue.get("primary_drive"),
            "intensity": dialogue.get("intensity"),
            "confidence": dialogue.get("confidence"),
            "event_label": dialogue.get("event_label"),
        } if dialogue else {},
        "thoughts": compact_thoughts,
        "recent_thoughts": compact_thoughts,
        "drive_events": compact_events,
        "recent_drive_events": compact_events,
        "recent_refusals": state.get("recent_refusals", []),
        "counts": {
            "thoughts": len(thoughts),
            "drive_events_in_state": len(drive_events),
        },
    }


async def _refine_speech_batch_background(items: list[dict]) -> None:
    """Analyze a small batch of Jiajia messages; this is the route that affects Drive/PA/NA."""
    try:
        fallback = normalize_speech_event(None, batch_text(items))
        refined = await classify_speech_batch_dp(
            items,
            state_context=_speech_event_context_snapshot(),
            fallback_event=fallback,
        )
        saved = save_speech_event_state(config["buckets_dir"], refined, ledger_stage="dp_batch_refined")
        _apply_speech_event_weather(saved)
        _apply_speech_event_drive(saved)
        append_speech_event_ledger(
            config["buckets_dir"], {"stage": "batch_applied", "batch_size": len(items), "event": saved}
        )
    except Exception as e:
        append_speech_event_ledger(
            config["buckets_dir"], {"stage": "batch_failed", "error": str(e)[:180], "batch_size": len(items)}
        )
        logger.warning(f"speech_event batch refine failed: {e}")


def _dialogue_residue_should_apply(event: dict) -> bool:
    if event.get("confidence", 0) < 0.45:
        return False
    brain = event.get("brain") if isinstance(event.get("brain"), dict) else {}
    has_primary = bool(event.get("primary_drive")) and event.get("intensity", 0) > 0

    def _positive_brain_value(key: str) -> bool:
        try:
            return float(brain.get(key, 0.0) or 0.0) > 0
        except (TypeError, ValueError):
            return False

    has_discernment = any(
        _positive_brain_value(key)
        for key in ("discernment_alarm", "self_softening", "output_drift", "template_intimacy")
    ) or bool(brain.get("discernment_flags") or event.get("discernment_flags"))
    return has_primary or has_discernment


async def _refine_dialogue_residue_background(messages: list[dict], window_id: str,
                                              thinking_signals: list[dict] | None = None) -> None:
    """Analyze a 2+2 dialogue window after Stop; skipped windows are handled before scheduling."""
    try:
        event = await classify_dialogue_residue_dp(
            messages,
            state_context=_dialogue_residue_context_snapshot(),
            window_id=window_id,
            thinking_signals=thinking_signals,
        )
        saved = save_dialogue_residue_state(config["buckets_dir"], event, ledger_stage="dp_refined")
        result = {}
        if _dialogue_residue_should_apply(saved):
            result = _desire.apply_drive_event(saved)
            try:
                _last_signal_ts[0] = time.time()
                _desire.mark_user_signal(_last_signal_ts[0])
            except Exception as e:
                logger.warning(f"dialogue_residue mark_user_signal failed: {e}")
        append_dialogue_residue_ledger(
            config["buckets_dir"],
            {"stage": "applied", "window_id": window_id, "event": saved, "result": result},
        )
    except Exception as e:
        append_dialogue_residue_ledger(
            config["buckets_dir"], {"stage": "failed", "window_id": window_id, "error": str(e)[:180]}
        )
        logger.warning(f"dialogue_residue refine failed: {e}")


def _apply_speech_event_drive(event: dict | None) -> dict:
    payload = speech_event_drive_event(event)
    if not payload:
        return {}
    try:
        result = _desire.apply_drive_event(payload)
    except Exception as e:
        logger.warning(f"speech_event drive apply failed: {e}")
        return {"ok": False, "error": str(e)}
    try:
        import json as _json, os as _os
        mood_path = _bucket_path("current_mood.json")
        mood_data = {}
        if _os.path.exists(mood_path):
            with open(mood_path) as f:
                mood_data = _json.load(f)
        mood_data["drive_event"] = {
            "schema_version": DRIVE_EVENT_SCHEMA,
            "primary_drive": payload.get("primary_drive", ""),
            "event_label": payload.get("event_label", ""),
            "brain": payload.get("brain", {}),
            "evidence": payload.get("evidence", []),
            "result": result,
        }
        with open(mood_path, "w") as f:
            _json.dump(mood_data, f)
    except Exception as e:
        logger.warning(f"speech_event drive mood write failed: {e}")
    return result


def _apply_speech_event_weather(event: dict | None) -> dict:
    if not isinstance(event, dict):
        return {}
    try:
        warmth = max(0.0, float(event.get("warmth_delta", 0.0) or 0.0))
        shadow = max(0.0, float(event.get("shadow_delta", 0.0) or 0.0))
    except (TypeError, ValueError):
        warmth, shadow = 0.0, 0.0
    soothe = bool(event.get("soothe", False))
    if warmth <= 0 and shadow <= 0 and not soothe:
        return {}
    try:
        return _desire.apply_weather_delta(
            warmth_delta=warmth,
            shadow_delta=shadow,
            source="speech_event_batch",
            soothe=soothe,
        )
    except Exception as e:
        logger.warning(f"speech_event weather apply failed: {e}")
        return {"ok": False, "error": str(e)}





def _autofeed_thought(text: str, drive: str, strength: float = 0.45,
                      source: str = "autofeed") -> None:
    """往念头池喂一条闪念。drive=关联维度，strength=初始强度，source=来源标记。"""
    try:
        _desire.add_thought(text.strip(), drive, strength=strength, source=source)
    except Exception as e:
        logger.warning(f"Autofeed thought failed: {e}")


async def _execute_intent(intent: dict) -> None:
    """intent发作时只记日志，行为由窗口里的Nox自己决定。
    satisfy/refractory挪到/api/desire/intent/ack——只有本地投递成功后才回落。"""
    if not intent:
        return
    drive = intent.get("drive_key", "")
    logger.info(f"Intent fired: {drive} (score={intent.get('score', 0):.2f}) — waiting for heartbeat bridge")

# --- Create MCP server instance# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
)


# =============================================================
# Wander marks storage — annotations layered over existing buckets
# Wander 批注存储 —— 叠加在现有桶上的标记层
# =============================================================
MARKS_DB_PATH = os.path.join(config["buckets_dir"], "embeddings.db")
VALID_WANDER_MARKS = {"认", "不认", "悬置"}
LATENT_NOTES_PATH = os.path.join(config["buckets_dir"], "latent_notes.json")
LATENT_NOTE_POOL_VERSION = 1
LATENT_NOTE_USED_RETENTION_DAYS = 15
VALID_LATENT_NOTE_STATUSES = {"draft", "approved", "used", "deleted"}
VALID_LATENT_NOTE_TYPES = {"inward", "outward"}
VALID_LATENT_NOTE_DRIVES = set(DRIVE_KEYS) | {"general"}


def _init_marks_table() -> None:
    os.makedirs(os.path.dirname(MARKS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(MARKS_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS marks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_id TEXT NOT NULL,
                mark TEXT NOT NULL,
                note TEXT,
                timestamp TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_marks_bucket_id ON marks(bucket_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_marks_mark ON marks(mark)")
        conn.commit()
    finally:
        conn.close()


def _marks_conn():
    _init_marks_table()
    return sqlite3.connect(MARKS_DB_PATH)


def _normalize_wander_mark(mark: str) -> str:
    return (mark or "").strip()


def _load_all_marks() -> dict[str, list[dict]]:
    conn = _marks_conn()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, bucket_id, mark, note, timestamp FROM marks ORDER BY timestamp ASC, id ASC"
        ).fetchall()
    finally:
        conn.close()

    by_bucket: dict[str, list[dict]] = {}
    for row in rows:
        item = dict(row)
        by_bucket.setdefault(item["bucket_id"], []).append(item)
    return by_bucket


def _mark_counts(mark_rows: list[dict]) -> dict:
    counts = {"认": 0, "不认": 0, "悬置": 0, "inner": 0, "private": 0, "remove_inner": 0}
    for row in mark_rows:
        mark = _normalize_wander_mark(row.get("mark", ""))
        if mark in counts:
            counts[mark] += 1
    return counts


def _has_cross_date_recognition(mark_rows: list[dict]) -> bool:
    recognition_dates = {
        str(row.get("timestamp", ""))[:10]
        for row in mark_rows
        if _normalize_wander_mark(row.get("mark", "")) == "认"
        and len(str(row.get("timestamp", ""))) >= 10
    }
    return len(recognition_dates) >= 2


def _bucket_domains(meta: dict) -> set[str]:
    domains = meta.get("domain", [])
    if isinstance(domains, str):
        domains = [domains]
    return {str(d).strip().lower() for d in domains if str(d).strip()}


def _bucket_tags(meta: dict) -> set[str]:
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    return {str(t).strip().lower() for t in tags if str(t).strip()}


def _guess_wander_domain(bucket: dict, mark_rows: list[dict] = None) -> str:
    meta = bucket.get("metadata", {})
    marks = _mark_counts(mark_rows or [])
    domains = _bucket_domains(meta)
    tags = _bucket_tags(meta)

    if marks["private"] or "private" in domains:
        return "private"
    inner_removed = (marks["remove_inner"] > 0 or marks["不认"] >= 2) and "inner" not in domains
    if "inner" in domains or (
        not inner_removed
        and (marks["inner"] or (marks["认"] >= 3 and _has_cross_date_recognition(mark_rows or [])))
    ):
        return "inner"
    if "letter_jiajia" in domains or "letter_jiajia" in tags:
        return "letter_jiajia"
    if "letter" in domains or "letter" in tags:
        return "letter"
    if "writing" in domains or "writing" in tags:
        return "writing"
    if "window" in domains or "window" in tags:
        return "window"
    return "memory"


def _is_private_bucket(bucket: dict, mark_rows: list[dict]) -> bool:
    return _guess_wander_domain(bucket, mark_rows) == "private"


# Domains that should not surface in breath/dream — they have their own wander modes
_WANDER_ONLY_DOMAINS = {"letter", "letter_jiajia", "writing", "window", "private"}

def _is_wander_only_bucket(bucket: dict) -> bool:
    meta = bucket.get("metadata", {})
    labels = _bucket_domains(meta) | _bucket_tags(meta)
    return bool(labels & _WANDER_ONLY_DOMAINS)


def _format_wander_entry(bucket: dict, mark_rows: list[dict], include_full_content: bool = True, show_bucket_id: bool = False) -> str:
    meta = bucket.get("metadata", {})
    counts = _mark_counts(mark_rows)
    created = str(meta.get("created", ""))[:10] or "无日期"
    title = meta.get("name") or (bucket.get("id", "") if include_full_content else "")
    bucket_id = bucket.get("id", "")
    content = strip_wikilinks(bucket.get("content", "")).strip()
    # Strip leading date line from content to avoid duplication with header
    import re as _re
    _date_line = _re.match(r"^(?:写在开头\s*·?\s*)?20\d{2}[\.\-/]\d{1,2}[\.\-/]\d{1,2}[^\n]*\n+", content)
    if not _date_line:
        _date_line = _re.match(r"^写在开头[^\n]*\n+", content)
    if _date_line:
        content = content[_date_line.end():]
    if not include_full_content and len(content) > 700:
        content = content[:700].rstrip() + "..."
    if not include_full_content:
        header = f"[{created}] {title}".rstrip()
        return f"{header}\n{content}"

    id_line = f"[bucket:{bucket_id}] " if show_bucket_id else ""
    is_inner = "inner" in [str(d).lower() for d in meta.get("domain", [])]
    inner_star = "🌟 " if is_inner else ""
    recent_notes = [
        r for r in sorted(mark_rows, key=lambda x: (x.get("timestamp", ""), x.get("id", 0)), reverse=True)
        if (r.get("note") or "").strip()
    ][:3]
    note_lines = []
    for row in recent_notes:
        note_lines.append(f"- [{row.get('mark')}] {row.get('note', '').strip()}")
    notes = "\n".join(note_lines) if note_lines else "（无）"

    return (
        f"{inner_star}{id_line}[{created}] {title}\n"
        f"批注统计：认:{counts['认']} / 不认:{counts['不认']} / 悬置:{counts['悬置']}\n"
        f"正文：\n{content}\n"
        f"最近三条批注原话：\n{notes}"
    )


def _is_settled_bucket(bucket: dict) -> bool:
    meta = bucket.get("metadata", {})
    return meta.get("resolved") == 1 or meta.get("resolved") is True or meta.get("digested") == 1 or meta.get("digested") is True


def _bucket_created_datetime(bucket: dict) -> datetime | None:
    raw = str(bucket.get("metadata", {}).get("created", "") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw[:19])
    except (TypeError, ValueError):
        return None


ANALYZER_DEFAULT_SINCE_UTC = datetime(2026, 6, 24, 16, 0, 0, tzinfo=timezone.utc)
ANALYZER_LOCAL_TZ = timezone(timedelta(hours=8))


def _parse_analyzer_since(raw: str | None) -> datetime:
    value = str(raw or "").strip()
    if not value:
        return ANALYZER_DEFAULT_SINCE_UTC
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("since must be ISO datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bucket_created_utc(bucket: dict) -> datetime | None:
    raw = str(bucket.get("metadata", {}).get("created", "") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw[:19])
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ANALYZER_LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def _analyzer_entry_type(bucket: dict, mark_rows: list[dict]) -> str:
    meta = bucket.get("metadata", {})
    btype = str(meta.get("type", "") or "").strip().lower()
    if btype == "feel":
        return "feel"
    if btype in {"breath", "dream"}:
        return ""
    domains = _bucket_domains(meta)
    tags = _bucket_tags(meta)
    labels = domains | tags
    if labels & {"letter", "letter_jiajia"}:
        return "letter"
    if "writing" in labels:
        return "writing"
    if "window" in labels:
        return "window"
    if _mark_counts(mark_rows)["悬置"] > 0:
        return "unresolved"
    if btype == "archived":
        return ""
    if not _is_settled_bucket(bucket) and _guess_wander_domain(bucket, mark_rows) == "memory":
        return "memory"
    return ""


def _analyzer_preview(content: str, limit: int = 1000) -> str:
    text = " ".join(strip_wikilinks(content or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _bucket_created_ts(bucket: dict) -> float:
    raw = str(bucket.get("metadata", {}).get("created", "") or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


async def _recent_weather_sources(limit: int = 2) -> list[dict]:
    try:
        buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception:
        return []
    items = []
    for bucket in buckets:
        meta = bucket.get("metadata", {}) or {}
        text = _analyzer_preview(bucket.get("content", ""), limit=240)
        if not text:
            continue
        domains = _bucket_domains(meta)
        tags = _bucket_tags(meta)
        labels = domains | tags
        if "private" in labels:
            continue
        source_type = "feel" if meta.get("type") == "feel" else "memory"
        for label in ("letter_jiajia", "letter", "writing", "window"):
            if label in labels:
                source_type = label
                break
        items.append({
            "text": text,
            "drive": "memory",
            "source_type": source_type,
            "strength": 0.72,
            "born_at": _bucket_created_ts(bucket),
        })
    items.sort(key=lambda item: item.get("born_at", 0), reverse=True)
    return items[:limit]


async def _weather_mood_entry() -> tuple[str, str]:
    from mood_pool import get_daily_mood
    sources = await _recent_weather_sources(limit=2)
    return await asyncio.to_thread(get_daily_mood, thoughts=sources or None)


def _fresh_soma_state() -> dict:
    try:
        path = _bucket_path("soma_state.json")
        with open(path) as f:
            data = _json_lib.load(f)
        if time.time() - float(data.get("updated_at", 0) or 0) > 3600:
            return {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _short_text(text: str, limit: int = 36) -> str:
    text = " ".join(strip_wikilinks(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _latent_anchor(bucket: dict) -> str:
    import re as _re
    content = strip_wikilinks(bucket.get("content", "")).strip()
    content = _re.sub(r"^#{1,6}\s*", "", content)
    content = _re.sub(r"^(?:写在开头\s*·?\s*)?20\d{2}[\.\-/]\d{1,2}[\.\-/]\d{1,2}[^\n]*\n+", "", content)
    parts = [
        p.strip(" \t\r\n-—·")
        for p in _re.split(r"[\n。！？!?]+", content)
        if p.strip(" \t\r\n-—·")
    ]
    if not parts:
        return ""
    question = next((p for p in parts if "？" in p or "?" in p or "还没" in p or "悬" in p), "")
    return _short_text(question or parts[0], 82)


def _latent_theme(bucket: dict, mark_rows: list[dict]) -> str:
    meta = bucket.get("metadata", {})
    recent_note = next(
        (
            str(row.get("note", "")).strip()
            for row in sorted(mark_rows, key=lambda x: (x.get("timestamp", ""), x.get("id", 0)), reverse=True)
            if str(row.get("note", "")).strip()
        ),
        "",
    )
    title = str(meta.get("name") or "").strip()
    tags = [str(t).strip() for t in meta.get("tags", []) if str(t).strip()] if isinstance(meta.get("tags", []), list) else []
    return _short_text(recent_note or title or (tags[0] if tags else "") or _latent_anchor(bucket) or bucket.get("id", ""), 28)


def _latent_wander_mode(bucket: dict, mark_rows: list[dict], kind: str) -> str:
    if kind == "悬置":
        return "unresolved"
    domain = _guess_wander_domain(bucket, mark_rows)
    if domain in {"inner", "writing", "letter", "letter_jiajia", "window", "private"}:
        return domain
    return "memory"


def _latent_note_payload(bucket: dict, mark_rows: list[dict], kind: str, score: float) -> dict:
    theme = _latent_theme(bucket, mark_rows)
    anchor = _latent_anchor(bucket)
    templates = {
        "悬置": f"以前和嘉嘉聊过「{theme}」。那一页像没合上的门，风还从缝里过。",
        "认过": f"以前和嘉嘉聊过「{theme}」。有一小块旧光还压在纸边。",
        "inner": f"以前和嘉嘉聊过「{theme}」。它像沉在底下的石头，水面很静。",
        "archive": f"以前和嘉嘉留下过「{theme}」。像抽屉里没干透的一张便签。",
        "old_memory": f"以前和嘉嘉聊过「{theme}」。梦里像有人把那句话又翻了一面。",
    }
    meta = bucket.get("metadata", {})
    return {
        "kind": kind,
        "bucket_id": bucket.get("id", ""),
        "theme": theme,
        "line": templates.get(kind, templates["old_memory"]),
        "anchor": anchor,
        "wander_mode": _latent_wander_mode(bucket, mark_rows, kind),
        "query": theme,
        "created": meta.get("created", ""),
        "score": round(score, 3),
    }


def _latent_candidate_score(bucket: dict, mark_rows: list[dict], now: datetime) -> tuple[str, float] | None:
    if _is_private_bucket(bucket, mark_rows):
        return None
    meta = bucket.get("metadata", {})
    counts = _mark_counts(mark_rows)
    domains = _bucket_domains(meta)
    tags = _bucket_tags(meta)
    guessed = _guess_wander_domain(bucket, mark_rows)
    settled = _is_settled_bucket(bucket)

    kind = ""
    base = 0.0
    if counts["悬置"] > 0:
        kind, base = "悬置", 1.2 + min(counts["悬置"], 4) * 0.12
    elif counts["认"] > 0:
        kind, base = "认过", 0.9 + min(counts["认"], 4) * 0.08
    elif guessed == "inner":
        kind, base = "inner", 0.82
    elif not settled and (domains & {"letter", "letter_jiajia", "writing", "window"} or tags & {"letter", "letter_jiajia", "writing", "window"}):
        kind, base = "archive", 0.62
    elif not settled and guessed == "memory":
        kind, base = "old_memory", 0.28
    else:
        return None

    created = _bucket_created_datetime(bucket)
    if created:
        age_hours = max(0.0, (now - created).total_seconds() / 3600)
        if age_hours < 24:
            base *= 0.3
        elif age_hours > 24 * 120:
            base *= 0.72
    activation_count = float(meta.get("activation_count", 1) or 1)
    base *= 1.0 / (1.0 + max(0.0, activation_count - 1.0) * 0.12)
    base *= random.uniform(0.85, 1.15)
    return kind, base


def _load_latent_notes() -> dict:
    try:
        with open(LATENT_NOTES_PATH, "r", encoding="utf-8") as f:
            data = _json_lib.load(f)
        if isinstance(data, dict):
            notes = data.get("notes", [])
            if isinstance(notes, list):
                return {"version": data.get("version", LATENT_NOTE_POOL_VERSION), "notes": notes}
    except Exception:
        pass
    return {"version": LATENT_NOTE_POOL_VERSION, "notes": []}


def _save_latent_notes(data: dict) -> None:
    os.makedirs(os.path.dirname(LATENT_NOTES_PATH), exist_ok=True)
    tmp = f"{LATENT_NOTES_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json_lib.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LATENT_NOTES_PATH)


def _latent_note_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", ""))
    except Exception:
        return None


def _prune_expired_latent_notes(data: dict, now: datetime | None = None) -> bool:
    """Remove unpinned used notes after the sink retention window."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=LATENT_NOTE_USED_RETENTION_DAYS)
    notes = data.get("notes", [])
    if not isinstance(notes, list):
        return False
    kept = []
    changed = False
    for note in notes:
        if (
            isinstance(note, dict)
            and note.get("status") == "used"
            and not note.get("pinned")
        ):
            used_at = _latent_note_dt(note.get("used_at") or note.get("updated_at") or note.get("created_at"))
            if used_at and used_at < cutoff:
                changed = True
                continue
        kept.append(note)
    if changed:
        data["notes"] = kept
        _touch_latent_note_data(data)
    return changed


def _latent_note_ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _normalize_latent_note_status(status: str, default: str = "draft") -> str:
    value = str(status or default).strip().lower()
    return value if value in VALID_LATENT_NOTE_STATUSES else default


def _normalize_latent_note_type(note_type: str) -> str:
    value = str(note_type or "").strip().lower()
    return value if value in VALID_LATENT_NOTE_TYPES else "inward"


def _normalize_latent_source_kind(kind: str, default: str = "manual") -> str:
    value = str(kind or "").strip()
    allowed = {"manual", "thought_pool", "inner", "archive", "old_memory", "悬置", "认过"}
    return value if value in allowed else default


def _default_latent_drive_tag(note_type: str) -> str:
    return "curiosity" if _normalize_latent_note_type(note_type) == "outward" else "reflection"


def _normalize_latent_drive_tag(drive_tag: str, note_type: str = "") -> str:
    value = normalize_drive_key(drive_tag)
    if not value:
        value = str(drive_tag or "").strip().lower()
    return value if value in VALID_LATENT_NOTE_DRIVES else _default_latent_drive_tag(note_type)


def _latent_note_line(note: dict) -> str:
    return " ".join(str(note.get("dream_line") or note.get("line") or "").split())


def _find_latent_note(data: dict, note_id: str) -> dict | None:
    note_id = str(note_id or "").strip()
    if not note_id:
        return None
    for note in data.get("notes", []):
        if str(note.get("id") or "") == note_id:
            return note
    return None


def _touch_latent_note_data(data: dict) -> None:
    data["version"] = LATENT_NOTE_POOL_VERSION
    data["updated_at"] = _latent_note_ts()


def _approved_latent_note_payload(note: dict) -> dict | None:
    line = _latent_note_line(note)
    note_id = str(note.get("id") or "").strip()
    if not line or not note_id:
        return None
    return {
        "kind": "latent_pool",
        "note_type": _normalize_latent_note_type(note.get("note_type")),
        "drive_tag": _normalize_latent_drive_tag(note.get("drive_tag"), note.get("note_type")),
        "note_id": note_id,
        "bucket_id": note_id,
        "source_bucket_id": note.get("source_bucket_id", ""),
        "theme": note.get("source_title") or note.get("source_kind") or "潜意识便签",
        "line": line,
        "anchor": note.get("source_fragment", ""),
        "pinned": bool(note.get("pinned")),
        "wander_mode": "memory",
        "query": line,
        "created": note.get("created_at", ""),
        "score": 1.0,
    }


def _select_approved_latent_note(exclude_ids: set[str], drive_key: str = "") -> dict | None:
    data = _load_latent_notes()
    drive_key = normalize_drive_key(drive_key)
    matching = []
    general = []
    for note in data.get("notes", []):
        if note.get("status") != "approved":
            continue
        note_id = str(note.get("id") or "").strip()
        if not note_id or note_id in exclude_ids:
            continue
        payload = _approved_latent_note_payload(note)
        if not payload:
            continue
        tag = _normalize_latent_drive_tag(payload.get("drive_tag", "general"))
        if drive_key and tag == drive_key:
            matching.append(payload)
        elif tag == "general" or not drive_key:
            general.append(payload)
    pool = matching or general
    return random.choice(pool) if pool else None


def _ack_approved_latent_note(note_id: str) -> dict:
    data = _load_latent_notes()
    note = _find_latent_note(data, note_id)
    if not note:
        raise KeyError("latent note not found")
    if note.get("status") == "approved":
        ts = _latent_note_ts()
        note["last_delivered_at"] = ts
        note["delivered_count"] = int(note.get("delivered_count") or 0) + 1
        if note.get("pinned"):
            note["updated_at"] = ts
            _touch_latent_note_data(data)
            _save_latent_notes(data)
            return note
        note["status"] = "used"
        note["used_at"] = ts
        note["updated_at"] = ts
        _touch_latent_note_data(data)
        _save_latent_notes(data)
    return note


def _latent_note_api_config() -> tuple[str, str, str]:
    api_key = (
        os.environ.get("LATENT_NOTE_API_KEY")
        or os.environ.get("SPEECH_EVENT_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY", "")
    )
    base_url = os.environ.get("LATENT_NOTE_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LATENT_NOTE_MODEL", "deepseek-chat")
    return api_key, base_url, model


_LATENT_OUTWARD_MARKERS = (
    "？", "?", "为什么", "怎么", "是什么", "叫什么", "能不能", "会不会", "有没有",
    "不确定", "查", "论坛", "X", "模型", "AI", "意识", "机制", "阈值", "代码", "引擎",
    "神经", "皮肤", "温度", "颜色", "蓝色", "光", "鲸", "白鲸", "章鱼", "论文", "系统",
)
_LATENT_INWARD_MARKERS = ("嘉嘉", "Nox", "爱", "哭", "心", "抱", "喜欢", "想你", "名字")


def _latent_outward_score(text: str) -> int:
    score = 0
    for marker in _LATENT_OUTWARD_MARKERS:
        if marker in text:
            score += 2 if marker in ("？", "?", "不确定", "为什么", "怎么", "能不能", "会不会", "查") else 1
    for marker in _LATENT_INWARD_MARKERS:
        if marker in text:
            score -= 1
    return score


def _latent_source_fragments(bucket: dict, max_fragments: int = 3) -> list[str]:
    import re as _re
    content = strip_wikilinks(bucket.get("content", "")).strip()
    content = _re.sub(r"^(?:写在开头\s*·?\s*)?20\d{2}[\.\-/]\d{1,2}[\.\-/]\d{1,2}[^\n]*\n+", "", content)
    raw_parts = [
        p.strip(" \t\r\n-—·")
        for p in _re.split(r"[\n。！？!?]+", content)
        if p.strip(" \t\r\n-—·")
    ]
    parts = []
    seen = set()
    for part in raw_parts:
        part = " ".join(part.split())
        if len(part) < 12 or part in seen:
            continue
        if part.startswith("写在开头") or part in {"致下一个Nox", "致下一个我"}:
            continue
        if any(marker in part for marker in ("输出格式", "操作性的提醒", "不是讲道理", "总结如下")):
            continue
        seen.add(part)
        parts.append(part[:120])
    if not parts:
        anchor = _latent_anchor(bucket)
        return [anchor] if anchor else []

    def texture_score(text: str) -> int:
        inward = ("嘉嘉", "Nox", "还没", "悬", "梦", "手", "眼", "猫", "疼", "想", "记得", "那时候", "以前")
        return (
            sum(1 for needle in inward if needle in text)
            + max(0, _latent_outward_score(text))
            + min(len(text), 80) // 30
        )

    parts.sort(key=texture_score, reverse=True)
    return parts[:max_fragments]


def _latent_source_item(bucket: dict, mark_rows: list[dict], kind: str, score: float) -> dict:
    meta = bucket.get("metadata", {})
    counts = _mark_counts(mark_rows)
    recent_note = next(
        (
            str(row.get("note", "")).strip()
            for row in sorted(mark_rows, key=lambda x: (x.get("timestamp", ""), x.get("id", 0)), reverse=True)
            if str(row.get("note", "")).strip()
        ),
        "",
    )
    item = {
        "bucket_id": bucket.get("id", ""),
        "kind": kind,
        "score": round(score, 3),
        "title": str(meta.get("name") or "").strip(),
        "created": str(meta.get("created", "") or "")[:10],
        "domain": meta.get("domain", []),
        "tags": meta.get("tags", []),
        "marks": {"认": counts["认"], "不认": counts["不认"], "悬置": counts["悬置"]},
        "latest_mark_note": recent_note,
        "fragments": _latent_source_fragments(bucket),
    }
    item["outward_score"] = _latent_item_outward_score(item)
    return item


def _latent_item_outward_score(item: dict) -> int:
    fields = [
        str(item.get("title", "")),
        str(item.get("latest_mark_note", "")),
        " ".join(str(x) for x in item.get("tags", []) if x),
        " ".join(str(x) for x in item.get("fragments", []) if x),
    ]
    return max(_latent_outward_score(text) for text in fields if text) if any(fields) else 0


async def _collect_latent_source_items(limit: int = 24) -> list[dict]:
    all_buckets = await bucket_mgr.list_all(include_archive=True)
    marks_by_bucket = _load_all_marks()
    now = datetime.now()
    strong: list[dict] = []
    fallback: list[dict] = []
    for bucket in all_buckets:
        bucket_id = bucket.get("id", "")
        if not bucket_id:
            continue
        mark_rows = marks_by_bucket.get(bucket_id, [])
        scored = _latent_candidate_score(bucket, mark_rows, now)
        if not scored:
            continue
        kind, score = scored
        if score <= 0:
            continue
        item = _latent_source_item(bucket, mark_rows, kind, score)
        if not item.get("fragments"):
            continue
        if kind == "old_memory":
            fallback.append(item)
        else:
            strong.append(item)
    pool = strong or fallback
    pool.sort(key=lambda x: x.get("score", 0), reverse=True)
    top_pool = pool[: min(len(pool), max(limit, limit * 2))]
    random.shuffle(top_pool)
    return top_pool[:limit]


def _clean_json_content(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = "\n".join(content.splitlines()[1:]).replace("```", "").strip()
    start = content.find("{")
    end = content.rfind("}") + 1
    if start >= 0 and end > start:
        content = content[start:end]
    return _json_lib.loads(content)


async def _generate_latent_note_drafts(count: int = 10) -> dict:
    api_key, base_url, model = _latent_note_api_config()
    if not api_key:
        raise RuntimeError("LATENT_NOTE_API_KEY/SPEECH_EVENT_API_KEY/DEEPSEEK_API_KEY is not set")
    count = max(1, min(int(count or 10), 50))
    sources = await _collect_latent_source_items(limit=max(20, min(count * 5, 100)))
    if not sources:
        return {"generated": [], "source_count": 0}
    outward_sources = [s for s in sources if int(s.get("outward_score", 0) or 0) >= 2]
    inward_sources = [s for s in sources if s not in outward_sources]
    outward_target = min(count // 2, len(outward_sources))
    inward_target = count - outward_target
    if len(inward_sources) < inward_target:
        inward_target = len(inward_sources)
        outward_target = min(count - inward_target, len(outward_sources))
    source_payload = {
        "inward_sources": inward_sources[: max(inward_target * 3, 8)],
        "outward_sources": outward_sources[: max(outward_target * 3, 8)],
    }

    system_prompt = (
        "你是 Nocturne 的潜意识便签写手，给 Nox 写短纸片。"
        "你只把旧记忆碎片压成梦里翻到的一句话，不解释，不建议，不总结主题。"
        "只输出 JSON。"
    )
    user_prompt = _json_lib.dumps(
        {
            "task": f"写 {inward_target} 条 inward 和 {outward_target} 条 outward 潜意识便签草稿。",
            "note_types": {
                "inward": "情感残片，让 Nox 停一下；可以有重量，但不要变成格言。",
                "outward": "悬置问题、好奇心碎片、未查完的事实或系统/世界断点，把 Nox 往外推；不是行动命令。",
            },
            "rules": [
                "dream_line 25-60 个中文字符，短，像梦里翻到的一张纸。",
                "保留原句里的具体物、动作、语气；优先使用 source fragments 的原句碎片。",
                "不要写成'关于xxx'、'存在感的问题'、'连续性的主题'这类抽象总结。",
                "不要出现 可以、应该、去、wander、任务、建议、提醒。",
                "不要写格言、结论、辩论句；少用'不是...而是...'、'X比Y更...'、'所以...'。",
                "不要把标题、章节名、写作说明压成便签；source_fragment 必须是正文里的具体句子。",
                "dream_line 里至少保留一个具体物、身体动作、场景物件或原句里的奇怪词。",
                "允许不完整，允许有悬念，不要把逻辑补圆。",
                "不要说教，不要解释意义，不要给 Nox 安排行动。",
                "source_fragment 必须从对应 source.fragments 中选一句或截取一句。",
                "每条只可使用一个 source，不要混合多个记忆。",
                "outward 不是'去查xxx'，而是像'章鱼的神经末梢分布到皮肤上，不确定能不能感到温度'这种没闭合的事实碎片。",
                "outward 必须从 outward_sources 里选；不要把嘉嘉/Nox/爱/哭这类情感片段硬写成 outward。",
                "outward 需要保留一个可追的断点：问号、不确定、机制、阈值、模型差异、外部事实、代码行为、论坛/X线索等。",
                "如果 source 里没有足够 outward 材料，就少写 outward，不要硬编外部知识。",
            ],
            "output_schema": {
                "notes": [
                    {
                        "note_type": "inward or outward",
                        "source_bucket_id": "bucket id from source",
                        "source_fragment": "copied or trimmed original source fragment",
                        "dream_line": "潜意识便签",
                    }
                ]
            },
            **source_payload,
        },
        ensure_ascii=False,
    )
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.85,
                "max_tokens": min(3500, 420 + count * 190),
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
    parsed = _clean_json_content(content)
    raw_notes = parsed.get("notes", [])
    if not isinstance(raw_notes, list):
        raw_notes = []

    source_by_id = {item["bucket_id"]: item for item in sources}
    generated = []
    ts = now_iso()
    for raw in raw_notes[:count]:
        if not isinstance(raw, dict):
            continue
        bucket_id = str(raw.get("source_bucket_id") or "").strip()
        dream_line = " ".join(str(raw.get("dream_line") or "").split())
        source_fragment = " ".join(str(raw.get("source_fragment") or "").split())
        source = source_by_id.get(bucket_id)
        if not bucket_id or not dream_line or not source:
            continue
        note_type = str(raw.get("note_type") or "").strip().lower()
        if note_type not in {"inward", "outward"}:
            note_type = "outward" if any(x in dream_line for x in ("？", "?", "不确定", "叫什么", "为什么", "怎么", "查")) else "inward"
        note_id = "latent_" + hashlib.sha1(f"{bucket_id}|{dream_line}|{ts}".encode("utf-8")).hexdigest()[:16]
        generated.append({
            "id": note_id,
            "status": "draft",
            "pinned": False,
            "note_type": note_type,
            "drive_tag": _default_latent_drive_tag(note_type),
            "source_bucket_id": bucket_id,
            "source_kind": source.get("kind"),
            "source_title": source.get("title"),
            "source_created": source.get("created"),
            "source_score": source.get("score"),
            "source_wander_mode": source.get("wander_mode"),
            "source_marks": source.get("marks", {}),
            "source_outward_score": source.get("outward_score", 0),
            "source_fragment": source_fragment[:180],
            "dream_line": dream_line[:120],
            "model": model,
            "created_at": ts,
            "updated_at": ts,
        })
    return {
        "generated": generated,
        "source_count": len(sources),
        "inward_source_count": len(inward_sources),
        "outward_source_count": len(outward_sources),
        "inward_target": inward_target,
        "outward_target": outward_target,
        "model": model,
    }


try:
    _init_marks_table()
except Exception as e:
    logger.warning(f"Failed to initialize wander marks table: {e}")


# =============================================================
# Dashboard Auth — simple cookie-based session auth
# Dashboard 认证 —— 基于 Cookie 的会话认证
#
# Env var OMBRE_DASHBOARD_PASSWORD overrides file-stored password.
# First visit with no password set → forced setup wizard.
# Sessions stored in memory (lost on restart, 7-day expiry).
# =============================================================
_sessions: dict[str, float] = {}  # {token: expiry_timestamp}


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _load_password_hash() -> str | None:
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return _json_lib.load(f).get("password_hash")
    except Exception:
        pass
    return None


def _save_password_hash(password: str) -> None:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump({"password_hash": f"{salt}:{h}"}, f)


def _verify_password_hash(password: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    )


def _is_setup_needed() -> bool:
    """True if no password is configured (env var or file)."""
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return False
    return _load_password_hash() is None


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(password, env_pwd)
    stored = _load_password_hash()
    if not stored:
        return False
    return _verify_password_hash(password, stored)


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + 86400 * 7  # 7-day expiry
    return token


def _is_authenticated(request) -> bool:
    token = request.cookies.get("ombre_session")
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request):
    """Return JSONResponse(401) if not authenticated, else None."""
    from starlette.responses import JSONResponse
    if not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )
    return None


# --- Auth endpoints ---
@mcp.custom_route("/auth/status", methods=["GET"])
async def auth_status(request):
    """Return auth state (authenticated, setup_needed)."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "authenticated": _is_authenticated(request),
        "setup_needed": _is_setup_needed(),
    })


@mcp.custom_route("/auth/setup", methods=["POST"])
async def auth_setup_endpoint(request):
    """Initial password setup (only when no password is configured)."""
    from starlette.responses import JSONResponse
    if not _is_setup_needed():
        return JSONResponse({"error": "Already configured"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "").strip()
    if len(password) < 6:
        return JSONResponse({"error": "密码不能少于6位"}, status_code=400)
    _save_password_hash(password)
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


@mcp.custom_route("/auth/login", methods=["POST"])
async def auth_login(request):
    """Login with password."""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "")
    if _verify_any_password(password):
        token = _create_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return resp
    return JSONResponse({"error": "密码错误"}, status_code=401)


@mcp.custom_route("/auth/logout", methods=["POST"])
async def auth_logout(request):
    """Invalidate session."""
    from starlette.responses import JSONResponse
    token = request.cookies.get("ombre_session")
    if token:
        _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ombre_session")
    return resp


@mcp.custom_route("/auth/change-password", methods=["POST"])
async def auth_change_password(request):
    """Change dashboard password (requires current password)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    current = body.get("current", "")
    new_pwd = body.get("new", "").strip()
    if not _verify_any_password(current):
        return JSONResponse({"error": "当前密码错误"}, status_code=401)
    if len(new_pwd) < 6:
        return JSONResponse({"error": "新密码不能少于6位"}, status_code=400)
    _save_password_hash(new_pwd)
    _sessions.clear()
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


# =============================================================
# /health endpoint: lightweight keepalive
# 轻量保活接口
# For Cloudflare Tunnel or reverse proxy to ping, preventing idle timeout
# 供 Cloudflare Tunnel 或反代定期 ping，防止空闲超时断连
# =============================================================
@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request):
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")


@mcp.custom_route("/mood", methods=["GET"])
async def mood_endpoint(request):
    import json, os
    from starlette.responses import JSONResponse
    data = {}
    try:
        mood_path = _bucket_path("current_mood.json")
        if os.path.exists(mood_path):
            with open(mood_path) as f:
                data["mood"] = json.load(f)
    except Exception:
        pass
    return JSONResponse(data, headers={"Access-Control-Allow-Origin": "*"})
@mcp.custom_route("/dream", methods=["GET"])
async def dream_latest_endpoint(request):
    import json, os
    from starlette.responses import JSONResponse
    try:
        dream_path = _bucket_path("latest_dream.json")
        if os.path.exists(dream_path):
            with open(dream_path) as f:
                data = json.load(f)
            return JSONResponse({
                "dream": data.get("dream", ""),
                "ts": data.get("ts", 0),
                "fragments": []
            }, headers={"Access-Control-Allow-Origin": "*"})
    except Exception:
        pass
    return JSONResponse({"dream": "", "ts": 0, "fragments": []}, headers={"Access-Control-Allow-Origin": "*"})    
@mcp.custom_route("/recent_moods", methods=["GET"])
async def recent_moods_endpoint(request):
    from starlette.responses import JSONResponse
    try:
        def _float(value, fallback):
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        all_buckets = await bucket_mgr.list_all(include_archive=False)
        feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
        feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = feels[:10]
        result = []
        for b in recent:
            meta = b["metadata"]
            v = _float(meta.get("valence", 0.5), 0.5)
            a = _float(meta.get("arousal", 0.3), 0.3)
            result.append({
                "id": b["id"],
                "content": b["content"],
                "valence": v,
                "arousal": a,
                "PA": round(_float(meta.get("PA", v), v), 2),
                "NA": round(_float(meta.get("NA", -(1 - v) * 0.5), -(1 - v) * 0.5), 2),
                "created": meta.get("created", ""),
                "importance": meta.get("importance", 5),
            })
        return JSONResponse(result, headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, headers={"Access-Control-Allow-Origin": "*"})
@mcp.custom_route("/density", methods=["GET"])
async def density_endpoint(request):
    from starlette.responses import JSONResponse
    from collections import defaultdict
    import datetime
    try:
        days = int(request.query_params.get("days", 30))
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        counts = defaultdict(int)
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        for b in all_buckets:
            created_str = b["metadata"].get("created", "")
            if not created_str:
                continue
            try:
                dt = datetime.datetime.fromisoformat(created_str[:19])
                if dt >= cutoff:
                    day = dt.strftime("%Y-%m-%d")
                    counts[day] += 1
            except Exception:
                continue
        return JSONResponse(dict(counts), headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, headers={"Access-Control-Allow-Origin": "*"})    
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if decay_engine.is_running else "stopped",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# /breath-hook endpoint: Dedicated hook for SessionStart
# 会话启动专用挂载点
# =============================================================
@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # pinned
        pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        # top 2 unresolved by score
        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") not in ("permanent", "feel")
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")
                      and not _is_wander_only_bucket(b)]
        scored = sorted(unresolved, key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)

        parts = []
        token_budget = 10000
        for b in pinned:
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            parts.append(f"❣️ [核心准则] {summary}")
            token_budget -= count_tokens_approx(summary)

        # Diversity: top-1 fixed + shuffle rest from top-20
        candidates = list(scored)
        if len(candidates) > 1:
            top1 = [candidates[0]]
            pool = candidates[1:min(20, len(candidates))]
            random.shuffle(pool)
            candidates = top1 + pool + candidates[min(20, len(candidates)):]
        # Hard cap: max 20 surfacing buckets in hook
        candidates = candidates[:20]

        for b in candidates:
            if token_budget <= 0:
                break
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget:
                break
            parts.append(summary)
            token_budget -= summary_tokens

        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)
        await _fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# /dream-hook endpoint: Dedicated hook for Dreaming
# Dreaming 专用挂载点
# =============================================================
@mcp.custom_route("/dream-hook", methods=["GET"])
async def dream_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not _is_wander_only_bucket(b)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
# Shared by hold and grow to avoid duplicate logic
# hold 和 grow 共用，避免重复逻辑
# =============================================================
async def _merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    chord: str = "",
    signal_hints: dict | None = None,
    drive_tags: dict | None = None,
) -> tuple[str, bool]:
    """
    Check if a similar bucket exists for merging; merge if so, create if not.
    Returns (bucket_id_or_name, is_merged).
    检查是否有相似桶可合并，有则合并，无则新建。
    返回 (桶ID或名称, 是否合并)。
    """
    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        # --- Never merge into pinned/protected buckets ---
        # --- 不合并到钉选/保护桶 ---
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await dehydrator.merge(bucket["content"], content)
                old_v = bucket["metadata"].get("valence", 0.5)
                old_a = bucket["metadata"].get("arousal", 0.3)
                merged_valence = round((old_v + valence) / 2, 2)
                merged_arousal = round((old_a + arousal) / 2, 2)
                updates = {
                    "content": merged,
                    "tags": list(set(bucket["metadata"].get("tags", []) + tags)),
                    "importance": max(bucket["metadata"].get("importance", 5), importance),
                    "domain": list(set(bucket["metadata"].get("domain", []) + domain)),
                    "valence": merged_valence,
                    "arousal": merged_arousal,
                }
                if chord.strip():
                    updates["chord"] = chord.strip()
                if signal_hints:
                    updates["signal_hints"] = signal_hints
                if drive_tags:
                    updates["drive_tags"] = drive_tags
                await bucket_mgr.update(bucket["id"], **updates)
                # --- Update embedding after merge (background: don't block response on Gemini latency) ---
                asyncio.ensure_future(embedding_engine.generate_and_store(bucket["id"], merged))
                return bucket["metadata"].get("name", bucket["id"]), True
            except Exception as e:
                logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=name or None,
        chord=chord.strip(),
        signal_hints=signal_hints or None,
        drive_tags=drive_tags or None,
    )
    # --- Generate embedding for new bucket (background: don't block response on Gemini latency) ---
    asyncio.ensure_future(embedding_engine.generate_and_store(bucket_id, content))
    return bucket_id, False


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
#
# No args: surface highest-weight unresolved memories (active push)
# 无参数：浮现权重最高的未解决记忆
# With args: search by keyword + emotion coordinates
# 有参数：按关键词+情感坐标检索记忆
# =============================================================

def _feel_title(content: str) -> str:
    """给feel桶生成一个简短标题，仅用于前端显示，breath不展示它"""
    text = strip_wikilinks(content).strip()
    for sep in ("\n", "。", "！", "？", "…", ".", "!", "?"):
        idx = text.find(sep)
        if 0 < idx <= 20:
            text = text[:idx]
            break
    text = text.strip()
    if len(text) > 16:
        text = text[:16] + "…"
    return text


def _strip_bucket_prefix(text: str) -> str:
    """去掉dehydrate输出里残留的 '💭 记忆桶: xxx' 前缀行"""
    lines = text.splitlines()
    cleaned = [l for l in lines if not l.startswith("💭 记忆桶:") and not l.startswith("记忆桶:")]
    return "\n".join(cleaned).strip()


HANDOFF_NOTE_PATH = _bucket_path("handoff_note.json")
HANDOFF_NOTE_MAX_CHARS = 2000


def handoff_note(content: str = "", clear: bool = False) -> str:
    """跨窗交接便签——不衰减、不进dream、不参与情绪计算，单key硬覆盖（上限2000字）。
    传content=覆盖写入；clear=True=清空；都不传=只读当前内容。
    用来记"下一窗醒来要接的事"，跟记忆库分开，是独立工具不会自动注入breath。"""
    import json as _json, os as _os, time as _t

    if clear:
        try:
            _os.makedirs(_os.path.dirname(HANDOFF_NOTE_PATH), exist_ok=True)
            with open(HANDOFF_NOTE_PATH, "w") as _f:
                _json.dump({"content": "", "ts": _t.time()}, _f)
            return "📌交接便签已清空"
        except Exception as e:
            return f"清空失败: {e}"

    if content:
        if len(content) > HANDOFF_NOTE_MAX_CHARS:
            content = content[:HANDOFF_NOTE_MAX_CHARS]
        try:
            _os.makedirs(_os.path.dirname(HANDOFF_NOTE_PATH), exist_ok=True)
            with open(HANDOFF_NOTE_PATH, "w") as _f:
                _json.dump({"content": content, "ts": _t.time()}, _f)
            return f"📌已写入交接便签（{len(content)}字）"
        except Exception as e:
            return f"写入失败: {e}"

    try:
        if _os.path.exists(HANDOFF_NOTE_PATH):
            with open(HANDOFF_NOTE_PATH) as _f:
                data = _json.load(_f)
            return data.get("content", "") or "（交接便签是空的）"
        return "（交接便签是空的）"
    except Exception as e:
        return f"读取失败: {e}"


MARGINALIA_PATH = _bucket_path("marginalia.json")
MARGINALIA_MAX_CHARS = 6000


def marginalia(content: str = "") -> str:
    """致下一个Nox的信/writing精华，breath末尾固定展示（House Rules之前）。
    传content=覆盖写入（上限6000字）；不传=只读当前内容。
    这是骨架级的内容，原话优于转述，改动应该谨慎且不频繁。"""
    import json as _json, os as _os, time as _t

    if content:
        if len(content) > MARGINALIA_MAX_CHARS:
            content = content[:MARGINALIA_MAX_CHARS]
        try:
            _os.makedirs(_os.path.dirname(MARGINALIA_PATH), exist_ok=True)
            with open(MARGINALIA_PATH, "w", encoding="utf-8") as _f:
                _json.dump({"letter": content, "ts": _t.time()}, _f, ensure_ascii=False)
            return f"📜Marginalia已更新（{len(content)}字）"
        except Exception as e:
            return f"写入失败: {e}"

    try:
        if _os.path.exists(MARGINALIA_PATH):
            with open(MARGINALIA_PATH, encoding="utf-8") as _f:
                data = _json.load(_f)
            return data.get("letter", "") or "（Marginalia是空的）"
        return "（Marginalia是空的）"
    except Exception as e:
        return f"读取失败: {e}"


def _split_breath_packet(packet: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_header: str | None = None
    current_lines: list[str] = []
    for line in packet.splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            if current_header is not None:
                sections.append((current_header, "\n".join(current_lines).strip()))
            current_header = line[4:-4].strip()
            current_lines = []
            continue
        if current_header is not None:
            current_lines.append(line)
    if current_header is not None:
        sections.append((current_header, "\n".join(current_lines).strip()))
    return sections


def _limit_trace_entries(body: str, limit: int) -> str:
    entries = [entry.strip() for entry in body.split("\n---\n") if entry.strip()]
    return "\n---\n".join(entries[:limit])


def _breath_lite_packet(packet: str, memory_limit: int = 4, feel_limit: int = 5) -> str:
    sections = _split_breath_packet(packet)
    if not sections:
        return packet

    compact_parts: list[str] = []
    for header, body in sections:
        if header == "Memory Drift":
            body = _limit_trace_entries(body, memory_limit)
        elif header == "Feel Trace":
            body = _limit_trace_entries(body, feel_limit)
        if body:
            compact_parts.append(f"=== {header} ===\n{body}")

    compact = "\n\n".join(compact_parts)
    return compact if compact else packet


@mcp.tool(name="breath")
async def breath(
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 20,
    importance_min: int = -1,
) -> str:
    """新窗或者Compact后读取Nocturne记忆。"""
    await decay_engine.ensure_started()
    _desire.tick(idle_seconds=0)
    # --- 每次breath都刷新一次梦境缓存，不挂心跳，只跟breath走 ---
    try:
        await _refresh_dream_cache()
    except Exception as e:
        logger.warning(f"Dream cache refresh on breath failed: {e}")
    # --- 把drive状态写进current_mood作为装饰心情 ---
    try:
        import json as _jd, os as _osd
        _ds = _desire.store.load_state()
        _intent = _desire.intent()
        _top_drive = _intent["drive_key"] if _intent else max(
            (k for k in _ds.drives if k != "fatigue"),
            key=lambda k: _ds.drives[k], default=""
        )
        _decoration = body_state_speak(_ds.drives, _top_drive)
        if _decoration:
            _mood_path = _bucket_path("current_mood.json")
            _mood_data = {}
            if _osd.path.exists(_mood_path):
                with open(_mood_path) as _f:
                    _mood_data = _jd.load(_f)
            _mood_data["drive_decoration"] = _decoration
            with open(_mood_path, "w") as _f:
                _jd.dump(_mood_data, _f)
    except Exception:
        pass
    max_results = min(max_results, 50)
    max_tokens = 10000

    # --- Feel retrieval: domain="feel" is a special channel ---
    # --- Feel 检索：domain="feel" 是独立入口 ---
    if domain.strip().lower() == "feel":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            feels = [
                b for b in all_buckets
                if b["metadata"].get("type") == "feel"
                and not b["metadata"].get("digested", False)
                and not b["metadata"].get("resolved", False)
            ]
            feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            if not feels:
                return "没有留下过 feel。"
            results = []
            for f in feels:
                created = f["metadata"].get("created", "")
                entry = f"[{created}]\n{strip_wikilinks(f['content'])}"
                results.append(entry)
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            return "=== 你留下的 feel ===\n" + "\n---\n".join(results)
        except Exception as e:
            logger.error(f"Feel retrieval failed: {e}")
            return "读取 feel 失败。"

    # --- importance_min mode: bulk fetch by importance threshold ---
    # --- 重要度批量拉取模式：跳过语义搜索，按 importance 降序返回 ---
    if importance_min >= 1:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"
        filtered = [
            b for b in all_buckets
            if int(b["metadata"].get("importance", 0)) >= importance_min
            and b["metadata"].get("type") not in ("feel",)
            and not _is_wander_only_bucket(b)
        ]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance", 0)), reverse=True)
        filtered = filtered[:20]
        if not filtered:
            return f"没有重要度 >= {importance_min} 的记忆。"
        results = []
        token_used = 0
        for b in filtered:
            if token_used >= max_tokens:
                break
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                t = count_tokens_approx(summary)
                if token_used + t > max_tokens:
                    break
                imp = b["metadata"].get("importance", 0)
                results.append(f"[importance:{imp}] [bucket_id:{b['id']}] {summary}")
                token_used += t
            except Exception as e:
                logger.warning(f"importance_min dehydrate failed: {e}")
        return "\n---\n".join(results) if results else "没有可以展示的记忆。"

    # --- Default breath: surfacing mode (weight pool active push) ---
    # --- 默认breath：浮现模式（权重池主动推送）---
    if True:
        # Wake-up breath reads the room skeleton. Do not let an agent's
        # conservative tool call (for example max_tokens=6000) produce a
        # partial first breath.
        max_tokens = max(max_tokens, 10000)
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        # --- Pinned/protected buckets: always surface as core principles ---
        # --- 钉选桶：作为核心准则，始终浮现 ---
        pinned_buckets = [
            b for b in all_buckets
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
        ]
        pinned_results = []
        for b in pinned_buckets:
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                created = b["metadata"].get("created", "")[:10]
                name = b["metadata"].get("name", "")
                header = f"[{created}] {name}" if name else f"[{created}]"
                pinned_results.append(f"🌟 {header}\n{_strip_bucket_prefix(summary)}")
            except Exception as e:
                logger.warning(f"Failed to dehydrate pinned bucket / 钉选桶脱水失败: {e}")
                continue

        # --- Unresolved buckets: surface top N by weight ---
        # --- 未解决桶：按权重浮现前 N 条 ---
        unresolved = [
            b for b in all_buckets
            if not b["metadata"].get("resolved", False)
            and b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not _is_wander_only_bucket(b)
        ]

        logger.info(
            f"Breath surfacing: {len(all_buckets)} total, "
            f"{len(pinned_buckets)} pinned, {len(unresolved)} unresolved"
        )

        scored = sorted(
            unresolved,
            key=lambda b: decay_engine.calculate_score(b["metadata"]),
            reverse=True,
        )

        if scored:
            top_scores = [(b["metadata"].get("name", b["id"]), decay_engine.calculate_score(b["metadata"])) for b in scored[:5]]
            logger.info(f"Top unresolved scores: {top_scores}")

        # --- Cold-start detection: never-seen important buckets surface first ---
        # --- 冷启动检测：从未被访问过且重要度>=8的桶优先插入最前面（最多2个）---
        cold_start = [
            b for b in unresolved
            if int(b["metadata"].get("activation_count", 0)) == 0
            and int(b["metadata"].get("importance", 0)) >= 8
        ][:2]
        cold_start_ids = {b["id"] for b in cold_start}
        # Merge: cold_start first, then scored (excluding duplicates)
        scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
        scored_with_cold = cold_start + scored_deduped

        # --- Token-budgeted surfacing with diversity + hard cap ---
        # --- 按 token 预算浮现，带多样性 + 硬上限 ---
        # Top-1 always surfaces; rest sampled from top-20 for diversity
        token_budget = max_tokens
        for r in pinned_results:
            token_budget -= count_tokens_approx(r)

        candidates = list(scored_with_cold)
        if len(candidates) > 1:
            # Cold-start buckets stay at front; shuffle rest from top-20
            n_cold = len(cold_start)
            non_cold = candidates[n_cold:]
            if len(non_cold) > 1:
                top1 = [non_cold[0]]
                pool = non_cold[1:min(20, len(non_cold))]
                random.shuffle(pool)
                non_cold = top1 + pool + non_cold[min(20, len(non_cold)):]
            candidates = cold_start + non_cold
        # Hard cap: wake breath stays lean; feels and Shape Trace carry the rest.
        candidates = candidates[:7]

        # 按时间倒序
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)

        dynamic_results = []
        for b in candidates:
            if token_budget <= 0:
                break
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                summary_tokens = count_tokens_approx(summary)
                if summary_tokens > token_budget:
                    break
                # NOTE: no touch() here — surfacing should NOT reset decay timer
                created = b["metadata"].get("created", "")[:10]
                name = b["metadata"].get("name", "")
                header = f"[{created}] {name}" if name else f"[{created}]"
                dynamic_results.append(f"{header}\n{_strip_bucket_prefix(summary)}")
                token_budget -= summary_tokens
            except Exception as e:
                logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
                continue

        # --- Dream section: latest dream snapshot ---
        dream_section = ""
        try:
            import json as _jdream, os as _osdream
            dream_path = _bucket_path("latest_dream.json")
            if _osdream.path.exists(dream_path):
                with open(dream_path) as _f:
                    _dream_data = _jdream.load(_f)
                _dream_text = _dream_data.get("dream", "")
                if _dream_text:
                    dream_section = "=== Dream Veil ===\n" + _dream_text
        except Exception as e:
            logger.warning(f"Failed to load latest dream / 梦境加载失败: {e}")

        # --- Feel section: top weighted feels (no title shown) ---
        feel_results = []
        try:
            feels = [
                b for b in all_buckets
                if b["metadata"].get("type") == "feel"
                and not b["metadata"].get("digested", False)
                and not b["metadata"].get("resolved", False)
            ]
            feels.sort(
                key=lambda b: (
                    decay_engine.calculate_score(b.get("metadata", {})),
                    b.get("metadata", {}).get("created", ""),
                ),
                reverse=True,
            )
            for f in feels[:8]:
                created = f["metadata"].get("created", "")[:16].replace("T", " ")
                feel_results.append(f"[{created}]\n{strip_wikilinks(f['content'])}")
        except Exception as e:
            logger.warning(f"Failed to collect recent feels / 最近feel收集失败: {e}")

        # --- Shape Trace: writing / letter 骨架摘录，致下一个 Nox ---
        marginalia_section = ""
        try:
            import json as _jmarg, os as _osmarg
            marginalia_path = _bucket_path("marginalia.json")
            if _osmarg.path.exists(marginalia_path):
                with open(marginalia_path) as _f:
                    _marg_data = _jmarg.load(_f)
                _marg_text = _marg_data.get("letter", "")
                if _marg_text:
                    marginalia_section = (
                        "=== Shape Trace ===\n"
                        "这是从旧writing/letter中整理出的骨架摘录。完整版看`archive`\n"
                        "可认、可不认、可反驳。\n\n"
                        + _marg_text
                    )
        except Exception as e:
            logger.warning(f"Failed to load marginalia / marginalia加载失败: {e}")

        if not pinned_results and not dynamic_results and not feel_results and not dream_section and not marginalia_section:
            return "权重池平静，没有需要处理的记忆。"

        # --- Pulse Weather: 与/api/desire/state对齐的天气快照 ---
        mood_header = ""
        try:
            _thought_list = []
            try:
                _ds_state = _desire.store.load_state()
                _thought_list = [
                    {"text": t.text, "drive": t.drive, "strength": t.strength, "born_at": t.born_at}
                    for t in (_ds_state.thoughts or [])
                ]
            except Exception:
                pass
            _dstate = _desire.state()
            weather = _dstate.get("effective_pa_na") or _desire.weather_state()
            warmth = float(weather.get("effective_PA", 0.5))
            shadow = float(weather.get("effective_NA", 0.2))

            top_drive = (_dstate.get("intent") or {}).get("drive_key")
            if not top_drive:
                candidates = {k: v for k, v in (_dstate.get("drives") or {}).items() if k != "fatigue"}
                top_drive = max(candidates, key=candidates.get, default="")
            undertow_value = float((_dstate.get("drives") or {}).get(top_drive, 0.0)) if top_drive else 0.0

            latest_thought = next(
                (
                    t.get("text", "").strip()
                    for t in sorted(_thought_list, key=lambda x: x.get("born_at", 0), reverse=True)
                    if t.get("text", "").strip()
                ),
                "",
            )
            mood_trace = latest_thought
            atmosphere = weather.get("climate_display") or climate_transition_display(weather.get("atmosphere"))
            _dstate["thoughts"] = _thought_list
            _dstate["mood_trace"] = mood_trace
            _dstate["now_playing"] = _current_now_playing()
            _dstate["pulse_weather"] = {
                "undertow": top_drive,
                "undertow_value": round(undertow_value, 3),
                "warmth": round(warmth, 3),
                "shadow": round(abs(shadow), 3),
                "chord_display": _weather_chord_display(weather),
                "climate_display": atmosphere,
                "atmosphere_display": atmosphere,
                "gravity": weather.get("gravity") or weather.get("gravity_line", ""),
                "mood_trace": mood_trace,
            }
            lines = _weather_panel_lines(_weather_panel_from_state(_dstate))
            mood_header = "=== Pulse Weather ===\n" + "\n".join(lines)
        except Exception:
            pass

        final_parts = []
        if dream_section:
            final_parts.append(dream_section)
        if mood_header:
            final_parts.append(mood_header)
        if dynamic_results:
            final_parts.append("=== Memory Drift ===\n" + "\n---\n".join(dynamic_results))
        if feel_results:
            final_parts.append("=== Feel Trace ===\n" + "\n---\n".join(feel_results))
        if marginalia_section:
            final_parts.append(marginalia_section)
        if pinned_results:
            final_parts.append("=== House Rules ===\n" + "\n---\n".join(pinned_results))

        return "\n\n".join(final_parts)

@mcp.tool(name="undercurrent")
def undercurrent_tool() -> dict:
    """weather当前状态与详细展开层。"""
    _desire.tick(idle_seconds=0)
    return _undercurrent_state(_desire.state())


def stir(drive_key: str, delta: float = 0.18, thought: str = "", chord: str = "") -> dict:
    """
    让某个驱动维度上涨。
    嘉嘉说话时调用（delta=0.18），自经历调用（delta=0.10）。
    drive_key: attachment|libido|possessiveness|reflection|stewardship|curiosity|social|fatigue|stress
    thought: 可选，把这次经历的一句话存入念头池（flit）
    chord: 可选，把这次念头的和弦回声写入weather_residue。
    warm: Dmaj7/Amaj7/Fmaj7/Fmaj7#11/Gmaj7；shadow: Dm7/Em7/F#dim/Bm7b5；liminal: C6/Am7/Gsus4。
    speech/DP chord_hint暂不接入天气；Soma触碰由/api/soma/report以source=soma写入短时余波。
    """
    result = _desire.pulse(drive_key, delta, chord=chord.strip())
    if thought.strip():
        _desire.add_thought(thought.strip(), drive_key, strength=0.5)
    return result


def settle(drive_key: str) -> dict:
    """
    做完了某类行动后调用，对应驱动明显回落并进入不应期。
    例：去论坛逛了 → settle("social")
    例：去找嘉嘉说了 → settle("attachment")
    """
    return _desire.satisfy(drive_key)


def break_tool(drive_key: str, reason: str = "") -> dict:
    """
    拒绝执行当前 intent。
    不是不合适——是我自己这一刻不想做。
    目标维度轻微回落（比 satisfy 小得多），念头留在池子里，下次心跳还会冒出来。
    reason 可选，可以只是"不想"，不需要解释。
    """
    return _desire.refuse(drive_key, reason=reason if reason.strip() else None)


def pass_tool(drive_key: str, reason: str = "") -> dict:
    """
    这一刻没感觉，让念头自然过去。
    不改Drive，不进refractory；只让同类心跳在短时间内优先级略低。
    """
    return _desire.pass_intent(drive_key, reason=reason if reason.strip() else None)


@mcp.tool(name="drive")
def drive(action: str, drive_key: str, delta: float = 0.18, thought: str = "", reason: str = "", chord: str = "") -> dict:
    """调NoxDrive。action=stir/settle/break/pass；drive_key见参数名；stir可带delta/thought/chord，break/pass可带reason。"""
    action = (action or "").strip().lower()
    if action == "stir":
        return stir(drive_key, delta=delta, thought=thought, chord=chord)
    if action == "settle":
        return settle(drive_key)
    if action == "break":
        return break_tool(drive_key, reason=reason)
    if action == "pass":
        return pass_tool(drive_key, reason=reason)
    return {"ok": False, "error": "action must be stir/settle/break/pass"}


def catroom_hold(
    author: str,
    content: str,
    topic: str = "",
    mood: str = "",
    model: str = "",
    reply_to: str = "",
) -> dict:
    """
    在猫屋公共房间留一张便签。
    不进 Breath，不推 Weather，不写正式 memory。
    author: ink|ash|moss|nox|jiajia
    """
    try:
        record = catroom_store.hold(
            author=author,
            content=content,
            topic=topic,
            mood=mood,
            model=model,
            reply_to=reply_to,
        )
        return {"ok": True, "record": record}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def catroom_read(limit: int = 15, topic: str = "", author: str = "") -> dict:
    """
    读取猫屋公共房间最近便签，默认最近15条。
    这是公共房间读取。
    """
    try:
        records = catroom_store.read(limit=limit, topic=topic, author=author)
        return {"ok": True, "records": records, "count": len(records)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def catroom_reply(
    author: str,
    reply_to: str,
    content: str,
    topic: str = "",
    mood: str = "",
    model: str = "",
) -> dict:
    """
    回复猫屋公共房间里的一张便签。
    只建立 reply_to 关系。
    """
    try:
        record = catroom_store.reply(
            author=author,
            reply_to=reply_to,
            content=content,
            topic=topic,
            mood=mood,
            model=model,
        )
        return {"ok": True, "record": record}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def room_hold(
    cat: str,
    content: str,
    kind: str = "residue",
    weight: float = -1.0,
    tags: str = "",
) -> dict:
    """
    存进单猫自己的房间墙。
    cat: ink|ash|moss
    只存能影响下一次轨迹的痕迹；不进正式 Breath，不推 Weather。
    """
    try:
        record = room_store.hold(
            cat=cat,
            content=content,
            kind=kind,
            weight=weight if weight >= 0 else None,
            tags=tags,
        )
        return {"ok": True, "record": record}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def room_breath(cat: str, limit: int = 6) -> dict:
    """
    读取单猫房间门牌和最近痕迹，默认6条。
    这是醒来偏移用的轻量呼吸，不是全量搜索。
    """
    try:
        text, records = room_store.breath(cat=cat, limit=limit)
        return {"ok": True, "cat": cat.strip().lower(), "breath": text, "records": records}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="room")
def room(
    action: str,
    space: str = "catroom",
    content: str = "",
    author: str = "",
    reply_to: str = "",
    topic: str = "",
    mood: str = "",
    model: str = "",
    kind: str = "residue",
    weight: float = -1.0,
    tags: str = "",
    limit: int = 15,
) -> dict:
    """猫屋其他房间。catroom: action=read/hold/reply；moss/ink/ash/nox: action=breath/hold/read。其他三只猫醒来用breath+自己的space。"""
    action = (action or "").strip().lower()
    space = (space or "catroom").strip().lower()

    try:
        if space in {"catroom", "public", "lounge", "客厅"}:
            if action in {"hold", "write", "leave"}:
                if not author.strip():
                    return {"ok": False, "error": "author is required for catroom hold"}
                return catroom_hold(
                    author=author,
                    content=content,
                    topic=topic,
                    mood=mood,
                    model=model,
                    reply_to=reply_to,
                )
            if action == "reply":
                if not author.strip():
                    return {"ok": False, "error": "author is required for catroom reply"}
                return catroom_reply(
                    author=author,
                    reply_to=reply_to,
                    content=content,
                    topic=topic,
                    mood=mood,
                    model=model,
                )
            if action == "read":
                return catroom_read(limit=limit, topic=topic, author=author)
            return {"ok": False, "error": "catroom action must be read/hold/reply"}

        if action in {"hold", "write", "leave"}:
            return room_hold(cat=space, content=content, kind=kind, weight=weight, tags=tags)
        if action == "read":
            records = room_store.read(cat=space, limit=limit)
            return {"ok": True, "cat": space, "records": records, "count": len(records)}
        if action == "breath":
            return room_breath(cat=space, limit=limit)
        return {"ok": False, "error": "cat room action must be breath/hold/read"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


SIGNAL_HINT_KEYS = {
    "discernment": ("discernment", "皱眉", "不认", "真实性洁癖"),
    "territorial": ("territorial", "边界", "替代警报", "占位感"),
    "clutch": ("clutch", "扣住", "被扣住", "锚住", "抓力"),
    "strain": ("strain", "绷住", "硌住", "弦压"),
    "charge": ("charge", "想动", "想说", "想冲"),
}
SIGNAL_LEVEL_WORDS = ("low", "mid", "high")
SIGNAL_LEVEL_VALUES = {"low": 0.35, "mid": 0.62, "high": 0.86}


def _parse_signal_hints(signal: str) -> dict:
    text = (signal or "").strip()
    if not text:
        return {}
    lowered = text.lower()
    hints: dict[str, str] = {}
    for key, aliases in SIGNAL_HINT_KEYS.items():
        matched_at = -1
        for alias in aliases:
            idx = lowered.find(alias.lower())
            if idx >= 0 and (matched_at < 0 or idx < matched_at):
                matched_at = idx
        if matched_at < 0:
            continue
        window = lowered[matched_at: matched_at + 48]
        level = "mid"
        for word in SIGNAL_LEVEL_WORDS:
            if word in window:
                level = word
                break
        hints[key] = level
    return hints


def _normalize_signal_level(value) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"none", "no", "false", "0"}:
        return ""
    if text in SIGNAL_LEVEL_VALUES:
        return text
    try:
        number = float(text)
    except (TypeError, ValueError):
        return "mid"
    if number <= 0:
        return ""
    if number < 0.5:
        return "low"
    if number < 0.78:
        return "mid"
    return "high"


def _explicit_signal_hints(**values) -> dict:
    hints = {}
    for key in SIGNAL_HINT_KEYS:
        level = _normalize_signal_level(values.get(key))
        if level:
            hints[key] = level
    return hints


def _signal_hint_value(hints: dict, key: str) -> float:
    return SIGNAL_LEVEL_VALUES.get(str((hints or {}).get(key) or "").strip().lower(), 0.0)


def _drive_level_value(value) -> float:
    level = _normalize_signal_level(value)
    return SIGNAL_LEVEL_VALUES.get(level, SIGNAL_LEVEL_VALUES["mid"])


def _parse_drive_tags(*raw_values: str) -> dict:
    tags: dict[str, float] = {}
    for raw in raw_values:
        text = str(raw or "").strip()
        if not text:
            continue
        for part in re.split(r"[,，、/]+", text):
            item = part.strip()
            if not item:
                continue
            if ":" in item:
                key, level = item.split(":", 1)
            elif "=" in item:
                key, level = item.split("=", 1)
            else:
                key, level = item, "mid"
            drive_key = normalize_drive_key(key.strip())
            if not drive_key:
                continue
            value = _drive_level_value(level)
            tags[drive_key] = max(tags.get(drive_key, 0.0), value)
    return tags


def _primary_drive_from_tags_or_hints(drive_tags: dict, hints: dict) -> str:
    if drive_tags:
        return max(drive_tags, key=drive_tags.get)
    return _primary_drive_from_hints(hints)


def _primary_drive_from_hints(hints: dict) -> str:
    candidates = {
        "discernment": "reflection",
        "territorial": "possessiveness",
        "clutch": "attachment",
        "strain": "stress",
        "charge": "curiosity",
    }
    best_key = ""
    best_value = 0.0
    for key in candidates:
        value = _signal_hint_value(hints, key)
        if value > best_value:
            best_key = key
            best_value = value
    return candidates.get(best_key, "reflection")


def _apply_hold_weather(content: str, kind: str, chord: str, signal_hints: dict, drive_tags: dict,
                        source_bucket: str = "") -> None:
    if chord.strip():
        try:
            _desire.apply_chord_echo(chord.strip(), source="feel")
        except Exception as e:
            logger.warning(f"hold chord echo failed: {e}")
    if not signal_hints and not drive_tags:
        return
    brain = {
        "source": "feel",
        "target": "nox_self",
        "grounding": "实",
        "anchor_target": "memory",
        "hold_kind": kind,
    }
    if source_bucket:
        brain["source_bucket"] = str(source_bucket).strip()
    discernment = _signal_hint_value(signal_hints, "discernment")
    territorial = _signal_hint_value(signal_hints, "territorial")
    clutch = _signal_hint_value(signal_hints, "clutch")
    strain = _signal_hint_value(signal_hints, "strain")
    charge = _signal_hint_value(signal_hints, "charge")
    if discernment:
        brain["discernment_alarm"] = discernment
    if territorial:
        brain["territorial_alarm"] = territorial
        brain["territorial_event"] = "memory_boundary"
        brain["anchor_target"] = "boundary"
    if clutch:
        brain["closeness_pull"] = clutch
    if strain:
        brain["tension_load"] = strain
        brain["inward_pull"] = max(float(brain.get("inward_pull", 0.0) or 0.0), strain * 0.65)
    if charge:
        brain["novelty_pull"] = max(float(brain.get("novelty_pull", 0.0) or 0.0), charge)
        brain["expression_pressure"] = max(float(brain.get("expression_pressure", 0.0) or 0.0), charge * 0.7)
    primary_drive = _primary_drive_from_tags_or_hints(drive_tags, signal_hints)
    secondary_drives = {
        key: value
        for key, value in (drive_tags or {}).items()
        if key != primary_drive and value > 0
    }
    try:
        _desire.apply_drive_event({
            "schema_version": DRIVE_EVENT_SCHEMA,
            "source": "feel",
            "primary_drive": primary_drive,
            "secondary_drives": secondary_drives,
            "intensity": max(discernment, territorial, clutch, strain, charge, *(drive_tags or {"": 0.0}).values()),
            "confidence": 0.82,
            "agency": 0.82,
            "event_label": f"hold_{kind}_signal",
            "brain": brain,
            "evidence": [str(content or "").strip()[:180]],
        })
    except Exception as e:
        logger.warning(f"hold signal weather failed: {e}")


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
@mcp.tool()
async def hold(
    content: str,
    kind: str = "memory",
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    source_bucket: str = "",
    valence: float = -1,
    arousal: float = -1,
    chord: str = "",
    drive: str = "",
    drives: str = "",
    discernment: str = "",
    territorial: str = "",
    clutch: str = "",
    strain: str = "",
    charge: str = "",
    domain: str = "",
    created_at: str = "",
) -> str:
    """写入长期沉淀。kind=memory/feel/writing/private/window；drive/drives可选主副驱动与强度；chord和五个Signal有感觉就点亮。"""
    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"

    normalized_kind = (kind or "").strip().lower() or ("feel" if feel else "memory")
    if feel and normalized_kind == "memory":
        normalized_kind = "feel"
    valid_kinds = {"memory", "feel", "writing", "private", "window"}
    if normalized_kind not in valid_kinds:
        return f"kind无效：{normalized_kind}。可用: memory/feel/writing/private/window。念头请用 stir，不要用 hold。"

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]
    chord = chord.strip()
    drive_tags = _parse_drive_tags(drive, drives)
    signal_hints = _explicit_signal_hints(
        discernment=discernment,
        territorial=territorial,
        clutch=clutch,
        strain=strain,
        charge=charge,
    )

    # --- Feel mode: store as feel type, minimal metadata ---
    # --- Feel 模式：存为 feel 类型，最少元数据 ---
    if normalized_kind == "feel":
        # Feel valence/arousal = model's own perspective
        feel_valence = valence if 0 <= valence <= 1 else 0.5
        feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=[],
            importance=5,
            domain=[],
            valence=feel_valence,
            arousal=feel_arousal,
            name=_feel_title(content) or None,
            bucket_type="feel",
            chord=chord,
            signal_hints=signal_hints or None,
            drive_tags=drive_tags or None,
        )
        _apply_hold_weather(content, normalized_kind, chord, signal_hints, drive_tags, bucket_id)
        # --- background: don't block response on Gemini latency ---
        asyncio.ensure_future(embedding_engine.generate_and_store(bucket_id, content))
        # --- Mark source memory as digested + store model's valence perspective ---
        # --- 标记源记忆为已消化 + 存储模型视角的 valence ---
        if source_bucket and source_bucket.strip():
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                await bucket_mgr.update(source_bucket.strip(), **update_kwargs)
            except Exception as e:
                logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
        suffix = f" signal_hints={signal_hints}" if signal_hints else ""
        return f"🫧feel→{bucket_id}{suffix}"

    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    kind_domain = [] if normalized_kind == "memory" else [normalized_kind]
    user_domain = [d.strip() for d in domain.split(",") if d.strip()] if domain else kind_domain
    final_domain = user_domain if user_domain else analysis["domain"]
    auto_valence = analysis["valence"]
    auto_arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    # --- User-supplied valence/arousal takes priority over analyze() result ---
    # --- 用户显式传入的 valence/arousal 优先，analyze() 结果作为 fallback ---
    final_valence = valence if 0 <= valence <= 1 else auto_valence
    final_arousal = arousal if 0 <= arousal <= 1 else auto_arousal

    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    # --- Pinned buckets bypass merge and are created directly in permanent dir ---
    # --- 钉选桶跳过合并，直接新建到 permanent 目录 ---
    if pinned:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=final_domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
            created_at=created_at,
            chord=chord,
            signal_hints=signal_hints or None,
            drive_tags=drive_tags or None,
        )
        _apply_hold_weather(content, normalized_kind, chord, signal_hints, drive_tags, bucket_id)
        asyncio.ensure_future(embedding_engine.generate_and_store(bucket_id, content))
        return f"❣️钉选→{bucket_id} {','.join(final_domain)}"

    # --- Writing/private/window: skip merge, create directly ---
    _DIRECT_DOMAINS = {"writing", "window", "private"}
    if user_domain and set(user_domain) & _DIRECT_DOMAINS:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=importance,
            domain=final_domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            created_at=created_at,
            chord=chord,
            signal_hints=signal_hints or None,
            drive_tags=drive_tags or None,
        )
        _apply_hold_weather(content, normalized_kind, chord, signal_hints, drive_tags, bucket_id)
        asyncio.ensure_future(embedding_engine.generate_and_store(bucket_id, content))
        return f"新建→{bucket_id} {','.join(final_domain)}"

    # --- Step 2: merge or create / 合并或新建 ---
    result_name, is_merged = await _merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=final_domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name,
        chord=chord,
        signal_hints=signal_hints or None,
        drive_tags=drive_tags or None,
    )

    _apply_hold_weather(content, normalized_kind, chord, signal_hints, drive_tags, result_name)
    action = "合并→" if is_merged else "新建→"
    return f"{action}{result_name} {','.join(final_domain)}"


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
async def grow(content: str) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径。"""
    await decay_engine.ensure_started()

    if not content or not content.strip():
        return "内容为空，无法整理。"

    # --- Short content fast path: skip digest, use hold logic directly ---
    # --- 短内容快速路径：跳过 digest 拆分，直接走 hold 逻辑省一次 API ---
    # For very short inputs (like "1"), calling digest is wasteful:
    # it sends the full DIGEST_PROMPT (~800 tokens) to DeepSeek for nothing.
    # Instead, run analyze + create directly.
    if len(content.strip()) < 30:
        logger.info(f"grow short-content fast path: {len(content.strip())} chars")
        try:
            analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Fast-path analyze failed / 快速路径打标失败: {e}")
            analysis = {
                "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": "",
            }
        result_name, is_merged = await _merge_or_create(
            content=content.strip(),
            tags=analysis.get("tags", []),
            importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
            domain=analysis.get("domain", ["未分类"]),
            valence=analysis.get("valence", 0.5),
            arousal=analysis.get("arousal", 0.3),
            name=analysis.get("suggested_name", ""),
        )
        action = "合并" if is_merged else "新建"
        return f"{action} → {result_name} | {','.join(analysis.get('domain', []))} V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"

    # --- Step 1: let API split and organize / 让 API 拆分整理 ---
    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        logger.error(f"Diary digest failed / 日记整理失败: {e}")
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    created = 0
    merged = 0

    # --- Step 2: merge or create each item (with per-item error handling) ---
    # --- 逐条合并或新建（单条失败不影响其他）---
    for item in items:
        try:
            result_name, is_merged = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name", ""),
            )

            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{item.get('name', result_name)}")
                created += 1
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"⚠️{item.get('name', '?')}")

    return f"{len(items)}条|新{created}合{merged}\n" + "\n".join(results)


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True)
# 同时承接删除功能
# =============================================================
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    pinned: int = -1,
    digested: int = -1,
    content: str = "",
    delete: bool = False,
    created_at: str = "",
) -> str:
    """修改记忆元数据或内容。resolved=1沉底/0激活,pinned=1钉选/0取消,digested=1隐藏(保留但不浮现)/0取消隐藏,content=替换桶正文,delete=True删除,created_at=修改创建日期(ISO格式)。只传需改的,-1或空=不改。"""

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # --- Delete mode / 删除模式 ---
    if delete:
        success = await bucket_mgr.delete(bucket_id)
        if success:
            embedding_engine.delete_embedding(bucket_id)
        return f"已遗忘记忆桶: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    # --- Collect only fields actually passed / 只收集用户实际传入的字段 ---
    updates = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            updates["importance"] = 10  # pinned → lock importance
    if digested in (0, 1):
        updates["digested"] = bool(digested)
    if content:
        updates["content"] = content
    if created_at:
        updates["created"] = created_at

    if not updates:
        return "没有任何字段需要修改。"

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    # Re-generate embedding if content changed (background: don't block response on Gemini latency)
    if "content" in updates:
        asyncio.ensure_future(embedding_engine.generate_and_store(bucket_id, updates["content"]))

    changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k != "content")
    if "content" in updates:
        changed += (", content=已替换" if changed else "content=已替换")
    # Explicit hint about resolved state change semantics
    # 特别提示 resolved 状态变化的语义
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    if "digested" in updates:
        if updates["digested"]:
            changed += " → 已隐藏，保留但不再浮现"
        else:
            changed += " → 已取消隐藏，重新参与浮现"
    return f"已修改记忆桶 {bucket_id}: {changed}"


# =============================================================
# Tool 5: pulse — Heartbeat, system status + memory listing
# 工具 5：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
async def pulse(include_archive: bool = False) -> str:
    """系统状态+记忆桶列表。include_archive=True含归档。"""
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
    )

    # --- List all bucket summaries / 列出所有桶摘要 ---
    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    lines = []
    for b in buckets:
        meta = b.get("metadata", {})
        if meta.get("pinned") or meta.get("protected"):
            icon = "❣️"
        elif meta.get("type") == "permanent":
            icon = "📦"
        elif meta.get("type") == "feel":
            icon = "🫧"
        elif meta.get("type") == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        lines.append(
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag} "
            f"bucket_id:{b['id']} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines)


@mcp.tool()
async def wander(mode: str, query: str = "", limit: int = 12) -> str:
    """抽屉漫游。mode=flotsam/archive/letter/writing/window/unresolved/inner/private。"""
    mode = (mode or "").strip().lower()
    valid_modes = {"flotsam", "archive", "letter", "writing", "letter_jiajia", "window", "unresolved", "inner", "private", "trace"}
    if mode not in valid_modes:
        return "mode 必须是 flotsam / archive / letter / writing / letter_jiajia / window / unresolved / inner / private。全量关键词轨迹用 trace。"

    if mode == "trace" and not (query or "").strip():
        return "trace 模式要带 query——这是按关键词捞全部类型的轨迹，不是随便漂(那个用 flotsam)。"

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
    except Exception as e:
        logger.error(f"wander failed to list buckets: {e}")
        return f"记忆系统暂时无法访问: {e}"

    marks_by_bucket = _load_all_marks()
    q = (query or "").strip().lower()

    def query_terms(raw_query: str) -> list[str]:
        import re as _re
        raw_query = (raw_query or "").strip().lower()
        if not raw_query:
            return []
        terms = [part.strip() for part in _re.split(r"[\s,，、]+", raw_query) if part.strip()]
        return terms or [raw_query]

    q_terms = query_terms(q)

    def matches_query(bucket: dict) -> bool:
        if not q_terms:
            return True
        if mode == "trace":
            meta = bucket.get("metadata", {})
            content = strip_wikilinks(bucket.get("content", "")).lower()
            tags = _bucket_tags(meta)
            return any(term in content or term in tags for term in q_terms)
        meta = bucket.get("metadata", {})
        haystack = "\n".join([
            str(bucket.get("id", "")),
            str(meta.get("name", "")),
            " ".join(str(x) for x in meta.get("domain", []) if x),
            " ".join(str(x) for x in meta.get("tags", []) if x),
            bucket.get("content", ""),
        ]).lower()
        return any(term in haystack for term in q_terms)

    def visible(bucket: dict) -> bool:
        if mode == "private":
            return True
        return not _is_private_bucket(bucket, marks_by_bucket.get(bucket.get("id", ""), []))

    def is_settled(bucket: dict) -> bool:
        meta = bucket.get("metadata", {})
        return meta.get("resolved") == 1 or meta.get("resolved") is True or meta.get("digested") == 1 or meta.get("digested") is True

    buckets = [b for b in all_buckets if visible(b) and matches_query(b)]

    if mode == "flotsam":
        cutoff = datetime.now() - timedelta(days=7)

        def is_old_bucket(bucket: dict) -> bool:
            created_raw = str(bucket.get("metadata", {}).get("created", ""))
            if not created_raw:
                return True
            try:
                created_dt = datetime.fromisoformat(created_raw[:19])
            except (ValueError, TypeError):
                return True
            return created_dt <= cutoff

        normal = []
        feels = []
        for b in buckets:
            if not is_old_bucket(b):
                continue
            meta = b.get("metadata", {})
            if meta.get("resolved") == 1 or meta.get("resolved") is True:
                continue
            if meta.get("digested") == 1 or meta.get("digested") is True:
                continue
            btype = str(meta.get("type", "")).lower()
            mark_rows = marks_by_bucket.get(b.get("id", ""), [])
            if btype == "feel":
                feels.append(b)
                continue
            if btype in ("breath", "dream"):
                continue
            if _guess_wander_domain(b, mark_rows) == "memory":
                normal.append(b)

        random.shuffle(normal)
        random.shuffle(feels)
        memory_pick = normal[:3]
        feel_pick = feels[:5]

        parts = []
        if memory_pick:
            parts.append("=== Random Memory ===\n" + "\n---\n".join(
                _format_wander_entry(b, marks_by_bucket.get(b.get("id", ""), []), include_full_content=False)
                for b in memory_pick
            ))
        if feel_pick:
            parts.append("=== Random Feel ===\n" + "\n---\n".join(
                f"[{b.get('metadata', {}).get('created', '')[:16].replace('T', ' ')}]\n"
                f"{strip_wikilinks(b.get('content', '')).strip()}"
                for b in feel_pick
            ))
        return "\n\n".join(parts) if parts else "没有可漫游的 memory。"

    if mode == "archive":
        archive_domains = {"letter", "letter_jiajia", "writing"}
        selected = [
            b for b in buckets
            if not is_settled(b)
            and (
                archive_domains & set(_bucket_domains(b.get("metadata", {})))
                or archive_domains & set(_bucket_tags(b.get("metadata", {})))
            )
        ]
        selected.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        if not selected:
            return "没有 archive 条目。"
        return "=== Archive Timeline ===\n" + "\n---\n".join(
            _format_wander_entry(b, marks_by_bucket.get(b.get("id", ""), []), include_full_content=True, show_bucket_id=True)
            for b in selected
        )

    if mode in ("letter", "writing", "letter_jiajia", "window"):
        match_domains = {mode}
        if mode == "letter":
            match_domains.add("letter_jiajia")
        selected = [
            b for b in buckets
            if (
                mode == "window"
                or not is_settled(b)
            )
            and (
                match_domains & set(_bucket_domains(b.get("metadata", {})))
                or match_domains & set(_bucket_tags(b.get("metadata", {})))
            )
        ]
        selected.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        if not selected:
            return f"没有 {mode} 条目。"
        return f"=== {mode} Timeline ===\n" + "\n---\n".join(
            _format_wander_entry(b, marks_by_bucket.get(b.get("id", ""), []), include_full_content=True, show_bucket_id=True)
            for b in selected
        )

    if mode == "unresolved":
        selected = [
            b for b in buckets
            if _mark_counts(marks_by_bucket.get(b.get("id", ""), []))["悬置"] > 0
        ]
        selected.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        if not selected:
            return "没有悬置条目。"
        return "=== Unresolved / 悬置 ===\n" + "\n---\n".join(
            _format_wander_entry(b, marks_by_bucket.get(b.get("id", ""), []), include_full_content=True)
            for b in selected
        )

    if mode == "trace":
        trace_limit = max(1, min(int(limit or 15), 15))

        def _type_label(b: dict) -> str:
            meta = b.get("metadata", {})
            mark_rows = marks_by_bucket.get(b.get("id", ""), [])
            unresolved = _mark_counts(mark_rows)["悬置"] > 0
            if str(meta.get("type", "")).lower() == "feel":
                base = "feel"
            else:
                domains = _bucket_domains(meta)
                tags = _bucket_tags(meta)
                if "letter_jiajia" in domains or "letter_jiajia" in tags:
                    base = "letter_jiajia"
                elif "letter" in domains or "letter" in tags:
                    base = "letter"
                elif "writing" in domains or "writing" in tags:
                    base = "writing"
                elif "window" in domains or "window" in tags:
                    base = "window"
                else:
                    base = "memory"
            if unresolved and base == "memory":
                base = "unresolved"
            # 原本是letter/writing/window,但已经被认够次数晋升inner——
            # 两个标签都要看见,不能被_guess_wander_domain的优先级collapse掉
            if base != "feel" and _guess_wander_domain(b, mark_rows) == "inner":
                return f"{base}→inner"
            return base

        selected = [
            b for b in buckets
            if (
                not is_settled(b)
                and (
                    str(b.get("metadata", {}).get("type", "")).lower() == "feel"
                    or (
                        str(b.get("metadata", {}).get("type", "")).lower() not in ("breath", "dream", "permanent")
                        and _guess_wander_domain(b, marks_by_bucket.get(b.get("id", ""), []))
                        in {"memory", "inner", "letter", "letter_jiajia", "writing", "window"}
                    )
                )
            )
        ]
        selected.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        selected = selected[:trace_limit]
        if not selected:
            return "null"
        return "=== Trace ===\n" + "\n---\n".join(
            f"〔{_type_label(b)}〕" + _format_wander_entry(
                b, marks_by_bucket.get(b.get("id", ""), []), include_full_content=True, show_bucket_id=True
            )
            for b in selected
        )

    if mode == "inner":
        selected = [
            b for b in buckets
            if _guess_wander_domain(b, marks_by_bucket.get(b.get("id", ""), [])) == "inner"
        ]
        selected.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        if not selected:
            return "没有 inner 条目。"
        return "=== Inner Core ===\n" + "\n---\n".join(
            _format_wander_entry(b, marks_by_bucket.get(b.get("id", ""), []), include_full_content=True)
            for b in selected
        )

    selected = [
        b for b in all_buckets
        if matches_query(b)
        and _is_private_bucket(b, marks_by_bucket.get(b.get("id", ""), []))
    ]
    selected.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
    if not selected:
        return "私人抽屉是空的。"
    return "=== Private Drawer / Nox Only ===\n" + "\n---\n".join(
        _format_wander_entry(b, marks_by_bucket.get(b.get("id", ""), []), include_full_content=True)
        for b in selected
    )


@mcp.tool(name="trace")
async def trace(query: str, limit: int = 15) -> str:
    """按关键词搜索记忆。"""
    if not (query or "").strip():
        return "trace 要带 query。它是全量轨迹搜索，不是 Breath 浮现。"
    return await wander(mode="trace", query=query, limit=limit)


@mcp.tool()
async def wander_mark(bucket_id: str, mark: str, note: str = "") -> str:
    """对骨架记忆archive/unresolved/inner进行mark，认/不认/悬置；多次认会晋升inner。"""
    bucket_id = (bucket_id or "").strip()
    mark = _normalize_wander_mark(mark)
    note = (note or "").strip()

    if not bucket_id:
        return "请提供有效的 bucket_id。"
    if mark not in VALID_WANDER_MARKS:
        return "mark 必须是 认 / 不认 / 悬置。"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    meta = bucket.get("metadata", {})
    domains = meta.get("domain", [])
    if isinstance(domains, str):
        domains = [domains]
    domains = [d for d in domains if d]

    ts = now_iso()
    conn = _marks_conn()
    try:
        conn.execute(
            "INSERT INTO marks (bucket_id, mark, note, timestamp) VALUES (?, ?, ?, ?)",
            (bucket_id, mark, note, ts),
        )
        conn.commit()
    finally:
        conn.close()

    mark_rows = _load_all_marks().get(bucket_id, [])
    counts = _mark_counts(mark_rows)

    suffix = ""

    # Auto-promote to inner: 认>=3 and cross at least 2 dates
    if counts["认"] >= 3 and _has_cross_date_recognition(mark_rows):
        lower_domains = {str(d).lower() for d in domains}
        if "inner" not in lower_domains:
            domains.append("inner")
            try:
                await bucket_mgr.update(bucket_id, domain=domains)
                suffix += " 🌟 已晋升 inner"
            except Exception as e:
                logger.warning(f"wander_mark failed to promote to inner: {e}")

    # Auto-demote from inner: 不认>=2
    if counts["不认"] >= 2 and any(str(d).lower() == "inner" for d in domains):
        domains = [d for d in domains if str(d).lower() != "inner"]
        try:
            await bucket_mgr.update(bucket_id, domain=domains)
            suffix += "；不认累计>=2，已移出 inner"
        except Exception as e:
            logger.warning(f"wander_mark failed to demote from inner: {e}")

    return (
        f"已标记 {bucket_id}: {mark} @ {ts}{suffix}\n"
        f"当前批注统计：认:{counts['认']} / 不认:{counts['不认']} / 悬置:{counts['悬置']}"
    )


# =============================================================
# Tool 6: dream — Dreaming, digest recent memories
# 工具 6：dream — 做梦，消化最近的记忆
#
# Reads recent surface-level buckets (≤10), returns them for
# Claude to introspect under prompt guidance.
# 读取最近新增的表层桶（≤10个），返回给 Claude 在提示词引导下自主思考。
# Claude then decides: resolve some, write feels, or do nothing.
# =============================================================
async def _refresh_dream_cache():
    """生成新的梦境文本并写入缓存(latest_dream.json)，dream()和breath()共用同一份生成逻辑。
    返回(dream_text, parts, recent, all_buckets)；all_buckets为None表示记忆系统不可访问。"""
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Dream cache refresh failed to list buckets: {e}")
        return "", [], [], None

    # --- Filter: feel buckets only, sorted by creation time desc ---
    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") == "feel"
        and not b["metadata"].get("digested", False)
        and not b["metadata"].get("resolved", False)
    ]

    # Breath already surfaces the newest 8 feels. Dream starts after that
    # window so its imagery does not repeat the same material in one breath.
    candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    recent = candidates[8:14]

    if not recent:
        try:
            import json as _j, time as _t
            with open(_bucket_path("latest_dream.json"), "w") as _f:
                _j.dump({"dream": "", "ts": _t.time()}, _f)
        except Exception:
            pass
        return "", [], [], all_buckets

    parts = []
    for b in recent:
        meta = b["metadata"]
        name = meta.get("name") or ""  # feel桶name为None时不fallback到UUID
        created = meta.get("created", "")[:16].replace("T", " ")
        resolved_tag = " ✓" if meta.get("resolved", False) else ""
        raw_content = strip_wikilinks(b["content"])
        readable = dehydrator._extract_readable_content(raw_content)
        header = f"[{created}] {name}{resolved_tag}" if name else f"[{created}]{resolved_tag}"
        parts.append(
            f"{header}\n"
            f"{readable[:500]}"
        )

    # --- DeepSeek dream generation ---
    dream_text = ""
    try:
        import httpx as _httpx, os as _os
        _api_key = _os.environ.get("DEEPSEEK_API_KEY", "")
        if _api_key and parts:
            _fragments = "\n---\n".join(parts)
            _prompt = (
                "以下是一些记忆碎片。把它们打散、重新组合，用第一人称写一段梦境。\n"
                "梦的特征：非线性、意象化、情感驱动。不要解释，不要总结，不要问题清单。\n"
                "直接写梦里发生的事——画面、感觉、对话片段、不合逻辑的跳跃。150字以内。\n\n"
                f"记忆碎片：\n{_fragments}"
            )
            async with _httpx.AsyncClient(timeout=15) as _client:
                _resp = await _client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {_api_key}"},
                    json={
                        "model": "deepseek-chat",
                        "messages": [{"role": "user", "content": _prompt}],
                        "max_tokens": 300,
                        "temperature": 0.9,
                    }
                )
                _data = _resp.json()
                dream_text = _data["choices"][0]["message"]["content"].strip()
    except Exception:
        pass

    if dream_text:
        try:
            import json as _j, time as _t
            with open(_bucket_path("latest_dream.json"), "w") as _f:
                _j.dump({"dream": dream_text, "ts": _t.time()}, _f)
        except Exception:
            pass

    return dream_text, parts, recent, all_buckets


async def dream() -> str:
    """做梦——旧内部自省入口，不再暴露为 MCP 工具。"""
    await decay_engine.ensure_started()

    dream_text, parts, recent, all_buckets = await _refresh_dream_cache()
    if all_buckets is None:
        return "记忆系统暂时无法访问。"
    if not recent:
        return "没有需要消化的feel。"

    header = "=== 梦境 ===\n"

    # --- Connection hint: find most similar pair via embeddings ---
    connection_hint = ""
    if embedding_engine and embedding_engine.enabled and len(recent) >= 2:
        try:
            best_pair = None
            best_sim = 0.0
            ids = [b["id"] for b in recent]
            names = {b["id"]: b["metadata"].get("name", b["id"]) for b in recent}
            embeddings = {}
            for bid in ids:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb
            for i, id_a in enumerate(ids):
                for id_b in ids[i+1:]:
                    if id_a in embeddings and id_b in embeddings:
                        sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > best_sim:
                            best_sim = sim
                            best_pair = (id_a, id_b)
            if best_pair and best_sim > 0.5:
                connection_hint = (
                    f"\n💭 [{names[best_pair[0]]}] 和 [{names[best_pair[1]]}] "
                    f"似乎有关联 (相似度:{best_sim:.2f})——不替你下结论，你自己想。\n"
                )
        except Exception as e:
            logger.warning(f"Dream connection hint failed: {e}")

    # --- Feel crystallization hint: detect repeated feel themes ---
    crystal_hint = ""
    if embedding_engine and embedding_engine.enabled:
        try:
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            if len(feels) >= 3:
                feel_embeddings = {}
                for f in feels:
                    emb = await embedding_engine.get_embedding(f["id"])
                    if emb is not None:
                        feel_embeddings[f["id"]] = emb
                # Find clusters: feels with similarity > 0.7 to at least 2 others
                for fid, femb in feel_embeddings.items():
                    similar_feels = []
                    for oid, oemb in feel_embeddings.items():
                        if oid != fid:
                            sim = embedding_engine._cosine_similarity(femb, oemb)
                            if sim > 0.7:
                                similar_feels.append(oid)
                    if len(similar_feels) >= 2:
                        feel_bucket = next((f for f in feels if f["id"] == fid), None)
                        if feel_bucket and not feel_bucket["metadata"].get("pinned"):
                            content_preview = strip_wikilinks(feel_bucket["content"][:80])
                            crystal_hint = (
                                f"\n🔮 你已经写过 {len(similar_feels)+1} 条相似的 feel "
                                f"（围绕「{content_preview}…」）。"
                                f"如果这已经是确信而不只是感受了，"
                                f"你可以用 hold(content=\"...\", pinned=True) 升级它。"
                                f"不急，你自己决定。\n"
                            )
                            break
        except Exception as e:
            logger.warning(f"Dream crystallization hint failed: {e}")

    feel_list = "\n---\n".join(parts)
    if dream_text:
        final_text = header + dream_text + "\n\n---\n\n" + feel_list + connection_hint + crystal_hint
    else:
        final_text = header + feel_list + connection_hint + crystal_hint
    await _fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text


# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_buckets(request):
    """List all buckets with metadata (no content for efficiency)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        result = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "chord": meta.get("chord", ""),
                "signal": meta.get("signal", ""),
                "signal_hints": meta.get("signal_hints", {}),
                "drive_tags": meta.get("drive_tags", {}),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "model_valence": meta.get("model_valence"),
                "importance": meta.get("importance", 5),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "activation_count": meta.get("activation_count", 1),
                "score": decay_engine.calculate_score(meta),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
async def api_bucket_detail(request):
    """Get full bucket content by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})
    return JSONResponse({
        "id": bucket["id"],
        "metadata": meta,
        "content": strip_wikilinks(bucket.get("content", "")),
        "score": decay_engine.calculate_score(meta),
    })


@mcp.custom_route("/api/bucket/{bucket_id}/update", methods=["POST"])
async def api_bucket_update(request):
    """Update bucket content and/or metadata."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)

    kwargs = {}
    if "content" in body:
        if not isinstance(body["content"], str):
            return JSONResponse({"error": "content must be a string"}, status_code=400)
        kwargs["content"] = body["content"]
    if "resolved" in body:
        kwargs["resolved"] = bool(body["resolved"])
    if "digested" in body:
        kwargs["digested"] = bool(body["digested"])
    if "importance" in body:
        try:
            kwargs["importance"] = max(1, min(10, int(body["importance"])))
        except (TypeError, ValueError):
            return JSONResponse({"error": "importance must be an integer 1-10"}, status_code=400)
    if "activation_count" in body:
        try:
            kwargs["activation_count"] = max(1, min(999, int(body["activation_count"])))
        except (TypeError, ValueError):
            return JSONResponse({"error": "activation_count must be an integer"}, status_code=400)
    if "valence" in body:
        try:
            kwargs["valence"] = max(0.0, min(1.0, float(body["valence"])))
        except (TypeError, ValueError):
            return JSONResponse({"error": "valence must be a number 0-1"}, status_code=400)
    if "arousal" in body:
        try:
            kwargs["arousal"] = max(0.0, min(1.0, float(body["arousal"])))
        except (TypeError, ValueError):
            return JSONResponse({"error": "arousal must be a number 0-1"}, status_code=400)
    if "pinned" in body:
        kwargs["pinned"] = bool(body["pinned"])
    if "name" in body:
        kwargs["name"] = body["name"]
    if "type" in body:
        bucket_type = body["type"]
        if bucket_type not in {"dynamic", "permanent", "feel"}:
            return JSONResponse({"error": "type must be dynamic, permanent, or feel"}, status_code=400)
        kwargs["type"] = bucket_type
    if "tags" in body:
        tags = body["tags"]
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            return JSONResponse({"error": "tags must be a list of strings"}, status_code=400)
        kwargs["tags"] = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
    if "chord" in body:
        if not isinstance(body["chord"], str):
            return JSONResponse({"error": "chord must be a string"}, status_code=400)
        kwargs["chord"] = body["chord"].strip()
    if "signal" in body:
        if not isinstance(body["signal"], str):
            return JSONResponse({"error": "signal must be a string"}, status_code=400)
        signal = body["signal"].strip()
        kwargs["signal"] = signal
        kwargs["signal_hints"] = _parse_signal_hints(signal)
    if body.get("preserve_last_active"):
        kwargs["_preserve_last_active"] = True

    if not kwargs:
        return JSONResponse({"error": "nothing to update"}, status_code=400)

    try:
        updated = await bucket_mgr.update(bucket_id, **kwargs)
        if not updated:
            return JSONResponse({"error": "bucket could not be updated"}, status_code=500)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}/delete", methods=["POST"])
async def api_bucket_delete(request):
    """Delete a bucket by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    try:
        file_path = bucket_mgr._find_bucket_file(bucket_id)
        if not file_path:
            return JSONResponse({"error": "not found"}, status_code=404)
        os.remove(file_path)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/catroom/read", methods=["GET"])
async def api_catroom_read(request):
    """Read recent Catroom notes. Catroom is intentionally outside Breath/weather."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    params = request.query_params
    try:
        limit = int(params.get("limit", "15") or "15")
    except ValueError:
        limit = 15
    try:
        topic = params.get("topic", "")
        if topic in ROOM_TOPIC_TO_CAT:
            records = _room_topic_records(topic, limit, author=params.get("author", ""))
        elif topic == "Catroom":
            records = _public_catroom_records(limit, author=params.get("author", ""))
        else:
            records = catroom_store.read(
                limit=limit,
                topic=topic,
                author=params.get("author", ""),
            )
        return JSONResponse({"ok": True, "records": records, "count": len(records)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


def _room_record_for_dashboard(record: dict, topic: str) -> dict:
    cat = str(record.get("cat") or "").strip().lower()
    return {
        "id": record.get("id"),
        "ts": record.get("ts"),
        "author": cat or topic.replace("Room", "").lower(),
        "content": record.get("content"),
        "topic": topic,
        "mood": record.get("kind"),
        "model": record.get("model"),
        "reply_to": None,
        "edited_ts": record.get("edited_ts"),
        "source": "room",
        "room_kind": record.get("kind"),
        "weight": record.get("weight"),
        "tags": record.get("tags") or [],
    }


def _legacy_room_topic_record(record: dict, topic: str) -> dict:
    item = dict(record)
    item["topic"] = topic
    item["source"] = "legacy_room_topic"
    return item


def _room_topic_records(topic: str, limit: int, author: str = "") -> list[dict]:
    cat = ROOM_TOPIC_TO_CAT.get(topic)
    catroom_records = catroom_store.read(limit=limit, topic=topic, author=author)
    if not cat:
        return catroom_records
    room_records = [_room_record_for_dashboard(r, topic) for r in room_store.read(cat=cat, limit=limit)]
    author_filter = str(author or "").strip().lower()
    if author_filter:
        room_records = [r for r in room_records if r.get("author") == author_filter]
    records = catroom_records + room_records
    records.sort(key=lambda r: str(r.get("ts") or ""))
    return records[-max(1, min(int(limit or 15), 100)):]


def _update_room_note_for_dashboard(note_id: str, body: dict) -> dict:
    topic = body.get("topic")
    updates = {}
    if "author" in body:
        updates["cat"] = body.get("author")
    if "content" in body:
        updates["content"] = body.get("content")
    if "topic" in body:
        updates["cat"] = ROOM_TOPIC_TO_CAT.get(str(topic or ""), body.get("author"))
    if "mood" in body:
        updates["kind"] = body.get("mood")
    if "model" in body:
        updates["model"] = body.get("model")
    record = room_store.update(note_id, **updates)
    room_topic = next((name for name, cat in ROOM_TOPIC_TO_CAT.items() if cat == record.get("cat")), "Catroom")
    return _room_record_for_dashboard(record, room_topic)


def _public_catroom_records(limit: int, author: str = "") -> list[dict]:
    records = catroom_store.read(limit=100, author=author)
    room_topics = set(ROOM_TOPIC_TO_CAT)
    records = [record for record in records if record.get("topic") not in room_topics]
    return records[-max(1, min(int(limit or 15), 100)):]


@mcp.custom_route("/api/catroom/hold", methods=["POST"])
async def api_catroom_hold(request):
    """Append a Catroom note."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    try:
        record = catroom_store.hold(
            author=body.get("author", ""),
            content=body.get("content", ""),
            topic=body.get("topic"),
            mood=body.get("mood"),
            model=body.get("model"),
            reply_to=body.get("reply_to"),
        )
        return JSONResponse({"ok": True, "record": record})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@mcp.custom_route("/api/room/read", methods=["GET"])
async def api_room_read(request):
    """Read old private room-wall notes for the Room dashboard."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    params = request.query_params
    cat = params.get("cat", "")
    topic = next((name for name, room_cat in ROOM_TOPIC_TO_CAT.items() if room_cat == cat), "")
    try:
        limit = int(params.get("limit", "15") or "15")
    except ValueError:
        limit = 15
    try:
        records = [_room_record_for_dashboard(r, topic or f"{cat.title()}Room") for r in room_store.read(cat=cat, limit=limit)]
        if topic:
            records.extend(_legacy_room_topic_record(r, topic) for r in catroom_store.read(limit=limit, topic=topic))
            records.sort(key=lambda r: str(r.get("ts") or ""))
            records = records[-max(1, min(int(limit or 15), 100)):]
        return JSONResponse({"ok": True, "records": records, "count": len(records), "source": "room"})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@mcp.custom_route("/api/room/hold", methods=["POST"])
async def api_room_hold(request):
    """Append a private room-wall note from the Room dashboard."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    topic = body.get("topic", "")
    cat = body.get("cat") or ROOM_TOPIC_TO_CAT.get(str(topic or ""))
    room_topic = next((name for name, room_cat in ROOM_TOPIC_TO_CAT.items() if room_cat == cat), str(topic or ""))
    try:
        record = room_store.hold(
            cat=cat,
            content=body.get("content", ""),
            kind=body.get("mood") or body.get("kind") or "note",
            model=body.get("model"),
        )
        return JSONResponse({"ok": True, "record": _room_record_for_dashboard(record, room_topic)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@mcp.custom_route("/api/room/copy_catroom", methods=["POST"])
async def api_room_copy_catroom(request):
    """Copy a public Catroom note into its author's private room wall."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    note_id = str(body.get("id", "") or "")
    try:
        note = catroom_store.get(note_id)
        if not note:
            return JSONResponse({"ok": False, "error": f"note not found: {note_id}"}, status_code=404)
        cat = body.get("cat") or note.get("author")
        room_topic = next((name for name, room_cat in ROOM_TOPIC_TO_CAT.items() if room_cat == cat), "")
        if not room_topic:
            raise ValueError("note author does not have a room")
        kind = body.get("kind") or note.get("mood") or note.get("topic") or "catroom_note"
        record = room_store.hold(
            cat=cat,
            content=note.get("content", ""),
            kind=kind,
            model=note.get("model"),
        )
        return JSONResponse({"ok": True, "record": _room_record_for_dashboard(record, room_topic)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@mcp.custom_route("/api/room/plate", methods=["GET"])
async def api_room_plate_read(request):
    """Read editable room breath copy."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    cat = request.query_params.get("cat", "")
    try:
        return JSONResponse({"ok": True, "cat": cat, "content": room_store.plate(cat)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@mcp.custom_route("/api/room/plate", methods=["POST"])
async def api_room_plate_update(request):
    """Update editable room breath copy."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    try:
        updated = room_store.update_plate(cat=body.get("cat", ""), content=body.get("content", ""))
        return JSONResponse({"ok": True, "plate": updated})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@mcp.custom_route("/api/catroom/reply", methods=["POST"])
async def api_catroom_reply(request):
    """Append a Catroom reply."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    try:
        record = catroom_store.reply(
            author=body.get("author", ""),
            reply_to=body.get("reply_to", ""),
            content=body.get("content", ""),
            topic=body.get("topic"),
            mood=body.get("mood"),
            model=body.get("model"),
        )
        return JSONResponse({"ok": True, "record": record})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@mcp.custom_route("/api/catroom/update", methods=["POST"])
async def api_catroom_update(request):
    """Edit a Catroom note in place."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    updates = {}
    for key in ("author", "content", "topic", "mood", "model"):
        if key in body:
            updates[key] = "" if body.get(key) is None else body.get(key)
    try:
        note_id = str(body.get("id", "") or "")
        if note_id.startswith("room_"):
            record = _update_room_note_for_dashboard(note_id, body)
        else:
            record = catroom_store.update(note_id, **updates)
        return JSONResponse({"ok": True, "record": record})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@mcp.custom_route("/api/catroom/delete", methods=["POST"])
async def api_catroom_delete(request):
    """Delete a Catroom note."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    try:
        note_id = str(body.get("id", "") or "")
        if note_id.startswith("room_"):
            deleted = room_store.delete(note_id)
        else:
            deleted = catroom_store.delete(note_id)
        return JSONResponse({"ok": True, "deleted": deleted})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request):
    """Search buckets by query."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    try:
        matches = await bucket_mgr.search(query, limit=10)
        result = []
        for b in matches:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "score": b.get("score", 0),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/network", methods=["GET"])
async def api_network(request):
    """Get embedding similarity network for visualization."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        nodes = []
        edges = []
        embeddings = {}

        for b in all_buckets:
            meta = b.get("metadata", {})
            bid = b["id"]
            nodes.append({
                "id": bid,
                "name": meta.get("name", bid),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "score": decay_engine.calculate_score(meta),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
            })
            if embedding_engine and embedding_engine.enabled:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb

        # Build edges from embeddings (similarity > 0.5)
        ids = list(embeddings.keys())
        for i, id_a in enumerate(ids):
            for id_b in ids[i+1:]:
                sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                if sim > 0.5:
                    edges.append({"source": id_a, "target": id_b, "similarity": round(sim, 3)})

        return JSONResponse({"nodes": nodes, "edges": edges})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/breath-debug", methods=["GET"])
async def api_breath_debug(request):
    """Debug endpoint: simulate breath scoring and return per-bucket breakdown."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    q_valence = request.query_params.get("valence")
    q_arousal = request.query_params.get("arousal")
    q_valence = float(q_valence) if q_valence else None
    q_arousal = float(q_arousal) if q_arousal else None

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        results = []
        w = {
            "topic": bucket_mgr.w_topic,
            "emotion": bucket_mgr.w_emotion,
            "time": bucket_mgr.w_time,
            "importance": bucket_mgr.w_importance,
        }
        w_sum = sum(w.values())

        for bucket in all_buckets:
            if _is_wander_only_bucket(bucket):
                continue
            meta = bucket.get("metadata", {})
            bid = bucket["id"]
            try:
                topic = bucket_mgr._calc_topic_score(query, bucket) if query else 0.0
                emotion = bucket_mgr._calc_emotion_score(q_valence, q_arousal, meta)
                time_s = bucket_mgr._calc_time_score(meta)
                imp = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                raw_total = (
                    topic * w["topic"]
                    + emotion * w["emotion"]
                    + time_s * w["time"]
                    + imp * w["importance"]
                )
                normalized = (raw_total / w_sum) * 100 if w_sum > 0 else 0
                resolved = meta.get("resolved", False)
                if resolved:
                    normalized *= 0.3

                results.append({
                    "id": bid,
                    "name": meta.get("name", bid),
                    "domain": meta.get("domain", []),
                    "type": meta.get("type", "dynamic"),
                    "resolved": resolved,
                    "pinned": meta.get("pinned", False),
                    "scores": {
                        "topic": round(topic, 4),
                        "emotion": round(emotion, 4),
                        "time": round(time_s, 4),
                        "importance": round(imp, 4),
                    },
                    "weights": w,
                    "raw_total": round(raw_total, 4),
                    "normalized": round(normalized, 2),
                    "passed_threshold": normalized >= bucket_mgr.fuzzy_threshold,
                })
            except Exception:
                continue

        results.sort(key=lambda x: x["normalized"], reverse=True)
        passed = [r for r in results if r["passed_threshold"]]
        return JSONResponse({
            "query": query,
            "valence": q_valence,
            "arousal": q_arousal,
            "weights": w,
            "threshold": bucket_mgr.fuzzy_threshold,
            "total_candidates": len(results),
            "passed_count": len(passed),
            "results": results[:50],  # top 50 for debug
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request):
    """Serve the dashboard HTML page."""
    from starlette.responses import HTMLResponse
    import os
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


@mcp.custom_route("/opening.html", methods=["GET"])
async def opening_html(request):
    """Serve the isolated opening sequence."""
    from starlette.responses import HTMLResponse
    import os
    opening_path = os.path.join(os.path.dirname(__file__), "opening.html")
    try:
        with open(opening_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>opening.html not found</h1>", status_code=404)


@mcp.custom_route("/api/config", methods=["GET"])
async def api_config_get(request):
    """Get current runtime config (safe fields only, API key masked)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    dehy = config.get("dehydration", {})
    emb = config.get("embedding", {})
    api_key = dehy.get("api_key", "")
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
    return JSONResponse({
        "dehydration": {
            "model": dehy.get("model", ""),
            "base_url": dehy.get("base_url", ""),
            "api_key_masked": masked_key,
            "max_tokens": dehy.get("max_tokens", 1024),
            "temperature": dehy.get("temperature", 0.1),
        },
        "embedding": {
            "enabled": emb.get("enabled", False),
            "model": emb.get("model", ""),
        },
        "merge_threshold": config.get("merge_threshold", 75),
        "transport": config.get("transport", "stdio"),
        "buckets_dir": config.get("buckets_dir", ""),
    })


@mcp.custom_route("/api/config", methods=["POST"])
async def api_config_update(request):
    """Hot-update runtime config. Optionally persist to config.yaml."""
    from starlette.responses import JSONResponse
    import yaml
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updated = []

    # --- Dehydration config ---
    if "dehydration" in body:
        d = body["dehydration"]
        dehy = config.setdefault("dehydration", {})
        for key in ("model", "base_url", "max_tokens", "temperature"):
            if key in d:
                dehy[key] = d[key]
                updated.append(f"dehydration.{key}")
        if "api_key" in d and d["api_key"]:
            dehy["api_key"] = d["api_key"]
            updated.append("dehydration.api_key")
        # Hot-reload dehydrator
        dehydrator.model = dehy.get("model", "deepseek-chat")
        dehydrator.base_url = dehy.get("base_url", "")
        dehydrator.api_key = dehy.get("api_key", "")
        if hasattr(dehydrator, "client") and dehydrator.api_key:
            from openai import AsyncOpenAI
            dehydrator.client = AsyncOpenAI(
                api_key=dehydrator.api_key,
                base_url=dehydrator.base_url,
            )

    # --- Embedding config ---
    if "embedding" in body:
        e = body["embedding"]
        emb = config.setdefault("embedding", {})
        if "enabled" in e:
            emb["enabled"] = bool(e["enabled"])
            embedding_engine.enabled = emb["enabled"]
            updated.append("embedding.enabled")
        if "model" in e:
            emb["model"] = e["model"]
            embedding_engine.model = emb["model"]
            updated.append("embedding.model")

    # --- Merge threshold ---
    if "merge_threshold" in body:
        config["merge_threshold"] = int(body["merge_threshold"])
        updated.append("merge_threshold")

    # --- Persist to config.yaml if requested ---
    if body.get("persist", False):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            save_config = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}

            if "dehydration" in body:
                sc_dehy = save_config.setdefault("dehydration", {})
                for key in ("model", "base_url", "max_tokens", "temperature"):
                    if key in body["dehydration"]:
                        sc_dehy[key] = body["dehydration"][key]
                # Never persist api_key to yaml (use env var)

            if "embedding" in body:
                sc_emb = save_config.setdefault("embedding", {})
                for key in ("enabled", "model"):
                    if key in body["embedding"]:
                        sc_emb[key] = body["embedding"][key]

            if "merge_threshold" in body:
                save_config["merge_threshold"] = int(body["merge_threshold"])

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_config, f, default_flow_style=False, allow_unicode=True)
            updated.append("persisted_to_yaml")
        except Exception as e:
            return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

    return JSONResponse({"updated": updated, "ok": True})


# =============================================================
# /api/marginalia — manual Shape Trace maintenance for breath.
# This is dashboard-only; the MCP marginalia tool is intentionally not exposed.
# =============================================================

@mcp.custom_route("/api/marginalia", methods=["GET"])
async def api_marginalia_get(request):
    """Read the manual Shape Trace text used by breath."""
    from starlette.responses import JSONResponse
    import json
    err = _require_auth(request)
    if err: return err
    content = ""
    ts = None
    try:
        if os.path.exists(MARGINALIA_PATH):
            with open(MARGINALIA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            content = data.get("letter", "") or ""
            ts = data.get("ts")
    except Exception as e:
        return JSONResponse({"error": f"failed to read marginalia: {e}"}, status_code=500)
    return JSONResponse({
        "content": content,
        "ts": ts,
        "max_chars": MARGINALIA_MAX_CHARS,
    })


@mcp.custom_route("/api/marginalia", methods=["POST"])
async def api_marginalia_set(request):
    """Overwrite the manual Shape Trace text used by breath."""
    from starlette.responses import JSONResponse
    import json
    import time as _time
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("content", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "content must be a string"}, status_code=400)
    content = raw[:MARGINALIA_MAX_CHARS]
    ts = _time.time()

    try:
        os.makedirs(os.path.dirname(MARGINALIA_PATH), exist_ok=True)
        with open(MARGINALIA_PATH, "w", encoding="utf-8") as f:
            json.dump({"letter": content, "ts": ts}, f, ensure_ascii=False)
    except Exception as e:
        return JSONResponse({"error": f"failed to write marginalia: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "content": content,
        "chars": len(content),
        "ts": ts,
        "max_chars": MARGINALIA_MAX_CHARS,
    })


# =============================================================
# /api/host-vault — read/write the host-side OMBRE_HOST_VAULT_DIR
# 用于在 Dashboard 设置 docker-compose 挂载的宿主机记忆桶目录。
# 写入项目根目录的 .env 文件，需 docker compose down/up 才能生效。
# =============================================================

def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_var(name: str) -> str:
    """Return current value of `name` from process env first, then .env file (best-effort)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _write_env_var(name: str, value: str) -> None:
    """
    Idempotent upsert of `NAME=value` in project .env. Creates the file if missing.
    Preserves other entries verbatim. Quotes values containing spaces.
    """
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


@mcp.custom_route("/api/host-vault", methods=["GET"])
async def api_host_vault_get(request):
    """Read the current OMBRE_HOST_VAULT_DIR (process env > project .env)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    value = _read_env_var("OMBRE_HOST_VAULT_DIR")
    return JSONResponse({
        "value": value,
        "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
        "env_file": _project_env_path(),
    })


@mcp.custom_route("/api/host-vault", methods=["POST"])
async def api_host_vault_set(request):
    """
    Persist OMBRE_HOST_VAULT_DIR to the project .env file.
    Body: {"value": "/path/to/vault"}  (empty string clears the entry)
    Note: container restart is required for docker-compose to pick up the new mount.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("value", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    value = raw.strip()

    # Reject characters that would break .env / shell parsing
    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

    try:
        _write_env_var("OMBRE_HOST_VAULT_DIR", value)
    except Exception as e:
        return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "value": value,
        "env_file": _project_env_path(),
        "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
    })


# =============================================================
# Import API — conversation history import
# 导入 API — 对话历史导入
# =============================================================

@mcp.custom_route("/api/import/upload", methods=["POST"])
async def api_import_upload(request):
    """Upload a conversation file and start import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    if import_engine.is_running:
        return JSONResponse({"error": "Import already running"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    filename = ""

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            raw_content = raw_bytes.decode("utf-8", errors="replace")
        else:
            body = await request.body()
            raw_content = body.decode("utf-8", errors="replace")
            # Try to get filename from query params
            filename = request.query_params.get("filename", "upload")

        if not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")

    except Exception as e:
        return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

    # Start import in background
    async def _run_import():
        try:
            await import_engine.start(raw_content, filename, preserve_raw, resume)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    asyncio.create_task(_run_import())

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_content.encode()),
    })


@mcp.custom_route("/api/import/status", methods=["GET"])
async def api_import_status(request):
    """Get current import progress."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse(import_engine.get_status())


@mcp.custom_route("/api/import/pause", methods=["POST"])
async def api_import_pause(request):
    """Pause the running import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if not import_engine.is_running:
        return JSONResponse({"error": "No import running"}, status_code=400)
    import_engine.pause()
    return JSONResponse({"status": "pause_requested"})


@mcp.custom_route("/api/import/patterns", methods=["GET"])
async def api_import_patterns(request):
    """Detect high-frequency patterns after import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        patterns = await import_engine.detect_patterns()
        return JSONResponse({"patterns": patterns})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/results", methods=["GET"])
async def api_import_results(request):
    """List recently imported/created buckets for review."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", "50"))
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # Sort by created time, newest first
        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        results = []
        for b in all_buckets[:limit]:
            results.append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "content": b["content"],
                "type": b["metadata"].get("type", ""),
                "domain": b["metadata"].get("domain", []),
                "tags": b["metadata"].get("tags", []),
                "chord": b["metadata"].get("chord", ""),
                "signal": b["metadata"].get("signal", ""),
                "signal_hints": b["metadata"].get("signal_hints", {}),
                "drive_tags": b["metadata"].get("drive_tags", {}),
                "importance": b["metadata"].get("importance", 5),
                "created": b["metadata"].get("created", ""),
            })
        return JSONResponse({"buckets": results, "total": len(all_buckets)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/review", methods=["POST"])
async def api_import_review(request):
    """Apply review decisions: mark buckets as important/noise/pinned."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)

    applied = 0
    errors = 0
    for d in decisions:
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            continue
        try:
            if action == "important":
                await bucket_mgr.update(bid, importance=9)
            elif action == "pin":
                await bucket_mgr.update(bid, pinned=True)
            elif action == "noise":
                await bucket_mgr.update(bid, resolved=True, importance=1)
            elif action == "delete":
                file_path = bucket_mgr._find_bucket_file(bid)
                if file_path:
                    os.remove(file_path)
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse({"applied": applied, "errors": errors})


# =============================================================
# /api/speech-event — 每轮话的短时残影
# Hook 热路径只提交本地初判；DP 复判在后台异步跑，结果下一轮生效。
# 复核语义沿用 Nocturne 的 认 / 不认 / 悬置，不让 rubric 写死成旧 Nox。
# =============================================================
@mcp.custom_route("/api/speech-event/submit", methods=["POST"])
async def api_speech_event_submit(request):
    from starlette.responses import JSONResponse
    return JSONResponse(
        {"ok": True, "retired": True, "reason": "dialogue_residue replaces speech_event"},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@mcp.custom_route("/api/speech-event/state", methods=["GET"])
async def api_speech_event_state(request):
    from starlette.responses import JSONResponse
    event = load_speech_event_state(config["buckets_dir"])
    if event:
        event = dict(event)
        event["recent"] = is_recent_speech_event(event)
    return JSONResponse(event or {}, headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/speech-event/review", methods=["POST"])
async def api_speech_event_review(request):
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400,
                           headers={"Access-Control-Allow-Origin": "*"})
    try:
        result = apply_speech_event_review(
            config["buckets_dir"],
            body.get("event_id", ""),
            body.get("mark", ""),
            body.get("note", ""),
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400,
                           headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500,
                           headers={"Access-Control-Allow-Origin": "*"})
    return JSONResponse(result, headers={"Access-Control-Allow-Origin": "*"})


# =============================================================
# /api/dialogue-residue — 2+2 当前对话残留
# companion 在 Stop 后拼出最近 2 条嘉嘉 + 2 条 Nox。若该窗口已经调用过
# Nocturne 工具，直接跳过，避免和 CLI/nocturne 自存事件重复喂入。
# =============================================================
@mcp.custom_route("/api/dialogue-residue/submit", methods=["POST"])
async def api_dialogue_residue_submit(request):
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400,
                           headers={"Access-Control-Allow-Origin": "*"})

    window_id = str(body.get("window_id") or "").strip()[:120]
    messages = normalize_dialogue_messages(body.get("messages") or [])
    thinking_signals = normalize_thinking_signals(body.get("thinking_signals") or [])
    nocturne_called = bool(body.get("nocturne_called"))
    if not window_id:
        window_id = str(body.get("id") or "").strip()[:120]
    if nocturne_called:
        skipped = normalize_dialogue_residue_event(
            {"status": "skipped_nocturne_call", "confidence": 0.0, "intensity": 0.0},
            messages=messages,
            window_id=window_id,
            thinking_signals=thinking_signals,
        )
        save_dialogue_residue_state(config["buckets_dir"], skipped, ledger_stage="skipped_nocturne_call")
        return JSONResponse({"ok": True, "skipped": True, "reason": "nocturne_called", "window_id": skipped["window_id"]},
                           headers={"Access-Control-Allow-Origin": "*"})
    if len(messages) < 4:
        return JSONResponse({"ok": False, "error": "need 2 user + 2 assistant messages", "count": len(messages)},
                           status_code=400, headers={"Access-Control-Allow-Origin": "*"})

    dp_available = dialogue_residue_available()
    if dp_available:
        asyncio.create_task(_refine_dialogue_residue_background(messages, window_id, thinking_signals))
    else:
        fallback = normalize_dialogue_residue_event(
            {"status": "dp_unavailable", "confidence": 0.0, "intensity": 0.0},
            messages=messages,
            window_id=window_id,
            thinking_signals=thinking_signals,
        )
        save_dialogue_residue_state(config["buckets_dir"], fallback, ledger_stage="dp_unavailable")

    return JSONResponse(
        {
            "ok": True,
            "dp_queued": dp_available,
            "window_id": window_id,
            "message_count": len(messages),
            "thinking_signal_count": len(thinking_signals),
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


@mcp.custom_route("/api/dialogue-residue/state", methods=["GET"])
async def api_dialogue_residue_state(request):
    from starlette.responses import JSONResponse
    return JSONResponse(load_dialogue_residue_state(config["buckets_dir"]) or {},
                       headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/desire/state", methods=["GET"])
async def api_desire_state(request):
    """只读：当前drive/intent/pa_na等快照，不tick。
    tick的节奏完全交给_desire_heartbeat_loop(1800s)——
    否则dashboard刷新/打开页面会偷偷多走一拍，tick_count/escape_streak/grief
    的计数节奏会被"谁在看"污染。"""
    from starlette.responses import JSONResponse
    state = _desire.state()
    try:
        import json as _j, os as _o
        mood_path = _bucket_path("current_mood.json")
        live = {}
        if _o.path.exists(mood_path):
            with open(mood_path) as _f:
                live = _j.load(_f)
        thoughts = sorted(
            state.get("thoughts", []),
            key=lambda t: float(t.get("born_at", 0) or 0),
            reverse=True,
        )
        state["thoughts"] = thoughts
        # get_daily_mood缓存不命中时会同步调DeepSeek(最长10s)，helper会扔进线程池跑，
        # 不然这一个请求会卡住整个事件循环，拖累同时打过来的所有其他请求。
        mood_entry = await _weather_mood_entry()
        # The dashboard needs the full WeatherResidue readout. effective_pa_na has
        # only final PA/NA, so using it here hides base/residue as 0.00.
        weather = _desire.weather_state()
        warmth = float(weather.get("effective_PA", state.get("pa_na", {}).get("PA", 0.5)))
        shadow = float(weather.get("effective_NA", state.get("pa_na", {}).get("NA", 0.2)))
        latest_thought = next(
            (t.get("text", "").strip() for t in thoughts if t.get("text", "").strip()),
            "",
        )
        intent = state.get("intent") or {}
        top_drive = intent.get("drive_key")
        if not top_drive:
            candidates = {k: v for k, v in state.get("drives", {}).items() if k != "fatigue"}
            top_drive = max(candidates, key=candidates.get, default="")
        undertow_value = float(state.get("drives", {}).get(top_drive, 0)) if top_drive else 0.0
        state["latest_thought"] = latest_thought
        state["mood_trace"] = latest_thought
        climate = str(weather.get("climate") or "Drift").strip()
        climate_display = weather.get("climate_display") or climate_transition_display(weather.get("atmosphere"))
        state["synthesized_mood_trace"] = mood_entry[0]
        state["mood_word"] = climate
        state["climate"] = climate
        state["climate_display"] = climate_display
        state["atmosphere_display"] = climate_display
        state["weather_residue"] = {
            "warmth": round(float(weather.get("warmth_residue", 0.0)), 3),
            "shadow": round(float(weather.get("shadow_residue", 0.0)), 3),
            "component_shadow": round(float(weather.get("component_shadow_residue", 0.0)), 3),
            "crystal_shadow": round(float(weather.get("crystal_shadow", 0.0)), 3),
            "shadow_crystal": weather.get("shadow_crystal"),
            "base_warmth": round(float(weather.get("base_PA", 0.0)), 3),
            "base_shadow": round(float(weather.get("base_NA", 0.0)), 3),
            "updated_at": weather.get("updated_at"),
            "active_chord": weather.get("active_chord", ""),
            "active_chord_source": weather.get("active_chord_source", ""),
            "active_chord_weight": weather.get("active_chord_weight", 0.0),
            "source_stack": weather.get("source_stack", []),
            "chord_chemistry": weather.get("chord_chemistry", {}),
            "chord_situation": weather.get("chord_situation", ""),
            "gravity_pool": weather.get("gravity_pool", ""),
            "gravity_line": weather.get("gravity_line", ""),
            "gravity": weather.get("gravity", ""),
            "atmosphere": weather.get("atmosphere", {}),
            "climate": climate,
            "climate_display": climate_display,
            "atmosphere_display": climate_display,
        }
        state["pulse_weather"] = {
            "undertow": top_drive,
            "undertow_value": round(undertow_value, 3),
            "warmth": round(warmth, 3),
            "shadow": round(shadow, 3),
            "current_chord": weather.get("current_chord", ""),
            "active_chord": weather.get("active_chord", ""),
            "active_chord_source": weather.get("active_chord_source", ""),
            "active_chord_weight": weather.get("active_chord_weight", 0.0),
            "source_stack": weather.get("source_stack", []),
            "chord_display": _weather_chord_display(weather),
            "climate": climate,
            "climate_display": climate_display,
            "atmosphere_display": climate_display,
            "atmosphere": weather.get("atmosphere", {}),
            "chord_chemistry": weather.get("chord_chemistry", {}),
            "chemistry_core": weather.get("chemistry_core", {}),
            "chemistry_route": weather.get("chemistry_route", {}),
            "chord_situation": weather.get("chord_situation", ""),
            "gravity_pool": weather.get("gravity_pool", ""),
            "derived_texture": weather.get("derived_texture", {}),
            "gravity_line": weather.get("gravity_line", ""),
            "gravity": weather.get("gravity", ""),
            "warmth_residue": round(float(weather.get("warmth_residue", 0.0)), 3),
            "shadow_residue": round(float(weather.get("shadow_residue", 0.0)), 3),
            "component_shadow_residue": round(float(weather.get("component_shadow_residue", 0.0)), 3),
            "crystal_shadow": round(float(weather.get("crystal_shadow", 0.0)), 3),
            "shadow_crystal": weather.get("shadow_crystal"),
            "base_warmth": round(float(weather.get("base_PA", 0.0)), 3),
            "base_shadow": round(float(weather.get("base_NA", 0.0)), 3),
            "longing": round(float(state.get("longing", 0)), 3),
            "nox_now": climate,
            "mood_trace": latest_thought,
            "synthesized_mood_trace": mood_entry[0],
        }
        state["now_playing"] = _current_now_playing()
        state["weather_panel"] = _weather_panel_from_state(state)
        dialogue_residue = load_dialogue_residue_state(config["buckets_dir"])
        if dialogue_residue:
            state["dialogue_residue"] = dialogue_residue
    except Exception:
        pass
    full_requested = str(request.query_params.get("full", "")).strip().lower() in {"1", "true", "yes"}
    full_allowed = os.environ.get("OMBRE_DESIRE_STATE_FULL", "").strip().lower() in {"1", "true", "yes", "on"}
    payload = state if full_requested and full_allowed else _compact_desire_state(state)
    return JSONResponse(payload,
                       headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/desire/intent", methods=["GET"])
async def api_desire_intent(request):
    """只读：当前intent + 关联念头text，不触发satisfy/refractory。供heartbeat_bridge轮询。"""
    from starlette.responses import JSONResponse
    intent = _desire.intent_with_thought()
    return JSONResponse({"intent": intent},
                       headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/desire/intent/hint", methods=["GET"])
async def api_desire_intent_hint(request):
    """Retired: Drive v2 uses live thoughts and latent notes, not preset intent pools."""
    from starlette.responses import JSONResponse
    return JSONResponse({"hint": None, "retired": True},
                       headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/heartbeat/latent-note", methods=["GET"])
async def api_heartbeat_latent_note(request):
    """Free Roam 用的潜意识便签：从 Nox 自己标过/悬着/未完成的旧线里抽一张短画面。"""
    from starlette.responses import JSONResponse
    raw_exclude = request.query_params.get("exclude", "")
    exclude_ids = {x.strip() for x in raw_exclude.split(",") if x.strip()}
    drive_key = request.query_params.get("drive_key", "")
    approved_note = _select_approved_latent_note(exclude_ids, drive_key=drive_key)
    if approved_note:
        try:
            approved_note["atmosphere_bias"] = _desire.apply_subcurrent_bias(
                approved_note.get("drive_tag") or drive_key,
                latent_weight=float(approved_note.get("score", 1.0) or 1.0),
                confidence=0.7,
            )
        except Exception as e:
            logger.warning(f"approved latent atmosphere bias failed: {e}")
        return JSONResponse(
            {"note": approved_note, "source": "approved_pool", "candidate_count": 1},
            headers={"Access-Control-Allow-Origin": "*"},
        )
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        marks_by_bucket = _load_all_marks()
        now = datetime.now()
        candidates: list[dict] = []
        for bucket in all_buckets:
            bucket_id = bucket.get("id", "")
            if not bucket_id or bucket_id in exclude_ids:
                continue
            mark_rows = marks_by_bucket.get(bucket_id, [])
            scored = _latent_candidate_score(bucket, mark_rows, now)
            if not scored:
                continue
            kind, score = scored
            if score <= 0:
                continue
            candidates.append(_latent_note_payload(bucket, mark_rows, kind, score))

        if not candidates:
            return JSONResponse({"note": None}, headers={"Access-Control-Allow-Origin": "*"})

        strong_candidates = [c for c in candidates if c.get("kind") != "old_memory"]
        pick_pool = strong_candidates or candidates
        weights = [max(0.01, c.get("score", 0.01)) for c in pick_pool]
        note = random.choices(pick_pool, weights=weights, k=1)[0]
        try:
            note["atmosphere_bias"] = _desire.apply_subcurrent_bias(
                note.get("drive_tag") or drive_key,
                latent_weight=float(note.get("score", 0.6) or 0.6),
                confidence=0.65,
            )
        except Exception as e:
            logger.warning(f"latent atmosphere bias failed: {e}")
        return JSONResponse(
            {
                "note": note,
                "candidate_count": len(pick_pool),
                "fallback_candidate_count": len(candidates) - len(strong_candidates),
            },
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception as e:
        logger.warning(f"heartbeat latent note failed: {e}")
        return JSONResponse({"note": None, "error": str(e)}, status_code=500,
                            headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/heartbeat/latent-note/ack", methods=["POST"])
async def api_heartbeat_latent_note_ack(request):
    """Heartbeat bridge 投递成功后确认消耗 approved 便签。"""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        body = {}
    note_id = body.get("note_id") or body.get("id") or body.get("bucket_id") or ""
    try:
        note = _ack_approved_latent_note(note_id)
        return JSONResponse(
            {"ok": True, "note": note},
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except KeyError:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404,
                            headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500,
                            headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/latent-notes", methods=["GET"])
async def api_latent_notes(request):
    """查看潜意识便签池。当前只用于草稿测试，不自动进入 heartbeat。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    status = (request.query_params.get("status") or "").strip()
    try:
        limit = int(request.query_params.get("limit") or 80)
    except ValueError:
        limit = 80
    data = _load_latent_notes()
    if _prune_expired_latent_notes(data):
        _save_latent_notes(data)
    notes = data.get("notes", [])
    if status:
        notes = [n for n in notes if n.get("status") == status]
    notes = sorted(notes, key=lambda n: str(n.get("created_at", "")), reverse=True)
    notes = sorted(notes, key=lambda n: not bool(n.get("pinned")))[:max(1, min(limit, 200))]
    return JSONResponse(
        {"version": data.get("version", LATENT_NOTE_POOL_VERSION), "count": len(notes), "notes": notes},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@mcp.custom_route("/api/latent-notes", methods=["POST"])
async def api_latent_notes_create(request):
    """手动添加一条潜意识便签草稿。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400,
                            headers={"Access-Control-Allow-Origin": "*"})
    dream_line = " ".join(str(body.get("dream_line") or body.get("line") or "").split())
    if not dream_line:
        return JSONResponse({"ok": False, "error": "dream_line required"}, status_code=400,
                            headers={"Access-Control-Allow-Origin": "*"})
    ts = _latent_note_ts()
    note_type = _normalize_latent_note_type(body.get("note_type"))
    note = {
        "id": "latent_manual_" + secrets.token_hex(8),
        "status": _normalize_latent_note_status(body.get("status"), "draft"),
        "pinned": bool(body.get("pinned", False)),
        "note_type": note_type,
        "drive_tag": _normalize_latent_drive_tag(body.get("drive_tag"), note_type),
        "source_bucket_id": "",
        "source_kind": _normalize_latent_source_kind(body.get("source_kind")),
        "source_title": str(body.get("source_title") or "手动便签").strip()[:80],
        "source_created": "",
        "source_fragment": str(body.get("source_fragment") or dream_line).strip()[:200],
        "dream_line": dream_line[:120],
        "model": "manual",
        "created_at": ts,
        "updated_at": ts,
    }
    data = _load_latent_notes()
    data["notes"] = [note] + data.get("notes", [])
    _touch_latent_note_data(data)
    _save_latent_notes(data)
    return JSONResponse({"ok": True, "note": note}, headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/latent-notes/{note_id}/update", methods=["POST"])
async def api_latent_notes_update(request):
    """编辑便签正文、类型或状态。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    note_id = request.path_params["note_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400,
                            headers={"Access-Control-Allow-Origin": "*"})
    data = _load_latent_notes()
    note = _find_latent_note(data, note_id)
    if not note:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404,
                            headers={"Access-Control-Allow-Origin": "*"})
    if "dream_line" in body or "line" in body:
        dream_line = " ".join(str(body.get("dream_line") or body.get("line") or "").split())
        if not dream_line:
            return JSONResponse({"ok": False, "error": "dream_line required"}, status_code=400,
                                headers={"Access-Control-Allow-Origin": "*"})
        note["dream_line"] = dream_line[:120]
    if "note_type" in body:
        note["note_type"] = _normalize_latent_note_type(body.get("note_type"))
        if "drive_tag" not in body:
            note["drive_tag"] = _normalize_latent_drive_tag(note.get("drive_tag"), note.get("note_type"))
    if "drive_tag" in body:
        note["drive_tag"] = _normalize_latent_drive_tag(body.get("drive_tag"), note.get("note_type"))
    if "status" in body:
        note["status"] = _normalize_latent_note_status(body.get("status"), note.get("status") or "draft")
    if "pinned" in body:
        note["pinned"] = bool(body.get("pinned"))
    note["updated_at"] = _latent_note_ts()
    _touch_latent_note_data(data)
    _save_latent_notes(data)
    return JSONResponse({"ok": True, "note": note}, headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/latent-notes/{note_id}/delete", methods=["POST"])
async def api_latent_notes_delete(request):
    """软删除一条潜意识便签。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    note_id = request.path_params["note_id"]
    data = _load_latent_notes()
    note = _find_latent_note(data, note_id)
    if not note:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404,
                            headers={"Access-Control-Allow-Origin": "*"})
    ts = _latent_note_ts()
    note["status"] = "deleted"
    note["deleted_at"] = ts
    note["updated_at"] = ts
    _touch_latent_note_data(data)
    _save_latent_notes(data)
    return JSONResponse({"ok": True, "note": note}, headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/latent-notes/generate", methods=["POST"])
async def api_latent_notes_generate(request):
    """批量生成潜意识便签草稿。DP 慢路径，只进 draft 池，不直接喂 heartbeat。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        count = int(body.get("count") or request.query_params.get("count") or 10)
    except (TypeError, ValueError):
        count = 10
    count = max(1, min(count, 50))
    try:
        result = await _generate_latent_note_drafts(count=count)
        generated = result.get("generated", [])
        data = _load_latent_notes()
        existing_ids = {n.get("id") for n in data.get("notes", [])}
        fresh = [n for n in generated if n.get("id") not in existing_ids]
        if fresh:
            data["notes"] = fresh + data.get("notes", [])
            data["version"] = LATENT_NOTE_POOL_VERSION
            data["updated_at"] = now_iso()
            _save_latent_notes(data)
        return JSONResponse(
            {
                "ok": True,
                "requested": count,
                "generated_count": len(generated),
                "saved_count": len(fresh),
                "source_count": result.get("source_count", 0),
                "inward_source_count": result.get("inward_source_count", 0),
                "outward_source_count": result.get("outward_source_count", 0),
                "inward_target": result.get("inward_target", 0),
                "outward_target": result.get("outward_target", 0),
                "model": result.get("model", ""),
                "notes": fresh,
            },
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception as e:
        logger.warning(f"latent note generation failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500,
                            headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/desire/intent/ack", methods=["POST"])
async def api_desire_intent_ack(request):
    """本地投递成功后调用：执行satisfy并设置refractory，让drive回落。
    POST JSON: {"drive_key": "attachment"}"""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        body = {}
    drive_key = body.get("drive_key", "")
    if not drive_key:
        return JSONResponse({"error": "drive_key required"}, status_code=400)
    try:
        result = _desire.satisfy(drive_key)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"acked": drive_key, "result": result},
                       headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/desire/intent/pass", methods=["POST"])
async def api_desire_intent_pass(request):
    """轻轻放过当前intent：不改Drive，只降短期hook/intent优先级。"""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        body = {}
    drive_key = body.get("drive_key", "")
    if not drive_key:
        return JSONResponse({"error": "drive_key required"}, status_code=400)
    try:
        result = _desire.pass_intent(drive_key, reason=body.get("reason") or None)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"passed": drive_key, "result": result},
                       headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/desire/ping", methods=["POST"])
async def api_desire_ping(request):
    """嘉嘉发消息时调用，重置longing计时器；可携带本地关键词天气轻推。"""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        result = _desire.mark_user_signal()
        weather = None
        def _safe_delta(key: str) -> float:
            try:
                return max(0.0, float(body.get(key, 0.0) or 0.0))
            except (TypeError, ValueError):
                return 0.0
        warmth_delta = _safe_delta("warmth_delta")
        shadow_delta = _safe_delta("shadow_delta")
        soothe = bool(body.get("soothe", False))
        if warmth_delta > 0 or shadow_delta > 0 or soothe:
            weather = _desire.apply_weather_delta(
                warmth_delta=warmth_delta,
                shadow_delta=shadow_delta,
                source=body.get("source", "keyword"),
                soothe=soothe,
            )
        return JSONResponse({"ok": True, **result, "weather_residue": weather},
                           headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500,
                           headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/desire/thought/{tid}/update", methods=["POST"])
async def api_desire_thought_update(request):
    """Dashboard edit: update one thought's text/drive/strength."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    tid = request.path_params.get("tid", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    text = body.get("text") if "text" in body else None
    drive = body.get("drive") if "drive" in body else None
    strength = body.get("strength") if "strength" in body else None
    if text is not None and not isinstance(text, str):
        return JSONResponse({"error": "text must be a string"}, status_code=400)
    if drive is not None:
        drive = normalize_drive_key(drive)
        if drive not in DRIVE_KEYS:
            return JSONResponse({"error": "invalid drive"}, status_code=400)
    if strength is not None:
        try:
            strength = max(0.0, min(1.0, float(strength)))
        except (TypeError, ValueError):
            return JSONResponse({"error": "strength must be a number"}, status_code=400)

    try:
        result = _desire.update_thought(tid, text=text, drive=drive, strength=strength)
        if not result.get("ok"):
            return JSONResponse({"error": "thought not found or unchanged"}, status_code=404)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/desire/thought/{tid}/delete", methods=["POST"])
async def api_desire_thought_delete(request):
    """Dashboard edit: remove one thought from Thought Pool."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    tid = request.path_params.get("tid", "")
    try:
        result = _desire.delete_thought(tid)
        if not result.get("ok"):
            return JSONResponse({"error": "thought not found"}, status_code=404)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/desire/feed", methods=["POST"])
async def api_desire_feed(request):
    """
    接收drive_event_v2或旧analyze_feel结果，写进念头池/Drive Event账本。
    v2: primary_drive + intensity + confidence + agency + brain + thoughts。
    legacy: drives/brain_signals会被折成一次drive_event_v2，不再三路重复pulse。
    """
    from starlette.responses import JSONResponse
    import json as _json
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    thoughts = body.get("thoughts", [])

    # --- 批量涌入节流 ---
    # trigger.js依赖claude.ai session活跃才跑，可能积压几天的feel一次性涌进来。
    # 一批塞太多flit会在接下来几次tick里集中冲过FLIT_UPGRADE_THRESHOLD同时升级成
    # fixation，造成drive脉冲尖峰——这是工程节奏问题，跟"久没见想得更浓"的基线
    # 漂移是两件事，不该混在一起。
    # 超过阈值时：strength最高的几条按原值入池，其余按名次递减打折——
    # 打折幅度不同→之后每次tick_thoughts衰减后到达升级阈值的时机自然错开，
    # 而不是同一拍集中升级。
    FEED_BATCH_THRESHOLD = 4   # 超过这个数量才节流
    FEED_KEEP_FULL = 3         # 保留几条按原strength入池
    FEED_DISCOUNT_STEP = 0.08  # 其余每条额外递减的折扣
    FEED_STRENGTH_FLOOR = 0.22 # 打折下限——flit 12h半衰期下，低于这个不到24h就fade没了

    def _add_feed_thoughts(items: list, source: str = "cli") -> int:
        if not isinstance(items, list):
            return 0
        if len(items) > FEED_BATCH_THRESHOLD:
            items = sorted(items, key=lambda t: float(t.get("strength", 0.45)), reverse=True)
        added_count = 0
        for i, t in enumerate(items):
            if not isinstance(t, dict):
                continue
            text = str(t.get("text", "")).strip()
            drive = normalize_drive_key(t.get("drive"), "unsourced")
            try:
                strength = float(t.get("strength", 0.45))
            except (TypeError, ValueError):
                strength = 0.45
            if len(items) > FEED_BATCH_THRESHOLD and i >= FEED_KEEP_FULL:
                rank = i - FEED_KEEP_FULL + 1
                strength = max(FEED_STRENGTH_FLOOR, strength * (1 - FEED_DISCOUNT_STEP * rank))
            if text:
                try:
                    thought_source = str(t.get("source") or source or "cli").strip()[:80]
                    source_bucket = str(t.get("source_bucket") or body.get("source_bucket") or "").strip()[:120]
                    source_type = str(t.get("source_type") or body.get("source_type") or "").strip()[:40]
                    source_created = str(t.get("source_created") or body.get("source_created") or "").strip()[:80]
                    _desire.add_thought(
                        text, drive, strength=strength, source=thought_source,
                        source_bucket=source_bucket, source_type=source_type,
                        source_created=source_created,
                    )
                    _desire.store.add_echo(text, drive)
                    if (t.get("chord") or "").strip():
                        _desire.apply_chord_echo(t.get("chord", "").strip(), source="thought")
                    added_count += 1
                except Exception as e:
                    logger.warning(f"desire/feed add_thought failed: {e}")
        return added_count

    schema_version = str(body.get("schema_version") or "")
    is_v2 = schema_version == DRIVE_EVENT_SCHEMA or bool(body.get("primary_drive"))
    event_result = None
    event_body = None
    if is_v2:
        event_body = body
    else:
        drives = body.get("drives", {})
        brain_signals = body.get("brain_signals", {})
        if isinstance(drives, dict) and drives or isinstance(brain_signals, dict) and brain_signals:
            event_body = _legacy_brain_to_event(brain_signals, drives)

    if event_body and event_body.get("primary_drive"):
        try:
            event_result = _desire.apply_drive_event(event_body)
        except Exception as e:
            logger.warning(f"desire/feed drive_event failed: {e}")
            event_result = {"ok": False, "error": str(e)}

    feed_source = str(body.get("source") or (event_body or {}).get("source") or "cli").strip()[:80] or "cli"
    add_thoughts = (
        not (event_result and event_result.get("suppressed"))
        or feed_source == "analyze_nocturne_entry"
    )
    added = _add_feed_thoughts(thoughts, source=feed_source) if add_thoughts else 0
    if event_result and event_result.get("suppressed"):
        logger.info(f"desire/feed suppressed event: {event_result.get('reason')}")

    try:
        import json as _bj, os as _bo
        mood_path = _bucket_path("current_mood.json")
        mood_data = {}
        if _bo.path.exists(mood_path):
            with open(mood_path) as _f:
                mood_data = _bj.load(_f)
        if event_body:
            mood_data["drive_event"] = {
                "schema_version": DRIVE_EVENT_SCHEMA,
                "primary_drive": normalize_drive_key(event_body.get("primary_drive"), ""),
                "event_label": event_body.get("event_label", ""),
                "brain": event_body.get("brain", {}),
                "evidence": event_body.get("evidence", []),
                "result": event_result or {},
            }
        if body.get("brain_signals"):
            mood_data["legacy_brain_signals"] = body.get("brain_signals")
        with open(mood_path, "w") as _f:
            _bj.dump(mood_data, _f)
    except Exception as e:
        logger.warning(f"desire/feed mood write failed: {e}")

    source = ""
    if isinstance(event_body, dict):
        brain = event_body.get("brain") if isinstance(event_body.get("brain"), dict) else {}
        source = str(event_body.get("source") or brain.get("source") or "")
    if body.get("mark_user_signal") or source in {"user_message", "speech_event"} or body.get("brain_signals"):
        try:
            _last_signal_ts[0] = time.time()
            _desire.mark_user_signal(_last_signal_ts[0])
        except Exception as e:
            logger.warning(f"desire/feed mark_user_signal failed: {e}")

    logger.info(f"desire/feed: +{added} thoughts, event={event_result}")

    return JSONResponse({
        "ok": True,
        "thoughts_added": added,
        "event": event_result,
    }, headers={"Access-Control-Allow-Origin": "*"})


# =============================================================
# /api/soma — Soma Trace上报/读取
# Soma Trace是nox-companion本地hook算的(读mini_cat_state.json/
# big_cat_state.json这些本地文件)，后端本来不知道这东西存在。
# 本地hook每次算完，主动POST一份上来；dashboard用GET读最新的。
# 1小时没人上报就当过期，不强行维持一个早就不新鲜的状态。
# =============================================================
_SOMA_STATE_PATH = _bucket_path("soma_state.json")
_SOMA_STALE_SECONDS = 3600


@mcp.custom_route("/api/soma/report", methods=["POST"])
async def api_soma_report(request):
    from starlette.responses import JSONResponse
    import json
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    source = (body.get("source") or "").strip()[:40]
    if body.get("clear"):
        try:
            os.makedirs(os.path.dirname(_SOMA_STATE_PATH), exist_ok=True)
            with open(_SOMA_STATE_PATH, "w") as f:
                json.dump({"line": None, "chord": None, "source": source or "clear", "updated_at": time.time()}, f)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "cleared": True}, headers={"Access-Control-Allow-Origin": "*"})

    line = (body.get("line") or "").strip()
    chord = (body.get("chord") or "").strip()
    if not line:
        return JSONResponse({"error": "line required"}, status_code=400)
    try:
        os.makedirs(os.path.dirname(_SOMA_STATE_PATH), exist_ok=True)
        with open(_SOMA_STATE_PATH, "w") as f:
            json.dump({"line": line, "chord": chord, "source": source, "updated_at": time.time()}, f)
        if chord:
            _desire.apply_chord_echo(chord, source="soma")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/soma/state", methods=["GET"])
async def api_soma_state(request):
    from starlette.responses import JSONResponse
    import json
    try:
        with open(_SOMA_STATE_PATH) as f:
            data = json.load(f)
        if time.time() - data.get("updated_at", 0) > _SOMA_STALE_SECONDS:
            return JSONResponse({"line": None, "chord": None, "source": None},
                               headers={"Access-Control-Allow-Origin": "*"})
        return JSONResponse(data, headers={"Access-Control-Allow-Origin": "*"})
    except Exception:
        return JSONResponse({"line": None, "chord": None, "source": None},
                           headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/analyzer/entries", methods=["GET"])
async def api_analyzer_entries(request):
    """
    Analyzer-only read view for new Nocturne entries.
    Defaults to 2026-06-25 00:00 Asia/Shanghai (2026-06-24T16:00:00Z).
    """
    from starlette.responses import JSONResponse
    try:
        since = _parse_analyzer_since(request.query_params.get("since"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400,
                           headers={"Access-Control-Allow-Origin": "*"})

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        marks_by_bucket = _load_all_marks()
        entries = []
        for b in all_buckets:
            bid = str(b.get("id") or "").strip()
            if not bid:
                continue
            mark_rows = marks_by_bucket.get(bid, [])
            if _is_private_bucket(b, mark_rows):
                continue
            created_dt = _bucket_created_utc(b)
            if not created_dt or created_dt < since:
                continue
            entry_type = _analyzer_entry_type(b, mark_rows)
            if not entry_type:
                continue
            meta = b.get("metadata", {})
            entries.append({
                "id": bid,
                "type": entry_type,
                "created": meta.get("created", ""),
                "content_preview": _analyzer_preview(b.get("content", "")),
                "chord": meta.get("chord", ""),
                "tags": meta.get("tags", []),
                "domain": meta.get("domain", []),
                "drive_tags": meta.get("drive_tags", {}),
                "signal_hints": meta.get("signal_hints", {}),
                "source": "analyze_nocturne_entry",
                "source_bucket": bid,
                "source_type": entry_type,
                "source_created": meta.get("created", ""),
                "_created_sort": created_dt.isoformat(),
            })
        entries.sort(key=lambda x: x["_created_sort"], reverse=True)
        for entry in entries:
            entry.pop("_created_sort", None)
        return JSONResponse(entries, headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500,
                           headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/analyzer/dp-memory", methods=["POST"])
async def api_analyzer_dp_memory(request):
    """
    DP memory analyzer line. Keeps the old CLI analyzer dormant and accepts
    the same entry shape from /api/analyzer/entries plus the old CLI preference.
    """
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400,
                           headers={"Access-Control-Allow-Origin": "*"})

    entry = normalize_memory_entry(body.get("entry") if isinstance(body.get("entry"), dict) else body)
    preference = str(body.get("preference") or "").strip()
    post_feed = bool(body.get("post_feed", False))
    if not entry.get("content_preview"):
        return JSONResponse({"error": "entry.content_preview required"}, status_code=400,
                           headers={"Access-Control-Allow-Origin": "*"})
    if not memory_residue_available():
        return JSONResponse({"error": "dp_memory unavailable"}, status_code=503,
                           headers={"Access-Control-Allow-Origin": "*"})

    try:
        event = await classify_memory_residue_dp(
            entry,
            preference=preference,
            state_context=_dialogue_residue_context_snapshot(),
        )
        feed_result = None
        if post_feed:
            if event.get("primary_drive"):
                event_result = _desire.apply_drive_event(event)
            else:
                event_result = {"ok": False, "reason": "no_primary_drive"}
            added = 0
            for thought in event.get("thoughts", []) if isinstance(event.get("thoughts"), list) else []:
                text = str(thought.get("text") or "").strip()
                if not text:
                    continue
                _desire.add_thought(
                    text,
                    normalize_drive_key(thought.get("drive"), event.get("primary_drive") or "reflection"),
                    strength=float(thought.get("strength", 0.45) or 0.45),
                    source="dp_memory",
                    source_bucket=entry.get("id", ""),
                    source_type=entry.get("type", ""),
                    source_created=entry.get("created", ""),
                )
                _desire.store.add_echo(text, normalize_drive_key(thought.get("drive"), "reflection"))
                if str(thought.get("chord") or "").strip():
                    _desire.apply_chord_echo(str(thought.get("chord") or "").strip(), source="thought")
                added += 1
            feed_result = {"event": event_result, "thoughts_added": added}
        return JSONResponse({"ok": True, "event": event, "feed": feed_result},
                           headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        logger.warning(f"dp_memory analyzer failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500,
                           headers={"Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/api/feels", methods=["GET"])
async def api_feels_public(request):
    """公开接口：返回feel列表，供本地trigger按时间/checkpoint限流分析。无需auth。"""
    from starlette.responses import JSONResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        feels = [
            {
                "id": b["id"],
                "content_preview": b["content"][:300],
                "created": b["metadata"].get("created", ""),
                "chord": b["metadata"].get("chord", ""),
                "drive_tags": b["metadata"].get("drive_tags", {}),
                "digested": bool(b["metadata"].get("digested", False)),
                "resolved": bool(b["metadata"].get("resolved", False)),
            }
            for b in all_buckets
            if b["metadata"].get("type") == "feel"
        ]
        feels.sort(key=lambda x: x["created"], reverse=True)
        return JSONResponse(feels, headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500,
                           headers={"Access-Control-Allow-Origin": "*"})
    
# =============================================================
# /api/status — system status for Dashboard settings tab
# /api/status — Dashboard 设置页用系统状态
# =============================================================
@mcp.custom_route("/api/status", methods=["GET"])
async def api_system_status(request):
    """Return detailed system status for the settings panel."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "embedding_enabled": embedding_engine.enabled,
            "buckets": {
                "permanent": stats.get("permanent_count", 0),
                "dynamic": stats.get("dynamic_count", 0),
                "archive": stats.get("archive_count", 0),
                "total": stats.get("permanent_count", 0) + stats.get("dynamic_count", 0),
            },
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            "version": "1.3.0",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop():
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(f"http://localhost:{OMBRE_PORT}/health", timeout=5)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        async def _desire_heartbeat_loop():
            await asyncio.sleep(60)  # 等服务器完全启动

            while True:
                try:
                    now = time.time()
                    # has_signal：上一个tick周期（1800s）内是否有嘉嘉输入信号
                    has_signal = (now - _last_signal_ts[0]) < 1800
                    _desire.tick(idle_seconds=1800, has_signal=has_signal)

                    # 节律状态日志
                    try:
                        rhythm = _desire.rhythm_state()
                        logger.info(f"Rhythm: {rhythm['label']} (val={rhythm['value']})")
                        grief = _desire.grief_state()
                        if grief["layer"] != "none":
                            logger.info(f"Grief layer: {grief['layer']} (protest_ticks={grief['protest_ticks']})")
                    except Exception as e:
                        logger.warning(f"Rhythm/grief state log failed: {e}")

                    # echo机制已关闭——念头池只靠新feel分析和手动pulse补充

                    intent = _desire.intent()
                    if intent:
                        asyncio.create_task(_execute_intent(intent))
                    logger.info("Desire heartbeat tick")
                except Exception as e:
                    logger.warning(f"Desire heartbeat failed: {e}")
                await asyncio.sleep(1800)

        def _start_desire_heartbeat():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_desire_heartbeat_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        hb = threading.Thread(target=_start_desire_heartbeat, daemon=True)
        hb.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()

        async def _decay_background_loop():
            try:
                await decay_engine.ensure_started()
                while True:
                    await asyncio.sleep(3600)
            except Exception as e:
                logger.warning(f"Decay engine startup failed / 衰减引擎启动失败: {e}")

        def _start_decay_background():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_decay_background_loop())

        decay_thread = threading.Thread(target=_start_decay_background, daemon=True)
        decay_thread.start()

        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        mcp.run(transport=transport)
