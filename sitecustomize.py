"""Runtime tool-schema patches for Nocturne's stricter search behavior.

Python imports sitecustomize automatically during interpreter startup when this
file is on sys.path. The server entrypoint already runs from the repository
root, so this lets us adjust MCP tool registration without rewriting the large
server.py file.
"""
from __future__ import annotations

import functools
import inspect
import re
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - startup safety if MCP is unavailable
    FastMCP = None  # type: ignore[assignment]


_TRACE_LIMIT = 15
_EXCLUDED_BUCKET_TYPES = {"breath", "dream", "archived"}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _truthy(value: Any) -> bool:
    return value is True or value == 1 or str(value).strip().lower() in {"1", "true", "yes", "on"}


def _trace_terms(raw_query: str) -> list[str]:
    raw_query = (raw_query or "").strip().lower()
    if not raw_query:
        return []
    return [part.strip() for part in re.split(r"[\s,，、/|]+", raw_query) if part.strip()]


def _exact_trace_match(bucket: dict, terms: list[str]) -> bool:
    """Hard match only: content substring OR exact domain/tag match."""
    if not terms:
        return False
    meta = bucket.get("metadata", {}) or {}
    content = str(bucket.get("content", "") or "").lower()
    tags = {str(x).strip().lower() for x in _as_list(meta.get("tags")) if str(x).strip()}
    domains = {str(x).strip().lower() for x in _as_list(meta.get("domain")) if str(x).strip()}
    labels = tags | domains
    return any(term in content or term in labels for term in terms)


def _trace_type_label(bucket: dict, globals_: dict) -> str:
    meta = bucket.get("metadata", {}) or {}
    bucket_id = bucket.get("id", "")
    marks_by_bucket = globals_.get("_trace_marks_by_bucket", {}) or {}
    mark_rows = marks_by_bucket.get(bucket_id, [])
    bucket_domains = globals_.get("_bucket_domains")
    bucket_tags = globals_.get("_bucket_tags")
    guess_wander_domain = globals_.get("_guess_wander_domain")
    mark_counts = globals_.get("_mark_counts")

    if str(meta.get("type", "") or "").lower() == "feel":
        base = "feel"
    else:
        domains = bucket_domains(meta) if callable(bucket_domains) else {str(x).strip().lower() for x in _as_list(meta.get("domain")) if str(x).strip()}
        tags = bucket_tags(meta) if callable(bucket_tags) else {str(x).strip().lower() for x in _as_list(meta.get("tags")) if str(x).strip()}
        if "letter_jiajia" in domains or "letter_jiajia" in tags:
            base = "letter_jiajia"
        elif "letter" in domains or "letter" in tags:
            base = "letter"
        elif "writing" in domains or "writing" in tags:
            base = "writing"
        elif "window" in domains or "window" in tags:
            base = "window"
        elif callable(mark_counts) and mark_counts(mark_rows).get("悬置", 0) > 0:
            base = "unresolved"
        else:
            base = "memory"

    if base != "feel" and callable(guess_wander_domain) and guess_wander_domain(bucket, mark_rows) == "inner":
        return f"{base}→inner"
    return base


def _wrap_breath(func):
    @functools.wraps(func)
    async def breath_without_search(
        domain: str = "",
        valence: float = -1,
        arousal: float = -1,
        max_results: int = 20,
        importance_min: int = -1,
    ) -> str:
        # Query search is intentionally removed from breath. Exact keyword
        # lookup now belongs to trace only.
        return await func(
            query="",
            max_tokens=10000,
            domain=domain,
            valence=valence,
            arousal=arousal,
            max_results=max_results,
            importance_min=importance_min,
        )

    breath_without_search.__signature__ = inspect.Signature(parameters=[
        inspect.Parameter("domain", inspect.Parameter.POSITIONAL_OR_KEYWORD, default="", annotation=str),
        inspect.Parameter("valence", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=-1, annotation=float),
        inspect.Parameter("arousal", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=-1, annotation=float),
        inspect.Parameter("max_results", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=20, annotation=int),
        inspect.Parameter("importance_min", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=-1, annotation=int),
    ], return_annotation=str)
    breath_without_search.__doc__ = "新窗或者Compact后读取Nocturne记忆。关键词搜索请用 trace。"
    return breath_without_search


def _wrap_trace(func):
    @functools.wraps(func)
    async def strict_trace(query: str, limit: int = _TRACE_LIMIT) -> str:
        query = (query or "").strip()
        if not query:
            return "trace 要带 query。它是全量关键词搜索，不是 Breath 浮现。"

        globals_ = func.__globals__
        bucket_mgr = globals_.get("bucket_mgr")
        load_all_marks = globals_.get("_load_all_marks")
        is_private_bucket = globals_.get("_is_private_bucket")
        format_wander_entry = globals_.get("_format_wander_entry")
        if bucket_mgr is None or not callable(load_all_marks) or not callable(format_wander_entry):
            return "trace 暂时不可用：运行时组件未初始化。"

        try:
            all_buckets = await bucket_mgr.list_all(include_archive=True)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"

        marks_by_bucket = load_all_marks()
        globals_["_trace_marks_by_bucket"] = marks_by_bucket
        terms = _trace_terms(query)
        hard_limit = max(1, min(int(limit or _TRACE_LIMIT), _TRACE_LIMIT))

        selected: list[dict] = []
        for bucket in all_buckets:
            meta = bucket.get("metadata", {}) or {}
            bucket_id = bucket.get("id", "")
            btype = str(meta.get("type", "") or "").lower()
            if btype in _EXCLUDED_BUCKET_TYPES:
                continue
            # 已沉底/已消化的条目保留在库里，但 trace 不再主动捞出来。
            if _truthy(meta.get("resolved")) or _truthy(meta.get("digested")):
                continue
            mark_rows = marks_by_bucket.get(bucket_id, [])
            if callable(is_private_bucket) and is_private_bucket(bucket, mark_rows):
                continue
            if not _exact_trace_match(bucket, terms):
                continue
            selected.append(bucket)

        selected.sort(key=lambda b: b.get("metadata", {}).get("created", ""), reverse=True)
        selected = selected[:hard_limit]
        if not selected:
            return "null"

        parts = []
        for bucket in selected:
            mark_rows = marks_by_bucket.get(bucket.get("id", ""), [])
            label = _trace_type_label(bucket, globals_)
            parts.append(
                f"〔{label}〕" + format_wander_entry(
                    bucket,
                    mark_rows,
                    include_full_content=True,
                    show_bucket_id=True,
                )
            )
        return "=== Trace ===\n" + "\n---\n".join(parts)

    strict_trace.__signature__ = inspect.Signature(parameters=[
        inspect.Parameter("query", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
        inspect.Parameter("limit", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=_TRACE_LIMIT, annotation=int),
    ], return_annotation=str)
    strict_trace.__doc__ = "全量硬关键词搜索。只匹配正文包含关键词或标签/domain精准命中；最多15条；无命中返回null。"
    return strict_trace


if FastMCP is not None and not getattr(FastMCP, "_nocturne_search_patch", False):
    _original_tool = FastMCP.tool

    def _patched_tool(self, *args, **kwargs):
        direct_func = args[0] if args and callable(args[0]) else None
        declared_name = kwargs.get("name") if isinstance(kwargs.get("name"), str) else None

        if direct_func is not None:
            name = declared_name or getattr(direct_func, "__name__", "")
            if name == "breath":
                direct_func = _wrap_breath(direct_func)
            elif name == "trace":
                direct_func = _wrap_trace(direct_func)
            return _original_tool(self, direct_func, **kwargs)

        decorator = _original_tool(self, *args, **kwargs)

        def _decorator(func):
            name = declared_name or getattr(func, "__name__", "")
            if name == "breath":
                func = _wrap_breath(func)
            elif name == "trace":
                func = _wrap_trace(func)
            return decorator(func)

        return _decorator

    FastMCP.tool = _patched_tool
    FastMCP._nocturne_search_patch = True
