from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(slots=True)
class RouteContext:
    stream_id: str = ""
    platform: str = ""
    chat_type: str = ""
    group_id: str = ""
    user_id: str = ""
    function_tag: str = ""
    request_type: str = ""


@dataclass(slots=True)
class ResolvedRoute:
    function_tag: str
    chat_key: str
    selectors: List[str] = field(default_factory=list)
    model_list: List[str] = field(default_factory=list)
    source_task: str = ""
    hit_group_key: Optional[str] = None
    fallback_reason: str = ""

