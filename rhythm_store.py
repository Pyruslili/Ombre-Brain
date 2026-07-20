"""Rhythm — 嘉嘉外场节律：phone / watch / 未来源 统一入库与快照。

不是聊天管道。写入端：快捷指令 / iWatch / 本地 hook。
读取端：MCP rhythm.read / undercurrent / heartbeat 注入。
推送端：rhythm.push → Bark（只用于主动找你，不绑回复）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


DEFAULT_MAX_EVENTS = 400
DEFAULT_READ_MINUTES = 180
DEFAULT_READ_LIMIT = 5  # 读快照默认最近 5 条，别拉一长串
BARK_PUSH_URL = "https://api.day.app/push"
# Bark's Notification Service Extension caches icon bytes forever by the full
# URL. Keep a version fingerprint here so a failed/stale first download cannot
# pin the default Bark app icon indefinitely on the phone.
DEFAULT_BARK_ICON_URL = (
    "https://raw.githubusercontent.com/Pyruslili/Ombre-Brain/"
    "main/docs/assets/nox-bark-avatar.png?v=20260720-1627"
)
# 写入噪声：这些名字通常是调试残留，不当「在刷什么」
NOISE_APPS = {
    "bark",
    "快捷指令",
    "shortcuts",
    "设置",
    "settings",
    "springboard",
}


class RhythmStore:
    def __init__(self, path: str | Path, max_events: int = DEFAULT_MAX_EVENTS):
        self.path = Path(path)
        self.max_events = max(1, int(max_events))
        self._lock = threading.Lock()

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "events": [], "updated_at": 0.0}

    def _load(self) -> dict[str, Any]:
        try:
            if not self.path.exists():
                return self._empty()
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return self._empty()
            events = data.get("events")
            if not isinstance(events, list):
                data["events"] = []
            return data
        except Exception:
            return self._empty()

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        events = data.get("events") if isinstance(data.get("events"), list) else []
        if len(events) > self.max_events:
            data["events"] = events[-self.max_events :]
        data["updated_at"] = time.time()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _is_noise_app(app: str) -> bool:
        name = (app or "").strip().lower()
        if not name:
            return True
        return name in NOISE_APPS

    def append(
        self,
        *,
        source: str,
        event: str = "open",
        app: str = "",
        kind: str = "",
        value: Any = None,
        meta: dict | None = None,
        at: float | None = None,
    ) -> dict[str, Any]:
        source = (source or "unknown").strip().lower()[:32] or "unknown"
        event = (event or "open").strip().lower()[:32] or "open"
        app = (app or "").strip()[:80]
        kind = (kind or "").strip().lower()[:40]
        ts = float(at) if at is not None else time.time()

        # phone 必须带 app 名；空名 / 噪声 app 直接丢，不当事件
        if source == "phone":
            if not app or self._is_noise_app(app):
                return {
                    "skipped": True,
                    "reason": "empty_or_noise_app",
                    "app": app or None,
                    "source": source,
                }

        record: dict[str, Any] = {
            "at": ts,
            "source": source,
            "event": event,
        }
        if app:
            record["app"] = app
        if kind:
            record["kind"] = kind
        if value is not None and value != "":
            record["value"] = value
        if isinstance(meta, dict) and meta:
            # keep small
            clean = {str(k)[:40]: meta[k] for k in list(meta)[:12]}
            record["meta"] = clean

        with self._lock:
            data = self._load()
            events = data.get("events") if isinstance(data.get("events"), list) else []
            # 同一 app 连续 open 合并：只刷新时间，不堆一串重复
            if (
                source == "phone"
                and event == "open"
                and app
                and events
                and isinstance(events[-1], dict)
                and events[-1].get("source") == "phone"
                and events[-1].get("app") == app
                and events[-1].get("event") == "open"
            ):
                events[-1]["at"] = ts
                data["events"] = events
                self._save(data)
                record = dict(events[-1])
                record["merged"] = True
                return record
            events.append(record)
            data["events"] = events
            self._save(data)
        return record

    def read(
        self,
        *,
        minutes: float = DEFAULT_READ_MINUTES,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> dict[str, Any]:
        minutes = max(1.0, float(minutes or DEFAULT_READ_MINUTES))
        limit = max(1, min(50, int(limit or DEFAULT_READ_LIMIT)))
        cutoff = time.time() - minutes * 60.0

        with self._lock:
            data = self._load()
            events = data.get("events") if isinstance(data.get("events"), list) else []

        def _keep(e: dict) -> bool:
            if float(e.get("at") or 0) < cutoff:
                return False
            src = e.get("source")
            if src == "phone":
                app = str(e.get("app") or "").strip()
                if not app or self._is_noise_app(app):
                    return False
            return True

        recent = [e for e in events if isinstance(e, dict) and _keep(e)]
        recent.sort(key=lambda e: float(e.get("at") or 0), reverse=True)
        clipped = recent[:limit]

        phone_events = [e for e in clipped if e.get("source") == "phone"]
        watch_events = [e for e in clipped if e.get("source") == "watch"]
        other_events = [
            e for e in clipped if e.get("source") not in {"phone", "watch"}
        ]

        last_any = clipped[0] if clipped else None
        last_phone = phone_events[0] if phone_events else None
        last_watch = watch_events[0] if watch_events else None

        now = time.time()
        last_at = float(last_any.get("at") or 0) if last_any else 0.0
        idle_minutes = round((now - last_at) / 60.0, 1) if last_at else None

        hr = None
        hr_at = None
        for e in watch_events:
            kind = str(e.get("kind") or "")
            if kind in {"hr", "heart_rate", "heartrate"} or e.get("value") is not None:
                try:
                    hr = float(e.get("value"))
                    hr_at = float(e.get("at") or 0) or None
                    break
                except (TypeError, ValueError):
                    continue

        note = self._note_line(
            last_phone=last_phone,
            last_watch=last_watch,
            hr=hr,
            idle_minutes=idle_minutes,
            now=now,
        )

        return {
            "ok": True,
            "idle_minutes": idle_minutes,
            "last_event_at": last_at or None,
            "window_minutes": minutes,
            "phone": {
                "last_app": (last_phone or {}).get("app") or None,
                "last_at": float((last_phone or {}).get("at") or 0) or None,
                "last_event": (last_phone or {}).get("event"),
                "recent": [
                    {
                        "app": e.get("app"),
                        "event": e.get("event"),
                        "at": e.get("at"),
                    }
                    for e in phone_events[:limit]
                ],
            },
            "watch": {
                "hr": hr,
                "hr_at": hr_at,
                "recent": [
                    {
                        "kind": e.get("kind"),
                        "value": e.get("value"),
                        "event": e.get("event"),
                        "at": e.get("at"),
                    }
                    for e in watch_events[:limit]
                ],
            },
            "other": [
                {
                    "source": e.get("source"),
                    "app": e.get("app"),
                    "kind": e.get("kind"),
                    "event": e.get("event"),
                    "value": e.get("value"),
                    "meta": e.get("meta") if isinstance(e.get("meta"), dict) else None,
                    "at": e.get("at"),
                }
                for e in other_events[:limit]
            ],
            "note": note,
            "count": len(clipped),
        }

    @staticmethod
    def _ago(ts: float | None, now: float) -> str:
        if not ts:
            return ""
        mins = max(0, int((now - ts) / 60))
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 48:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"

    def _note_line(
        self,
        *,
        last_phone: dict | None,
        last_watch: dict | None,
        hr: float | None,
        idle_minutes: float | None,
        now: float,
    ) -> str:
        parts: list[str] = []
        if last_phone and last_phone.get("app"):
            ago = self._ago(float(last_phone.get("at") or 0), now)
            parts.append(f"{last_phone['app']}" + (f" · {ago}" if ago else ""))
        if hr is not None:
            parts.append(f"hr {hr:g}")
        elif last_watch and last_watch.get("kind"):
            parts.append(str(last_watch.get("kind")))
        if idle_minutes is not None and (not last_phone or idle_minutes >= 15):
            parts.append(f"idle {idle_minutes:g}m")
        return " · ".join(parts) if parts else ""


def normalize_bark_key(value: str = "") -> str:
    """Accept a device key or a full api.day.app notification URL.

    Bark examples often present the key inside a send URL.  Only unwrap URLs on
    Bark's own host; an arbitrary URL must never be silently treated as trusted.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    if parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() == "api.day.app":
        parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
        return parts[0] if parts else ""
    return raw


def is_bark_api_url(value: str = "") -> bool:
    try:
        parsed = urlparse((value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").lower() == "api.day.app"
    )


def resolve_bark_key(explicit: str = "") -> str:
    key = normalize_bark_key(explicit)
    if key:
        return key
    key = normalize_bark_key(os.environ.get("BARK_KEY", ""))
    if key:
        return key
    path = Path(os.path.expanduser("~/.bark_device_key"))
    try:
        if path.exists():
            stored = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            return normalize_bark_key(stored)
    except Exception:
        pass
    return ""


def resolve_bark_icon(explicit: str = "") -> str:
    icon = (explicit or "").strip()
    if icon:
        return icon
    icon = (os.environ.get("BARK_ICON_URL") or "").strip()
    if icon:
        return icon
    try:
        path = Path(os.path.expanduser("~/.config/nox/bark_icon_url"))
        if path.exists():
            icon = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            if icon:
                return icon
    except Exception:
        pass
    return DEFAULT_BARK_ICON_URL


def send_bark(
    *,
    title: str,
    body: str,
    key: str = "",
    icon: str = "",
    group: str = "NoxRhythm",
) -> dict[str, Any]:
    bark_key = resolve_bark_key(key)
    if not bark_key:
        return {"ok": False, "error": "BARK_KEY missing (env or ~/.bark_device_key)"}

    title = (title or "Nox").strip()[:80] or "Nox"
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "body required"}
    if len(body) > 500:
        body = body[:497] + "..."

    payload: dict[str, Any] = {
        "device_key": bark_key,
        "title": title,
        "body": body,
        "group": group or "NoxRhythm",
    }
    # 默认 Nox 人形 + 黑猫头像（GH raw）；可被 BARK_ICON_URL /
    # ~/.config/nox/bark_icon_url 覆盖。版本指纹用来避开 Bark 永久本地缓存。
    icon = resolve_bark_icon(icon)
    if icon:
        payload["icon"] = icon

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BARK_PUSH_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        return {"ok": False, "error": f"HTTP {e.code}", "detail": raw[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        parsed = {"raw": raw[:300]}

    code = parsed.get("code") if isinstance(parsed, dict) else None
    if status != 200 or (code is not None and code != 200):
        return {
            "ok": False,
            "error": (parsed.get("message") if isinstance(parsed, dict) else None)
            or f"status={status}",
            "detail": parsed,
        }
    return {"ok": True, "provider": "bark", "result": parsed}
