from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from utils import now_iso


CATROOM_AUTHORS = {"ink", "ash", "moss", "nox", "jiajia"}


class CatroomStore:
    """Append-only public note room for cat-house messages.

    This store intentionally stays outside buckets, Breath, DesireEngine, and
    weather residue. Catroom is a room, not memory.
    """

    def __init__(self, buckets_dir: str | os.PathLike[str]):
        self.path = Path(buckets_dir) / "catroom.jsonl"
        self._lock = threading.RLock()

    def hold(
        self,
        *,
        author: str,
        content: str,
        topic: str | None = None,
        mood: str | None = None,
        model: str | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        author = self._normalize_author(author)
        content = str(content or "").strip()
        if not content:
            raise ValueError("content is required")
        parent = str(reply_to or "").strip() or None
        if parent and not self.get(parent):
            raise ValueError(f"reply_to not found: {parent}")
        record = {
            "id": "cat_" + uuid.uuid4().hex[:16],
            "ts": now_iso(),
            "author": author,
            "content": content,
            "topic": self._clean_optional(topic),
            "mood": self._clean_optional(mood),
            "model": self._clean_optional(model),
            "reply_to": parent,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def reply(
        self,
        *,
        author: str,
        reply_to: str,
        content: str,
        topic: str | None = None,
        mood: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        parent_id = str(reply_to or "").strip()
        if not parent_id:
            raise ValueError("reply_to is required")
        return self.hold(
            author=author,
            content=content,
            topic=topic,
            mood=mood,
            model=model,
            reply_to=parent_id,
        )

    def read(
        self,
        *,
        limit: int = 15,
        topic: str | None = None,
        author: str | None = None,
    ) -> list[dict[str, Any]]:
        records = self._read_all()
        topic_filter = self._clean_optional(topic)
        author_filter = self._clean_optional(author)
        if author_filter:
            author_filter = self._normalize_author(author_filter)
            records = [r for r in records if r.get("author") == author_filter]
        if topic_filter:
            if topic_filter.lower() == "catroom":
                records = [r for r in records if not r.get("topic") or r.get("topic") == "Catroom"]
            else:
                records = [r for r in records if r.get("topic") == topic_filter]
        limit = max(1, min(int(limit or 15), 100))
        return records[-limit:]

    def get(self, record_id: str) -> dict[str, Any] | None:
        target = str(record_id or "").strip()
        if not target:
            return None
        for record in reversed(self._read_all()):
            if record.get("id") == target:
                return record
        return None

    def update(
        self,
        record_id: str,
        *,
        author: str | None = None,
        content: str | None = None,
        topic: str | None = None,
        mood: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        target = str(record_id or "").strip()
        if not target:
            raise ValueError("id is required")
        with self._lock:
            records = self._read_all()
            for idx, record in enumerate(records):
                if record.get("id") != target:
                    continue
                if author is not None:
                    record["author"] = self._normalize_author(author)
                if content is not None:
                    cleaned_content = str(content or "").strip()
                    if not cleaned_content:
                        raise ValueError("content is required")
                    record["content"] = cleaned_content
                if topic is not None:
                    record["topic"] = self._clean_optional(topic)
                if mood is not None:
                    record["mood"] = self._clean_optional(mood)
                if model is not None:
                    record["model"] = self._clean_optional(model)
                record["edited_ts"] = now_iso()
                records[idx] = record
                self._write_all(records)
                return record
        raise ValueError(f"note not found: {target}")

    def delete(self, record_id: str) -> dict[str, Any]:
        target = str(record_id or "").strip()
        if not target:
            raise ValueError("id is required")
        with self._lock:
            records = self._read_all()
            kept = [r for r in records if r.get("id") != target]
            if len(kept) == len(records):
                raise ValueError(f"note not found: {target}")
            self._write_all(kept)
        return {"id": target}

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._lock:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        return records

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        tmp_path.replace(self.path)

    @staticmethod
    def _clean_optional(value: str | None) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _normalize_author(author: str) -> str:
        normalized = str(author or "").strip().lower()
        if normalized not in CATROOM_AUTHORS:
            raise ValueError(f"author must be one of: {', '.join(sorted(CATROOM_AUTHORS))}")
        return normalized
