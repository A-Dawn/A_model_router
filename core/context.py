import contextvars
import threading
from dataclasses import asdict
from typing import Any, Dict, Optional

from .router_types import RouteContext

_route_context_var: contextvars.ContextVar[Optional[RouteContext]] = contextvars.ContextVar(
    "a_model_router_context",
    default=None,
)

_stream_meta_lock = threading.Lock()
_stream_metadata: Dict[str, Dict[str, str]] = {}


def _coalesce(new_value: Optional[str], old_value: str) -> str:
    if new_value is None:
        return old_value
    value = str(new_value).strip()
    return value if value else old_value


def get_current_context() -> Optional[RouteContext]:
    return _route_context_var.get()


def push_route_context(
    *,
    stream_id: Optional[str] = None,
    platform: Optional[str] = None,
    chat_type: Optional[str] = None,
    group_id: Optional[str] = None,
    user_id: Optional[str] = None,
    function_tag: Optional[str] = None,
    request_type: Optional[str] = None,
) -> contextvars.Token:
    current = get_current_context() or RouteContext()
    merged = RouteContext(
        stream_id=_coalesce(stream_id, current.stream_id),
        platform=_coalesce(platform, current.platform),
        chat_type=_coalesce(chat_type, current.chat_type),
        group_id=_coalesce(group_id, current.group_id),
        user_id=_coalesce(user_id, current.user_id),
        function_tag=_coalesce(function_tag, current.function_tag),
        request_type=_coalesce(request_type, current.request_type),
    )
    return _route_context_var.set(merged)


def pop_route_context(token: contextvars.Token) -> None:
    _route_context_var.reset(token)


def bind_stream_metadata(
    stream_id: str,
    *,
    platform: str = "",
    chat_type: str = "",
    group_id: str = "",
    user_id: str = "",
) -> None:
    stream_key = str(stream_id or "").strip()
    if not stream_key:
        return
    with _stream_meta_lock:
        old = _stream_metadata.get(stream_key, {})
        _stream_metadata[stream_key] = {
            "platform": str(platform or old.get("platform", "")).strip(),
            "chat_type": str(chat_type or old.get("chat_type", "")).strip(),
            "group_id": str(group_id or old.get("group_id", "")).strip(),
            "user_id": str(user_id or old.get("user_id", "")).strip(),
        }


def get_stream_metadata(stream_id: str) -> Dict[str, str]:
    stream_key = str(stream_id or "").strip()
    if not stream_key:
        return {}
    with _stream_meta_lock:
        return dict(_stream_metadata.get(stream_key, {}))


def list_stream_metadata() -> Dict[str, Dict[str, str]]:
    with _stream_meta_lock:
        return {k: dict(v) for k, v in _stream_metadata.items()}


def clear_stream_metadata() -> None:
    with _stream_meta_lock:
        _stream_metadata.clear()


def clear_all_context() -> None:
    _route_context_var.set(None)


def resolve_context(ctx: Optional[RouteContext] = None) -> RouteContext:
    base = ctx or get_current_context() or RouteContext()
    resolved = RouteContext(**asdict(base))
    if resolved.stream_id:
        meta = get_stream_metadata(resolved.stream_id)
        if meta:
            if not resolved.platform:
                resolved.platform = meta.get("platform", "")
            if not resolved.chat_type:
                resolved.chat_type = meta.get("chat_type", "")
            if not resolved.group_id:
                resolved.group_id = meta.get("group_id", "")
            if not resolved.user_id:
                resolved.user_id = meta.get("user_id", "")
    return resolved


def build_context_snapshot() -> Dict[str, Any]:
    current = resolve_context()
    return {
        "current": asdict(current),
        "stream_metadata_size": len(list_stream_metadata()),
    }

