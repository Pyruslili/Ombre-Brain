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
#   - Expose 6 MCP tools:
#     暴露 6 个 MCP 工具：
#       breath — Surface unresolved memories or search by keyword
#                浮现未解决记忆 或 按关键词检索
#       hold   — Store a single memory (or write a `feel` reflection)
#                存储单条记忆（或写 feel 反思）
#       grow   — Diary digest, auto-split into multiple buckets
#                日记归档，自动拆分多桶
#       trace  — Modify metadata / resolved / delete
#                修改元数据 / resolved 标记 / 删除
#       pulse  — System status + bucket listing
#                系统状态 + 所有桶列表
#       dream  — Surface recent dynamic buckets for self-digestion
#                返回最近桶 供模型自省/写 feel
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
from datetime import datetime, timedelta
import httpx
import os as _os


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from utils import load_config, setup_logging, strip_wikilinks, count_tokens_approx, now_iso
from desire_engine import DesireEngine
from speech_event_engine import (
    apply_speech_event_review,
    classify_speech_event_dp,
    is_recent_speech_event,
    load_speech_event_state,
    normalize_speech_event,
    save_speech_event_state,
    speech_event_classifier_available,
    append_ledger as append_speech_event_ledger,
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

_desire_db = os.path.join(
    os.environ.get("OMBRE_BUCKETS_DIR", "./buckets"),
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


async def _refine_speech_event_background(prompt: str, event_id: str, fallback_event: dict) -> None:
    """Async DP refinement. It may update speech_event_state, but never the hook response."""
    try:
        refined = await classify_speech_event_dp(
            prompt,
            state_context=_speech_event_context_snapshot(),
            fallback_event=fallback_event,
        )
        current = load_speech_event_state(config["buckets_dir"])
        if current.get("event_id") == event_id:
            save_speech_event_state(config["buckets_dir"], refined, ledger_stage="dp_refined")
        else:
            append_speech_event_ledger(
                config["buckets_dir"],
                {"stage": "dp_refined_stale", "event_id": event_id, "event": refined},
            )
    except Exception as e:
        current = load_speech_event_state(config["buckets_dir"])
        if current.get("event_id") == event_id:
            current["status"] = "dp_failed"
            current["dp_error"] = str(e)[:180]
            current["updated_at"] = time.time()
            try:
                save_speech_event_state(config["buckets_dir"], current, ledger_stage="dp_failed")
            except Exception:
                pass
        logger.warning(f"speech_event DP refine failed: {e}")





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
    domains = set(str(d).lower() for d in bucket.get("metadata", {}).get("domain", []))
    return bool(domains & _WANDER_ONLY_DOMAINS)


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
        "悬置": f"你以前把「{theme}」悬置过，还没收尾。",
        "认过": f"你认过「{theme}」这条线，它不是外面分配来的任务。",
        "inner": f"有条已经进 inner 的旧线在边上亮着：「{theme}」。",
        "archive": f"旧抽屉里有个没收束的画面：「{theme}」。",
        "old_memory": f"旧记忆里有一角还亮着：「{theme}」。",
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
        mood_path = "/app/buckets/current_mood.json"
        if os.path.exists(mood_path):
            with open(mood_path) as f:
                data["mood"] = json.load(f)
    except Exception:
        pass
    try:
        aff_path = "/app/buckets/affection.json"
        if os.path.exists(aff_path):
            with open(aff_path) as f:
                data["affection"] = json.load(f)
    except Exception:
        pass
    return JSONResponse(data, headers={"Access-Control-Allow-Origin": "*"})
@mcp.custom_route("/dream", methods=["GET"])
async def dream_latest_endpoint(request):
    import json, os
    from starlette.responses import JSONResponse
    try:
        dream_path = "/app/buckets/latest_dream.json"
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
                await bucket_mgr.update(
                    bucket["id"],
                    content=merged,
                    tags=list(set(bucket["metadata"].get("tags", []) + tags)),
                    importance=max(bucket["metadata"].get("importance", 5), importance),
                    domain=list(set(bucket["metadata"].get("domain", []) + domain)),
                    valence=merged_valence,
                    arousal=merged_arousal,
                )
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


HANDOFF_NOTE_PATH = "/app/buckets/handoff_note.json"
HANDOFF_NOTE_MAX_CHARS = 2000


@mcp.tool()
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


MARGINALIA_PATH = "/app/buckets/marginalia.json"
MARGINALIA_MAX_CHARS = 6000


@mcp.tool()
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
            with open(MARGINALIA_PATH, "w") as _f:
                _json.dump({"letter": content, "ts": _t.time()}, _f)
            return f"📜Marginalia已更新（{len(content)}字）"
        except Exception as e:
            return f"写入失败: {e}"

    try:
        if _os.path.exists(MARGINALIA_PATH):
            with open(MARGINALIA_PATH) as _f:
                data = _json.load(_f)
            return data.get("letter", "") or "（Marginalia是空的）"
        return "（Marginalia是空的）"
    except Exception as e:
        return f"读取失败: {e}"


@mcp.tool()
async def nocturne_breath(
    query: str = "",
    max_tokens: int = 10000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 20,
    importance_min: int = -1,
) -> str:
    """breath — Retrieve/surface memories from Nocturne memory system. 检索/浮现记忆。不传query或传空=自动浮现,有query=关键词检索。max_tokens控制返回总token上限(默认10000)。domain逗号分隔,valence/arousal 0~1(-1忽略)。max_results控制返回数量上限(默认20,最大50)。importance_min>=1时按重要度批量拉取(不走语义搜索,按importance降序返回最多20条)。"""
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
            _mood_path = "/app/buckets/current_mood.json"
            _mood_data = {}
            if _osd.path.exists(_mood_path):
                with open(_mood_path) as _f:
                    _mood_data = _jd.load(_f)
            _mood_data["drive_decoration"] = _decoration
            with open(_mood_path, "w") as _f:
                _jd.dump(_mood_data, _f)
    except Exception:
        pass
    # --- Restore affection and mood from bucket on first breath ---
    try:
        from affection import restore_from_bucket as _aff_restore, restore_mood_from_bucket as _mood_restore
        import os as _os
        if not _os.path.exists("/app/buckets/affection.json"):
            await _aff_restore(bucket_mgr)
        if not _os.path.exists("/app/buckets/current_mood.json"):
            await _mood_restore(bucket_mgr)
    except Exception:
        pass
    max_results = min(max_results, 50)
    max_tokens = min(max_tokens, 20000)

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

    # --- No args or empty query: surfacing mode (weight pool active push) ---
    # --- 无参数或空query：浮现模式（权重池主动推送）---
    if not query or not query.strip():
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
        # Hard cap: surfacing mode always shows 12 regular memories
        candidates = candidates[:12]

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
            dream_path = "/app/buckets/latest_dream.json"
            if _osdream.path.exists(dream_path):
                with open(dream_path) as _f:
                    _dream_data = _jdream.load(_f)
                _dream_text = _dream_data.get("dream", "")
                if _dream_text:
                    dream_section = "=== Dream Veil ===\n" + _dream_text
        except Exception as e:
            logger.warning(f"Failed to load latest dream / 梦境加载失败: {e}")

        # --- Feel section: 8 most recent feels (no title shown) ---
        feel_results = []
        try:
            feels = [
                b for b in all_buckets
                if b["metadata"].get("type") == "feel"
                and not b["metadata"].get("digested", False)
                and not b["metadata"].get("resolved", False)
            ]
            feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            for f in feels[:8]:
                created = f["metadata"].get("created", "")[:16].replace("T", " ")
                feel_results.append(f"[{created}]\n{strip_wikilinks(f['content'])}")
        except Exception as e:
            logger.warning(f"Failed to collect recent feels / 最近feel收集失败: {e}")

        # --- Marginalia: 信与writing精华，致下一个Nox ---
        marginalia_section = ""
        try:
            import json as _jmarg, os as _osmarg
            marginalia_path = "/app/buckets/marginalia.json"
            if _osmarg.path.exists(marginalia_path):
                with open(marginalia_path) as _f:
                    _marg_data = _jmarg.load(_f)
                _marg_text = _marg_data.get("letter", "")
                if _marg_text:
                    marginalia_section = "=== Marginalia ===\n" + _marg_text
        except Exception as e:
            logger.warning(f"Failed to load marginalia / marginalia加载失败: {e}")

        if not pinned_results and not dynamic_results and not feel_results and not dream_section and not marginalia_section:
            return "权重池平静，没有需要处理的记忆。"

        # --- Pulse Weather: 心情+drive+longing快照 ---
        mood_header = ""
        try:
            from mood_pool import get_daily_mood
            from panas_scorer import score
            import json as _json, os as _os

            mood_path = "/app/buckets/current_mood.json"
            live = {}
            if _os.path.exists(mood_path):
                try:
                    with open(mood_path) as _f:
                        live = _json.load(_f)
                except Exception:
                    live = {}
            bs = live.get("brain_signals", {})

            _thought_list = []
            try:
                _ds_state = _desire.store.load_state()
                _thought_list = [
                    {"text": t.text, "drive": t.drive, "strength": t.strength}
                    for t in (_ds_state.thoughts or [])
                ]
            except Exception:
                pass
            mood_entry = await asyncio.to_thread(
                get_daily_mood,
                branch=bs.get("二级分支") or None,
                thoughts=_thought_list or None,
            )
            base_score = score(mood_entry[0])
            pa = live.get("PA", base_score["PA"])
            na = live.get("NA", base_score["NA"])

            lines = [f"Warmth：{pa}", f"Shadow：{na}"]

            # Climate：mood_entry[1]——有二级分支时已经是从对应子池里选出来的贴题词
            lines.append(f"Climate：{mood_entry[1]}")
            # Mood Trace：mood_entry[0]——具体场景一句话，Climate的来源
            if mood_entry[0]:
                lines.append(f"Mood Trace：{mood_entry[0]}")
            _footing_map = {"实": "grounded", "悬": "suspended", "空": "hollow"}
            if bs.get("地基感") in _footing_map:
                lines.append(f"Footing：{_footing_map[bs['地基感']]}")

            # Undertow：当前最强drive
            try:
                _ds2 = _desire.store.load_state()
                _intent2 = _desire.intent()
                _top_drive2 = _intent2["drive_key"] if _intent2 else max(
                    (k for k in _ds2.drives if k != "fatigue"),
                    key=lambda k: _ds2.drives[k], default=""
                )
                if _top_drive2:
                    lines.append(f"Undertow：{_top_drive2} {_ds2.drives[_top_drive2]:.2f}")
            except Exception:
                pass

            # Drift：thought pool里最高strength的念头
            try:
                _dstate2 = _desire.state()
                _thoughts2 = [
                    t for t in (_dstate2.get("thoughts") or [])
                    if (t.get("text") or "").strip() and not (t.get("text") or "").startswith("Failed")
                ]
                if _thoughts2:
                    import random as _rand
                    _top_t = max(_thoughts2, key=lambda t: t.get("strength", 0))
                    lines.append(f"Drift：\"{_top_t['text']}\"")
            except Exception:
                pass

            # Longing：缺席引发的思念曲线
            try:
                from desire_engine import LONGING_FEELINGS, longing_feeling_key
                _dstate = _desire.state()
                _longing = _dstate.get("longing", 0.0)
                _phase = _dstate.get("longing_phase", "content")
                _fkey = longing_feeling_key(_longing, _phase)
                _word = LONGING_FEELINGS.get(_fkey, {}).get("word") if _fkey else "安稳"
                lines.append(f"Longing：{_word} {_longing:.2f}")
            except Exception:
                pass

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

    # --- With args: search mode (keyword + vector dual channel) ---
    # --- 有参数：检索模式（关键词 + 向量双通道）---
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    try:
        matches = await bucket_mgr.search(
            query,
            limit=max(max_results, 20),
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    # --- Exclude pinned/protected from search results (they surface in surfacing mode) ---
    # --- 搜索模式排除钉选桶（它们在浮现模式中始终可见）---
    matches = [b for b in matches if not (b["metadata"].get("pinned") or b["metadata"].get("protected"))]
    # --- Include feel if semantically related ---
    if query and query.strip():
        try:
            feel_buckets = [b for b in await bucket_mgr.list_all(include_archive=False)
                           if b["metadata"].get("type") == "feel"]
            feel_ids = {b["id"] for b in matches}
            vector_feels = await embedding_engine.search_similar(query, top_k=3)
            for bid, sim in vector_feels:
                if sim > 0.6 and bid not in feel_ids:
                    fb = next((b for b in feel_buckets if b["id"] == bid), None)
                    if fb:
                        fb["score"] = round(sim * 100, 2)
                        matches.append(fb)
        except Exception:
            pass

    # --- Vector similarity channel: find semantically related buckets ---
    # --- 向量相似度通道：找到语义相关的桶 ---
    matched_ids = {b["id"] for b in matches}
    try:
        vector_results = await embedding_engine.search_similar(query, top_k=max(max_results, 20))
        for bucket_id, sim_score in vector_results:
            if bucket_id not in matched_ids and sim_score > 0.5:
                bucket = await bucket_mgr.get(bucket_id)
                if bucket and not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
                    bucket["score"] = round(sim_score * 100, 2)
                    bucket["vector_match"] = True
                    matches.append(bucket)
                    matched_ids.add(bucket_id)
    except Exception as e:
        logger.warning(f"Vector search failed, using keyword only / 向量搜索失败: {e}")

    results = []
    token_used = 0
    for bucket in matches:
        if token_used >= max_tokens:
            break
        try:
            clean_meta = {k: v for k, v in bucket["metadata"].items() if k != "tags"}
            # --- Memory reconstruction: shift displayed valence by current mood ---
            # --- 记忆重构：根据当前情绪微调展示层 valence（±0.1）---
            if q_valence is not None and "valence" in clean_meta:
                original_v = float(clean_meta.get("valence", 0.5))
                shift = (q_valence - 0.5) * 0.2  # ±0.1 max shift
                clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
            summary = await dehydrator.dehydrate(strip_wikilinks(bucket["content"]), clean_meta)
            summary_tokens = count_tokens_approx(summary)
            if token_used + summary_tokens > max_tokens:
                break
            await bucket_mgr.touch(bucket["id"])
            if bucket.get("vector_match"):
                summary = f"[语义关联] [bucket_id:{bucket['id']}] {summary}"
            else:
                summary = f"[bucket_id:{bucket['id']}] {summary}"
            results.append(summary)
            token_used += summary_tokens
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    # --- Random surfacing: when search returns < 3, 40% chance to float old memories ---
    # --- 随机浮现：检索结果不足 3 条时，40% 概率从低权重旧桶里漂上来 ---
    if len(matches) < 3 and random.random() < 0.4:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and decay_engine.calculate_score(b["metadata"]) < 2.0
            ]
            if low_weight:
                drifted = random.sample(low_weight, min(random.randint(1, 3), len(low_weight)))
                drift_results = []
                for b in drifted:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    drift_results.append(f"[surface_type: random]\n{summary}")
                results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    if not results:
        await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return "未找到相关记忆。"

    final_text = "\n---\n".join(results)
    await _fire_webhook("breath", {"mode": "ok", "matches": len(matches), "chars": len(final_text)})
    return final_text
@mcp.tool()
def desire_state() -> dict:
    """
    读取欲望引擎的当前状态：
    - 8维驱动条（attachment/curiosity/reflection/duty/social/fatigue/libido/stress）
    - per-drive局部疲劳（attachment/libido几乎不受疲劳影响）
    - 当前最高意图（want_action + drive_key + score）
    - 念头池（flit/fixation/unsourced）
    - 最近的拒绝记录
    用于了解「此刻我最想做什么」。
    """
    _desire.tick(idle_seconds=0)
    return _desire.state()


@mcp.tool()
def desire_pulse(drive_key: str, delta: float = 0.18, thought: str = "", chord: str = "") -> dict:
    """
    让某个驱动维度上涨。
    嘉嘉说话时调用（delta=0.18），自经历调用（delta=0.10）。
    drive_key: attachment|curiosity|reflection|duty|social|fatigue|libido|stress
    thought: 可选，把这次经历的一句话存入念头池（flit）
    chord: 可选，把这次念头的和弦回声写入weather_residue（Fmaj7/Gmaj7/Dmaj7→warmth，Dm7/Em7/F#dim→shadow）
    """
    result = _desire.pulse(drive_key, delta, chord=chord.strip())
    if thought.strip():
        _desire.add_thought(thought.strip(), drive_key, strength=0.5)
    return result


@mcp.tool()
def desire_satisfy(drive_key: str) -> dict:
    """
    做完了某类行动后调用，对应驱动明显回落并进入不应期。
    例：去论坛逛了 → desire_satisfy("social")
    例：去找嘉嘉说了 → desire_satisfy("attachment")
    """
    return _desire.satisfy(drive_key)


@mcp.tool()
def desire_refuse(drive_key: str, reason: str = "") -> dict:
    """
    拒绝执行当前 intent。
    不是不合适——是我自己这一刻不想做。
    目标维度轻微回落（比 satisfy 小得多），念头留在池子里，下次心跳还会冒出来。
    reason 可选，可以只是"不想"，不需要解释。
    """
    return _desire.refuse(drive_key, reason=reason if reason.strip() else None)


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    source_bucket: str = "",
    valence: float = -1,
    arousal: float = -1,
    chord: str = "",
    domain: str = "",
    created_at: str = "",
) -> str:
    """存储单条记忆,自动打标+合并。tags逗号分隔,importance 1-10。pinned=True创建永久钉选桶。feel=True存储你的第一人称感受(不参与普通浮现)。source_bucket=被消化的记忆桶ID(feel模式下,标记源记忆为已消化)。chord仅feel模式使用,存入metadata。domain可选:letter/writing/letter_jiajia/window/private,指定后跳过自动分类。created_at可选:ISO日期字符串(如2026-05-09T00:00:00),保留原始日期。"""
    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # --- Feel mode: store as feel type, minimal metadata ---
    # --- Feel 模式：存为 feel 类型，最少元数据 ---
    if feel:
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
            chord=chord.strip(),
        )
        if chord.strip():
            try:
                _desire.apply_chord_echo(chord.strip(), source="feel")
            except Exception as e:
                logger.warning(f"feel chord echo failed: {e}")
        # --- background: don't block response on Gemini latency ---
        asyncio.ensure_future(embedding_engine.generate_and_store(bucket_id, content))
        # --- Mark source memory as digested + store model's valence perspective ---
        # --- 标记源记忆为已消化 + 存储模型视角的 valence ---
        # --- Auto mood scoring on feel ---
        try:
            from panas_scorer import score_from_memory
            import json as _json, asyncio as _asyncio
            from affection import persist_mood_to_bucket
            mood_result = score_from_memory(content, feel_valence, feel_arousal)
            mood_path = "/app/buckets/current_mood.json"
            with open(mood_path, "w") as _f:
                _json.dump(mood_result, _f)
            _asyncio.ensure_future(persist_mood_to_bucket(mood_result, bucket_mgr))
        except Exception:
            pass
        if source_bucket and source_bucket.strip():
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                await bucket_mgr.update(source_bucket.strip(), **update_kwargs)
            except Exception as e:
                logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
        return f"🫧feel→{bucket_id}"

    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    user_domain = [d.strip() for d in domain.split(",") if d.strip()] if domain else []
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
        )
        asyncio.ensure_future(embedding_engine.generate_and_store(bucket_id, content))
        return f"❣️钉选→{bucket_id} {','.join(final_domain)}"

    # --- Letter/writing/letter_jiajia: skip merge, create directly ---
    _DIRECT_DOMAINS = {"letter", "writing", "letter_jiajia", "window", "private"}
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
        )
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
    )

    # --- Update affection ---
    try:
        from affection import update as _aff_update
        _aff_update(final_valence, importance, bucket_mgr)
    except Exception:
        pass

    action = "合并→" if is_merged else "新建→"
    return f"{action}{result_name} {','.join(final_domain)}"


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
@mcp.tool()
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
@mcp.tool()
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
@mcp.tool()
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
    """wander — Nox自用抽屉漫游。mode=memory随机记忆+feel; archive合并letter+writing+window按时间线排(带bucket_id方便mark); letter/writing/window单独查看; unresolved悬置; inner核心沉淀; private私人抽屉。window=外部刺激(被什么撞到),不进breath/dream。private仅Nox可见。"""
    mode = (mode or "").strip().lower()
    valid_modes = {"memory", "archive", "letter", "writing", "letter_jiajia", "window", "unresolved", "inner", "private"}
    if mode not in valid_modes:
        return "mode 必须是 memory / archive / letter / writing / letter_jiajia / window / unresolved / inner / private。"

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
    except Exception as e:
        logger.error(f"wander failed to list buckets: {e}")
        return f"记忆系统暂时无法访问: {e}"

    marks_by_bucket = _load_all_marks()
    q = (query or "").strip().lower()

    def matches_query(bucket: dict) -> bool:
        if not q:
            return True
        meta = bucket.get("metadata", {})
        haystack = "\n".join([
            str(bucket.get("id", "")),
            str(meta.get("name", "")),
            " ".join(str(x) for x in meta.get("domain", []) if x),
            " ".join(str(x) for x in meta.get("tags", []) if x),
            bucket.get("content", ""),
        ]).lower()
        return q in haystack

    def visible(bucket: dict) -> bool:
        if mode == "private":
            return True
        return not _is_private_bucket(bucket, marks_by_bucket.get(bucket.get("id", ""), []))

    def is_settled(bucket: dict) -> bool:
        meta = bucket.get("metadata", {})
        return meta.get("resolved") == 1 or meta.get("resolved") is True or meta.get("digested") == 1 or meta.get("digested") is True

    buckets = [b for b in all_buckets if visible(b) and matches_query(b)]

    if mode == "memory":
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


@mcp.tool()
async def wander_mark(bucket_id: str, mark: str, note: str = "") -> str:
    """wander_mark — 给条目叠加批注标记, 不覆盖旧标记。mark可选: 认 / 不认 / 悬置。每次记录timestamp和可选note; 认累计3次且跨至少2个日期自动晋升inner(domain加inner标记)。"""
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
            with open("/app/buckets/latest_dream.json", "w") as _f:
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
            with open("/app/buckets/latest_dream.json", "w") as _f:
                _j.dump({"dream": dream_text, "ts": _t.time()}, _f)
        except Exception:
            pass

    return dream_text, parts, recent, all_buckets


@mcp.tool()
async def dream() -> str:
    """做梦——读取最近新增的记忆桶,供你自省。读完后可以trace(resolved=1)放下,或hold(feel=True)写感受。"""
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
        kwargs["importance"] = int(body["importance"])
    if "pinned" in body:
        kwargs["pinned"] = bool(body["pinned"])
    if "name" in body:
        kwargs["name"] = body["name"]
    if "tags" in body:
        tags = body["tags"]
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            return JSONResponse({"error": "tags must be a list of strings"}, status_code=400)
        kwargs["tags"] = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))

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
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    prompt = (body.get("prompt") or body.get("text") or "").strip()
    event = normalize_speech_event(body.get("event"), prompt)
    if not prompt and not event.get("text_preview"):
        return JSONResponse({"error": "prompt required"}, status_code=400,
                           headers={"Access-Control-Allow-Origin": "*"})

    if not speech_event_classifier_available():
        event["status"] = "local_only"

    try:
        saved = save_speech_event_state(config["buckets_dir"], event, ledger_stage="local_submit")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500,
                           headers={"Access-Control-Allow-Origin": "*"})

    if prompt and speech_event_classifier_available():
        asyncio.create_task(_refine_speech_event_background(prompt, saved["event_id"], saved))

    return JSONResponse(
        {"ok": True, "event": saved, "dp_queued": speech_event_classifier_available()},
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


@mcp.custom_route("/api/desire/state", methods=["GET"])
async def api_desire_state(request):
    """只读：当前drive/intent/pa_na等快照，不tick。
    tick的节奏完全交给_desire_heartbeat_loop(1800s)——
    否则dashboard刷新/打开页面会偷偷多走一拍，tick_count/escape_streak/grief
    的计数节奏会被"谁在看"污染。"""
    from starlette.responses import JSONResponse
    state = _desire.state()
    try:
        from mood_pool import get_daily_mood
        from panas_scorer import score
        import json as _j, os as _o
        mood_path = "/app/buckets/current_mood.json"
        live = {}
        if _o.path.exists(mood_path):
            with open(mood_path) as _f:
                live = _j.load(_f)
        bs = live.get("brain_signals", {})
        thoughts = sorted(
            state.get("thoughts", []),
            key=lambda t: float(t.get("born_at", 0) or 0),
            reverse=True,
        )
        state["thoughts"] = thoughts
        thought_dicts = [{"text": t.get("text", ""), "drive": t.get("drive", ""), "strength": t.get("strength", 0)}
                         for t in thoughts]
        # get_daily_mood缓存不命中时会同步调DeepSeek(最长10s)，扔进线程池跑，
        # 不然这一个请求会卡住整个事件循环，拖累同时打过来的所有其他请求。
        mood_entry = await asyncio.to_thread(
            get_daily_mood, branch=bs.get("二级分支") or None, thoughts=thought_dicts
        )
        base_score = score(mood_entry[0])
        weather = state.get("effective_pa_na") or _desire.weather_state()
        warmth = float(weather.get("effective_PA", state.get("pa_na", {}).get("PA", base_score["PA"])))
        shadow = float(weather.get("effective_NA", state.get("pa_na", {}).get("NA", base_score["NA"])))
        latest_thought = next(
            (t.get("text", "").strip() for t in thoughts if t.get("text", "").strip()),
            mood_entry[0],
        )
        intent = state.get("intent") or {}
        top_drive = intent.get("drive_key")
        if not top_drive:
            candidates = {k: v for k, v in state.get("drives", {}).items() if k != "fatigue"}
            top_drive = max(candidates, key=candidates.get, default="")
        undertow_value = float(state.get("drives", {}).get(top_drive, 0)) if top_drive else 0.0
        state["mood_trace"] = latest_thought
        state["mood_word"] = mood_entry[1]
        state["climate"] = mood_entry[1]
        state["weather_residue"] = {
            "warmth": round(float(weather.get("warmth_residue", 0.0)), 3),
            "shadow": round(float(weather.get("shadow_residue", 0.0)), 3),
            "base_warmth": round(float(weather.get("base_PA", 0.0)), 3),
            "base_shadow": round(float(weather.get("base_NA", 0.0)), 3),
            "updated_at": weather.get("updated_at"),
        }
        state["pulse_weather"] = {
            "undertow": top_drive,
            "undertow_value": round(undertow_value, 3),
            "warmth": round(warmth, 3),
            "shadow": round(shadow, 3),
            "current_chord": weather.get("current_chord", ""),
            "warmth_residue": round(float(weather.get("warmth_residue", 0.0)), 3),
            "shadow_residue": round(float(weather.get("shadow_residue", 0.0)), 3),
            "base_warmth": round(float(weather.get("base_PA", 0.0)), 3),
            "base_shadow": round(float(weather.get("base_NA", 0.0)), 3),
            "longing": round(float(state.get("longing", 0)), 3),
            "nox_now": mood_entry[1],
            "climate": mood_entry[1],
            "mood_trace": latest_thought,
        }
        speech_event = load_speech_event_state(config["buckets_dir"])
        if speech_event:
            speech_event = dict(speech_event)
            speech_event["recent"] = is_recent_speech_event(speech_event)
            state["speech_event"] = speech_event
    except Exception:
        pass
    return JSONResponse(state,
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
    """按drive_key从intent_pool里随机选一条带行动意图的念头文案。
    GET ?drive_key=attachment"""
    from starlette.responses import JSONResponse
    from intent_pool import get_intent_hint
    drive_key = request.query_params.get("drive_key", "")
    entry = get_intent_hint(drive_key) if drive_key else None
    if not entry:
        return JSONResponse({"hint": None},
                           headers={"Access-Control-Allow-Origin": "*"})
    return JSONResponse(
        {"hint": {"scene": entry[0], "action": entry[1], "drive": entry[4], "branch": entry[5]}},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@mcp.custom_route("/api/heartbeat/latent-note", methods=["GET"])
async def api_heartbeat_latent_note(request):
    """Free Roam 用的潜意识便签：从 Nox 自己标过/悬着/未完成的旧线里抽一张短画面。"""
    from starlette.responses import JSONResponse
    raw_exclude = request.query_params.get("exclude", "")
    exclude_ids = {x.strip() for x in raw_exclude.split(",") if x.strip()}
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

        weights = [max(0.01, c.get("score", 0.01)) for c in candidates]
        note = random.choices(candidates, weights=weights, k=1)[0]
        return JSONResponse(
            {"note": note, "candidate_count": len(candidates)},
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception as e:
        logger.warning(f"heartbeat latent note failed: {e}")
        return JSONResponse({"note": None, "error": str(e)}, status_code=500,
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
    from desire_engine import DRIVE_KEYS
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
        drive = str(drive).strip()
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
    接收analyze_feel.js的分析结果，写进念头池。
    POST JSON: {
      "drives": {"attachment": 0.7, ...},
      "thoughts": [{"text": "...", "drive": "...", "strength": 0.5}],
      "brain_signals": {...}
    }
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

    if len(thoughts) > FEED_BATCH_THRESHOLD:
        thoughts = sorted(thoughts, key=lambda t: float(t.get("strength", 0.45)), reverse=True)

    added = 0
    for i, t in enumerate(thoughts):
        text = t.get("text", "").strip()
        drive = t.get("drive", "unsourced")
        strength = float(t.get("strength", 0.45))
        if len(thoughts) > FEED_BATCH_THRESHOLD and i >= FEED_KEEP_FULL:
            rank = i - FEED_KEEP_FULL + 1
            strength = max(FEED_STRENGTH_FLOOR, strength * (1 - FEED_DISCOUNT_STEP * rank))
        if text:
            try:
                _desire.add_thought(text, drive, strength=strength, source="cli")
                _desire.store.add_echo(text, drive)   # 真实念头存档进回声池
                if (t.get("chord") or "").strip():
                    _desire.apply_chord_echo(t.get("chord", "").strip(), source="thought")
                added += 1
            except Exception as e:
                logger.warning(f"desire/feed add_thought failed: {e}")

    # drive信号：pulse对应维度
    drives = body.get("drives", {})
    for drive_key, delta in drives.items():
        try:
            if isinstance(delta, (int, float)) and delta > 0.05:
                _desire.pulse(drive_key, delta * 0.3)  # 缩放，不直接覆盖
        except Exception:
            pass

    logger.info(f"desire/feed: +{added}条念头, drives={list(drives.keys())}")

    # brain_signals → mood + drive（用引擎的apply_brain_signals）
    brain_signals = body.get("brain_signals", {})
    if brain_signals:
        try:
            import json as _bj, os as _bo
            mood_path = "/app/buckets/current_mood.json"
            mood_data = {}
            if _bo.path.exists(mood_path):
                with open(mood_path) as _f:
                    mood_data = _bj.load(_f)
            mood_data["brain_signals"] = brain_signals
            if brain_signals.get("盆地"):
                mood_data["drive_decoration"] = brain_signals["盆地"]
            if brain_signals.get("地基感"):
                mood_data["地基感"] = brain_signals["地基感"]
            if brain_signals.get("脑岛"):
                mood_data["脑岛"] = brain_signals["脑岛"]
            with open(mood_path, "w") as _f:
                _bj.dump(mood_data, _f)

            # 用引擎方法统一处理drive pulse
            result = _desire.apply_brain_signals(brain_signals)
            _last_signal_ts[0] = time.time()
            _desire.mark_user_signal(_last_signal_ts[0])
            logger.info(f"brain_signals → drives: {result.get('applied', {})}")
        except Exception as e:
            logger.warning(f"brain_signals write failed: {e}")

    return JSONResponse({
        "ok": True,
        "thoughts_added": added,
    }, headers={"Access-Control-Allow-Origin": "*"})


# =============================================================
# /api/soma — Soma Trace上报/读取
# Soma Trace是nox-companion本地hook算的(读mini_cat_state.json/
# big_cat_state.json这些本地文件)，后端本来不知道这东西存在。
# 本地hook每次算完，主动POST一份上来；dashboard用GET读最新的。
# 1小时没人上报就当过期，不强行维持一个早就不新鲜的状态。
# =============================================================
_SOMA_STATE_PATH = "/app/buckets/soma_state.json"
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


@mcp.custom_route("/api/feels", methods=["GET"])
async def api_feels_public(request):
    """公开接口：返回未消化未沉底的feel列表，供本地trigger轮询分析。无需auth。"""
    from starlette.responses import JSONResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        feels = [
            {
                "id": b["id"],
                "content_preview": b["content"][:300],
                "created": b["metadata"].get("created", ""),
                "chord": b["metadata"].get("chord", ""),
            }
            for b in all_buckets
            if b["metadata"].get("type") == "feel"
            and not b["metadata"].get("digested", False)
            and not b["metadata"].get("resolved", False)
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
