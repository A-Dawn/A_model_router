import copy
import fnmatch
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from src.common.logger import get_logger
from src.config.api_ada_configs import TaskConfig
from src.config.config import model_config

from .context import resolve_context
from .router_types import ResolvedRoute, RouteContext

logger = get_logger("a_model_router.rule_engine")

SUPPORTED_FUNCTION_TAGS = {"planner", "replyer", "tool_use", "utils", "vision"}

FUNCTION_TO_SOURCE_TASK = {
    "planner": "planner",
    "replyer": "replyer",
    "tool_use": "tool_use",
    "utils": "utils",
    "vision": "vlm",
}

_TASK_ALIAS = {
    "vision": "vlm",
}

_router_config: Dict = {}
_SELECTOR_SPLIT_REGEX = re.compile(r"[,\n;\uFF0C\uFF1B]+")


@dataclass(slots=True)
class SelectorSpec:
    task: str
    model: str = ""


def configure_router(config: Optional[Dict]) -> None:
    global _router_config
    normalized = copy.deepcopy(config or {})
    router = normalized.get("router")
    if isinstance(router, dict):
        legacy_defaults = router.get("defaults")
        legacy_groups = router.get("groups")
        if legacy_defaults or legacy_groups:
            logger.warning(
                "[A_model_router] 旧格式 router.defaults/router.groups 已移除，请改用 defaults_rules/group_rules"
            )

        router["defaults"] = _convert_defaults_rules(router.get("defaults_rules"))
        router["groups"] = _convert_group_rules(router.get("group_rules"))

    _router_config = normalized


def get_router_snapshot() -> Dict:
    return copy.deepcopy(_router_config)


def _router_section() -> Dict:
    router = _router_config.get("router", {})
    return router if isinstance(router, dict) else {}


def is_router_enabled() -> bool:
    return bool(_router_section().get("enabled", True))


def should_log_decision() -> bool:
    return bool(_router_section().get("log_decision", True))


def strict_mode() -> bool:
    return bool(_router_section().get("strict_mode", False))


def ordered_strategy_name() -> str:
    value = str(_router_section().get("ordered_strategy_name", "ordered")).strip()
    return value or "ordered"


def normalize_function_tag(function_tag: str) -> str:
    tag = str(function_tag or "").strip().lower()
    if tag in {"tool", "tools", "tooluse"}:
        return "tool_use"
    if tag in {"vision", "vlm", "image"}:
        return "vision"
    if tag in {"reply", "replier"}:
        return "replyer"
    if tag in SUPPORTED_FUNCTION_TAGS:
        return tag
    return ""


def infer_function_tag_from_request_type(request_type: str) -> str:
    rt = str(request_type or "").strip().lower()
    if not rt:
        return ""
    if "tool" in rt:
        return "tool_use"
    if "planner" in rt:
        return "planner"
    if "reply" in rt:
        return "replyer"
    if "image" in rt or rt.startswith("vlm"):
        return "vision"
    if "emoji" in rt or rt.startswith("utils"):
        return "utils"
    return ""


def infer_function_tag_from_task_config(task_config: TaskConfig) -> str:
    if task_config is None:
        return ""
    task_cfgs = getattr(model_config, "model_task_config", None)
    if task_cfgs is None:
        return ""
    for function_tag, task_name in FUNCTION_TO_SOURCE_TASK.items():
        target = getattr(task_cfgs, task_name, None)
        if task_config is target:
            return function_tag
    # 兜底：对拷贝出的 TaskConfig 通过 model_list 对比推断任务类型
    for function_tag, task_name in FUNCTION_TO_SOURCE_TASK.items():
        target = getattr(task_cfgs, task_name, None)
        if not target:
            continue
        try:
            if list(task_config.model_list) == list(target.model_list):
                return function_tag
        except Exception:
            continue
    return ""


def _normalize_task_name(task_name: str) -> str:
    task = str(task_name or "").strip().lower()
    return _TASK_ALIAS.get(task, task)


def parse_selector(selector: str) -> SelectorSpec:
    raw = str(selector or "").strip()
    if not raw:
        raise ValueError("选择器为空")
    if ":" in raw:
        task_raw, model_raw = raw.split(":", 1)
        task = _normalize_task_name(task_raw)
        model = str(model_raw or "").strip()
        if not task:
            raise ValueError(f"选择器 '{raw}' 的任务名为空")
        if not model:
            raise ValueError(f"选择器 '{raw}' 的模型名为空")
        return SelectorSpec(task=task, model=model)
    return SelectorSpec(task=_normalize_task_name(raw), model="")


def _get_task_config(task_name: str) -> Optional[TaskConfig]:
    task_cfgs = getattr(model_config, "model_task_config", None)
    if not task_cfgs:
        return None
    return getattr(task_cfgs, task_name, None)


def _model_exists(model_name: str) -> bool:
    try:
        model_config.get_model_info(model_name)
        return True
    except Exception:
        return False


def _dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    deduped: List[str] = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def expand_selectors(selectors: List[str], strict_mode: bool = False) -> Tuple[List[str], List[str], bool]:
    expanded: List[str] = []
    errors: List[str] = []
    for selector in selectors:
        try:
            parsed = parse_selector(selector)
            task_cfg = _get_task_config(parsed.task)
            if task_cfg is None:
                raise ValueError(f"任务 '{parsed.task}' 不存在")
            if parsed.model:
                if not _model_exists(parsed.model):
                    raise ValueError(f"模型 '{parsed.model}' 不存在")
                expanded.append(parsed.model)
            else:
                model_list = list(getattr(task_cfg, "model_list", []) or [])
                if not model_list:
                    raise ValueError(f"任务 '{parsed.task}' 的 model_list 为空")
                expanded.extend(model_list)
        except Exception as exc:
            errors.append(f"{selector}: {exc}")
            if strict_mode:
                return [], errors, True
    return _dedupe_preserve_order(expanded), errors, False


def _normalize_selector_list(raw_value) -> List[str]:
    return _split_and_normalize_selectors(raw_value)


def _split_and_normalize_selectors(raw_value) -> List[str]:
    if isinstance(raw_value, list):
        values = raw_value
    elif raw_value is None:
        values = []
    else:
        values = [raw_value]

    selectors: List[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        for part in _SELECTOR_SPLIT_REGEX.split(text):
            value = str(part or "").strip()
            if value:
                selectors.append(value)
    return selectors


def _convert_defaults_rules(raw_rules) -> Dict[str, List[str]]:
    if not isinstance(raw_rules, list):
        return {}

    defaults: Dict[str, List[str]] = {}
    for idx, row in enumerate(raw_rules):
        if not isinstance(row, dict):
            logger.warning(f"[A_model_router] defaults_rules 第 {idx} 项不是对象，已跳过")
            continue

        function_tag = normalize_function_tag(row.get("function_tag", ""))
        if not function_tag:
            logger.warning(f"[A_model_router] defaults_rules 第 {idx} 项 function_tag 无效，已跳过")
            continue

        selectors = _split_and_normalize_selectors(row.get("selectors"))
        if not selectors:
            logger.warning(f"[A_model_router] defaults_rules 第 {idx} 项 selectors 为空，已跳过")
            continue

        defaults.setdefault(function_tag, []).extend(selectors)

    return {tag: _dedupe_preserve_order(items) for tag, items in defaults.items() if items}


def _convert_group_rules(raw_rules) -> Dict[str, Dict[str, List[str]]]:
    if not isinstance(raw_rules, list):
        return {}

    groups: Dict[str, Dict[str, List[str]]] = {}
    for idx, row in enumerate(raw_rules):
        if not isinstance(row, dict):
            logger.warning(f"[A_model_router] group_rules 第 {idx} 项不是对象，已跳过")
            continue

        chat_key = str(row.get("chat_key", "") or "").strip()
        if not chat_key:
            logger.warning(f"[A_model_router] group_rules 第 {idx} 项 chat_key 为空，已跳过")
            continue

        group_rules = groups.setdefault(chat_key, {})
        added_any = False
        for field_name in sorted(SUPPORTED_FUNCTION_TAGS):
            selectors = _split_and_normalize_selectors(row.get(field_name))
            if not selectors:
                continue
            group_rules.setdefault(field_name, []).extend(selectors)
            added_any = True

        if not added_any:
            logger.warning(
                f"[A_model_router] group_rules 第 {idx} 项未提供有效的功能映射，已跳过（需提供五个功能字段中的至少一个）"
            )
            continue

    normalized_groups: Dict[str, Dict[str, List[str]]] = {}
    for chat_key, function_map in groups.items():
        normalized_function_map: Dict[str, List[str]] = {}
        for function_tag, selectors in function_map.items():
            deduped = _dedupe_preserve_order(selectors)
            if deduped:
                normalized_function_map[function_tag] = deduped
        if normalized_function_map:
            normalized_groups[chat_key] = normalized_function_map

    return normalized_groups


def build_chat_key(ctx: RouteContext) -> str:
    platform = str(ctx.platform or "").strip()
    chat_type = str(ctx.chat_type or "").strip().lower()
    group_id = str(ctx.group_id or "").strip()
    user_id = str(ctx.user_id or "").strip()

    if not chat_type:
        chat_type = "group" if group_id else ("private" if user_id else "")
    if chat_type == "group":
        target_id = group_id
    elif chat_type in {"private", "friend", "user"}:
        target_id = user_id
        chat_type = "private"
    else:
        target_id = group_id or user_id
    if not platform or not target_id or not chat_type:
        return ""
    return f"{platform}:{target_id}:{chat_type}"


def _match_group_key(chat_key: str, groups: Dict) -> Optional[str]:
    if not chat_key:
        return None
    if chat_key in groups:
        return chat_key
    wildcard_keys = [key for key in groups.keys() if "*" in key and key != "*"]
    wildcard_keys.sort(key=lambda key: len(key.replace("*", "")), reverse=True)
    for key in wildcard_keys:
        if fnmatch.fnmatchcase(chat_key, key):
            return key
    if "*" in groups:
        return "*"
    return None


def _empty_resolved(ctx: RouteContext, function_tag: str, reason: str) -> ResolvedRoute:
    return ResolvedRoute(
        function_tag=function_tag,
        chat_key=build_chat_key(ctx),
        selectors=[],
        model_list=[],
        source_task=FUNCTION_TO_SOURCE_TASK.get(function_tag, ""),
        hit_group_key=None,
        fallback_reason=reason,
    )


def resolve_for_context(ctx: RouteContext) -> ResolvedRoute:
    resolved_ctx = resolve_context(ctx)
    function_tag = normalize_function_tag(resolved_ctx.function_tag)
    if not function_tag:
        function_tag = infer_function_tag_from_request_type(resolved_ctx.request_type)
    if not function_tag:
        return _empty_resolved(resolved_ctx, "", "missing_function_tag")

    router = _router_section()
    defaults = router.get("defaults", {})
    groups = router.get("groups", {})
    defaults = defaults if isinstance(defaults, dict) else {}
    groups = groups if isinstance(groups, dict) else {}

    chat_key = build_chat_key(resolved_ctx)
    fallback_reason_parts: List[str] = []

    group_key = _match_group_key(chat_key, groups) if chat_key else None
    group_selectors: List[str] = []
    using_group = False
    if group_key is not None:
        group_rules = groups.get(group_key, {})
        if isinstance(group_rules, dict):
            group_selectors = _normalize_selector_list(group_rules.get(function_tag))
        if group_selectors:
            using_group = True
        else:
            fallback_reason_parts.append(f"group_missing_function:{function_tag}")

    default_selectors = _normalize_selector_list(defaults.get(function_tag))
    selectors = group_selectors or default_selectors
    hit_group_key = group_key if using_group else ("defaults" if default_selectors else None)

    if not selectors:
        fallback_reason_parts.append("no_selectors")
        return ResolvedRoute(
            function_tag=function_tag,
            chat_key=chat_key,
            selectors=[],
            model_list=[],
            source_task=FUNCTION_TO_SOURCE_TASK.get(function_tag, ""),
            hit_group_key=hit_group_key,
            fallback_reason=";".join(fallback_reason_parts),
        )

    models, errors, strict_failed = expand_selectors(selectors, strict_mode=strict_mode())

    if errors and not strict_mode():
        logger.warning(f"[A_model_router] 选择器解析告警（{function_tag}）：{errors}")

    if strict_failed and using_group:
        fallback_reason_parts.append("group_strict_failed")
        selectors = default_selectors
        hit_group_key = "defaults" if default_selectors else None
        models, errors, strict_failed = expand_selectors(selectors, strict_mode=strict_mode())

    if strict_failed:
        fallback_reason_parts.append("defaults_strict_failed")

    if not models:
        fallback_reason_parts.append("empty_model_list")

    return ResolvedRoute(
        function_tag=function_tag,
        chat_key=chat_key,
        selectors=selectors,
        model_list=models,
        source_task=FUNCTION_TO_SOURCE_TASK.get(function_tag, ""),
        hit_group_key=hit_group_key,
        fallback_reason=";".join([part for part in fallback_reason_parts if part]),
    )


def build_task_config(base_task: TaskConfig, resolved: ResolvedRoute) -> TaskConfig:
    if not resolved.model_list or not resolved.hit_group_key:
        return base_task
    return TaskConfig(
        model_list=list(resolved.model_list),
        max_tokens=base_task.max_tokens,
        temperature=base_task.temperature,
        slow_threshold=base_task.slow_threshold,
        selection_strategy=ordered_strategy_name(),
    )
