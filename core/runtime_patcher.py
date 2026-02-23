import functools
from dataclasses import asdict
from typing import Any, Callable, Dict, Optional, Set, Tuple

from src.common.logger import get_logger
from src.config.api_ada_configs import TaskConfig
from src.config.config import model_config
from src.llm_models.model_client.base_client import client_registry

from .context import (
    bind_stream_metadata,
    clear_all_context,
    clear_stream_metadata,
    get_stream_metadata,
    list_stream_metadata,
    pop_route_context,
    push_route_context,
    resolve_context,
)
from .router_types import ResolvedRoute, RouteContext
from . import rule_engine

logger = get_logger("a_model_router.runtime")

_PATCH_INSTALLED = False
_ORIGINALS: Dict[Tuple[object, str], Callable[..., Any]] = {}


def configure_router(config: Optional[Dict]) -> None:
    rule_engine.configure_router(config or {})


def _remember_and_patch(target_obj: object, attr_name: str, patched: Callable[..., Any]) -> None:
    key = (target_obj, attr_name)
    if key not in _ORIGINALS:
        _ORIGINALS[key] = getattr(target_obj, attr_name)
    setattr(target_obj, attr_name, patched)


def _restore_all() -> None:
    for (target_obj, attr_name), original in list(_ORIGINALS.items())[::-1]:
        setattr(target_obj, attr_name, original)
    _ORIGINALS.clear()


def _empty_resolved(route_ctx: RouteContext, reason: str) -> ResolvedRoute:
    return ResolvedRoute(
        function_tag=route_ctx.function_tag,
        chat_key=rule_engine.build_chat_key(route_ctx),
        selectors=[],
        model_list=[],
        source_task=rule_engine.FUNCTION_TO_SOURCE_TASK.get(route_ctx.function_tag, ""),
        hit_group_key=None,
        fallback_reason=reason,
    )


def _route_signature(route_ctx: RouteContext) -> Tuple[str, str, str, str, str, str, str]:
    return (
        route_ctx.stream_id,
        route_ctx.platform,
        route_ctx.chat_type,
        route_ctx.group_id,
        route_ctx.user_id,
        route_ctx.function_tag,
        route_ctx.request_type,
    )


def _task_signature(task: TaskConfig) -> Tuple[Tuple[str, ...], int, float, float, str]:
    return (
        tuple(task.model_list),
        task.max_tokens,
        task.temperature,
        task.slow_threshold,
        task.selection_strategy,
    )


def _sync_model_usage(request_obj: Any) -> None:
    current_usage = dict(getattr(request_obj, "model_usage", {}) or {})
    model_list = list(getattr(request_obj.model_for_task, "model_list", []) or [])
    request_obj.model_usage = {model_name: current_usage.get(model_name, (0, 0, 0)) for model_name in model_list}


def _resolve_route_for_request(base_task: TaskConfig, request_type: str) -> Tuple[RouteContext, ResolvedRoute]:
    current_ctx = resolve_context()
    function_tag = rule_engine.normalize_function_tag(current_ctx.function_tag)
    if not function_tag:
        function_tag = rule_engine.infer_function_tag_from_request_type(request_type or current_ctx.request_type)
    if not function_tag:
        function_tag = rule_engine.infer_function_tag_from_task_config(base_task)

    route_ctx = RouteContext(
        stream_id=current_ctx.stream_id,
        platform=current_ctx.platform,
        chat_type=current_ctx.chat_type,
        group_id=current_ctx.group_id,
        user_id=current_ctx.user_id,
        function_tag=function_tag,
        request_type=str(request_type or current_ctx.request_type or ""),
    )

    if not rule_engine.is_router_enabled():
        return route_ctx, _empty_resolved(route_ctx, "router_disabled")
    if not function_tag:
        return route_ctx, _empty_resolved(route_ctx, "missing_function_tag")
    return route_ctx, rule_engine.resolve_for_context(route_ctx)


def _log_route_decision(route_ctx: RouteContext, resolved: ResolvedRoute, source: str) -> None:
    if not rule_engine.should_log_decision():
        return
    if not resolved.model_list:
        logger.info(
            "[A_model_router] 路由跳过 source=%s function=%s chat=%s reason=%s",
            source,
            resolved.function_tag,
            resolved.chat_key or "<未知>",
            resolved.fallback_reason or "empty_model_list",
        )
        return
    logger.info(
        "[A_model_router] 路由决策 source=%s function=%s chat=%s hit=%s selectors=%s models=%s fallback=%s",
        source,
        resolved.function_tag,
        resolved.chat_key or "<未知>",
        resolved.hit_group_key or "<无>",
        resolved.selectors,
        resolved.model_list,
        resolved.fallback_reason or "",
    )


def _apply_dynamic_route_for_request(request_obj: Any) -> None:
    base_task = getattr(request_obj, "_mr_base_model_for_task", request_obj.model_for_task)
    route_ctx, resolved = _resolve_route_for_request(base_task, getattr(request_obj, "request_type", ""))
    signature = _route_signature(route_ctx)
    if signature == getattr(request_obj, "_mr_last_route_signature", None):
        return

    new_task = base_task
    if resolved.model_list and resolved.hit_group_key:
        new_task = rule_engine.build_task_config(base_task, resolved)

    if _task_signature(new_task) != _task_signature(request_obj.model_for_task):
        request_obj.model_for_task = new_task
        _sync_model_usage(request_obj)

    request_obj._mr_last_route_signature = signature
    request_obj._mr_last_route_context = route_ctx
    request_obj._mr_last_resolved_route = resolved

    if resolved.model_list:
        _log_route_decision(route_ctx, resolved, source="_select_model")


def _ordered_select_model(request_obj: Any, exclude_models: Optional[Set[str]]) -> Tuple[Any, Any, Any]:
    _sync_model_usage(request_obj)
    exclude = exclude_models or set()
    for model_name in request_obj.model_for_task.model_list:
        if model_name in exclude:
            continue
        try:
            model_info = model_config.get_model_info(model_name)
            api_provider = model_config.get_provider(model_info.api_provider)
            force_new_client = getattr(request_obj, "request_type", "") == "embedding"
            client = client_registry.get_client_class_instance(api_provider, force_new=force_new_client)
            total_tokens, penalty, usage_penalty = request_obj.model_usage.get(model_info.name, (0, 0, 0))
            request_obj.model_usage[model_info.name] = (total_tokens, penalty, usage_penalty + 1)
            logger.debug(
                "[A_model_router] 顺序策略选中模型 model=%s request_type=%s",
                model_info.name,
                getattr(request_obj, "request_type", ""),
            )
            return model_info, api_provider, client
        except Exception as exc:
            logger.warning(f"[A_model_router] 顺序策略跳过不可用模型 '{model_name}'：{exc}")
            continue
    raise RuntimeError("没有可用的模型可供选择。所有模型均已尝试失败。")


def _bind_stream_metadata_from_stream(stream_id: str) -> None:
    stream_key = str(stream_id or "").strip()
    if not stream_key:
        return
    try:
        from src.chat.message_receive.chat_stream import get_chat_manager

        chat_manager = get_chat_manager()
        if not chat_manager:
            return
        stream = chat_manager.get_stream(stream_key)
        if not stream:
            return
        platform = str(getattr(stream, "platform", "") or "").strip()
        group_info = getattr(stream, "group_info", None)
        user_info = getattr(stream, "user_info", None)
        group_id = str(getattr(group_info, "group_id", "") or "").strip() if group_info else ""
        user_id = str(getattr(user_info, "user_id", "") or "").strip() if user_info else ""
        chat_type = "group" if group_id else "private"
        bind_stream_metadata(
            stream_key,
            platform=platform,
            chat_type=chat_type,
            group_id=group_id,
            user_id=user_id,
        )
    except Exception as exc:
        logger.debug(f"[A_model_router] 从 chat_manager 绑定流元数据失败：{exc}")


def _bind_stream_metadata_from_message_obj(message_obj: Any) -> Tuple[str, str, str, str, str]:
    stream_id = str(getattr(getattr(message_obj, "chat_stream", None), "stream_id", "") or "").strip()
    message_info = getattr(message_obj, "message_info", None)
    platform = str(getattr(message_info, "platform", "") or "").strip()
    group_info = getattr(message_info, "group_info", None)
    user_info = getattr(message_info, "user_info", None)
    group_id = str(getattr(group_info, "group_id", "") or "").strip() if group_info else ""
    user_id = str(getattr(user_info, "user_id", "") or "").strip() if user_info else ""
    chat_type = "group" if group_id else ("private" if user_id else "")
    if stream_id:
        bind_stream_metadata(
            stream_id,
            platform=platform,
            chat_type=chat_type,
            group_id=group_id,
            user_id=user_id,
        )
    return stream_id, platform, chat_type, group_id, user_id


def _build_llm_api_wrapper(original: Callable[..., Any], request_type_index: int) -> Callable[..., Any]:
    @functools.wraps(original)
    async def wrapped(*args, **kwargs):
        request_type = str(kwargs.get("request_type", "") or "")
        if not request_type and len(args) > request_type_index:
            request_type = str(args[request_type_index] or "")
        model_set = kwargs.get("model_config")
        if model_set is None and len(args) > 1:
            model_set = args[1]

        function_hint = rule_engine.infer_function_tag_from_request_type(request_type)
        if not function_hint and model_set is not None:
            function_hint = rule_engine.infer_function_tag_from_task_config(model_set)

        token = push_route_context(
            function_tag=function_hint or None,
            request_type=request_type or None,
        )
        try:
            return await original(*args, **kwargs)
        finally:
            pop_route_context(token)

    return wrapped


def _build_llm_request_init_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapped(self, model_set: TaskConfig, request_type: str = "") -> None:
        base_task = model_set
        routed_task = base_task
        route_ctx = RouteContext()
        resolved = _empty_resolved(route_ctx, "init_not_routed")
        try:
            route_ctx, resolved = _resolve_route_for_request(base_task, request_type)
            if resolved.model_list and resolved.hit_group_key:
                routed_task = rule_engine.build_task_config(base_task, resolved)
        except Exception as exc:
            logger.warning(f"[A_model_router] 在 LLMRequest.__init__ 中解析路由失败：{exc}")
        original(self, routed_task, request_type)
        self._mr_base_model_for_task = base_task
        self._mr_last_route_signature = _route_signature(route_ctx)
        self._mr_last_route_context = route_ctx
        self._mr_last_resolved_route = resolved
        _sync_model_usage(self)
        if resolved.model_list:
            _log_route_decision(route_ctx, resolved, source="__init__")

    return wrapped


def _build_llm_request_select_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapped(self, exclude_models: Optional[Set[str]] = None):
        _apply_dynamic_route_for_request(self)
        strategy = str(getattr(self.model_for_task, "selection_strategy", "")).strip().lower()
        ordered_names = {
            "ordered",
            rule_engine.ordered_strategy_name().strip().lower(),
        }
        if strategy in ordered_names:
            return _ordered_select_model(self, exclude_models)
        return original(self, exclude_models=exclude_models)

    return wrapped


def install_patches() -> bool:
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return True

    try:
        from src.chat.brain_chat.brain_planner import BrainPlanner
        from src.chat.message_receive.message import MessageRecv
        from src.chat.planner_actions.planner import ActionPlanner
        from src.chat.replyer.group_generator import DefaultReplyer
        from src.chat.replyer.private_generator import PrivateReplyer
        from src.llm_models.utils_model import LLMRequest
        from src.plugin_system.apis import llm_api
    except Exception as exc:
        logger.error(f"[A_model_router] 安装补丁时导入依赖失败：{exc}")
        _restore_all()
        return False

    planner_plan_original = ActionPlanner.plan
    brain_plan_original = BrainPlanner.plan
    group_reply_original = DefaultReplyer.generate_reply_with_context
    private_reply_original = PrivateReplyer.generate_reply_with_context
    message_process_original = MessageRecv.process

    @functools.wraps(planner_plan_original)
    async def planner_plan_wrapped(self, *args, **kwargs):
        stream_id = str(getattr(self, "chat_id", "") or "").strip()
        _bind_stream_metadata_from_stream(stream_id)
        token = push_route_context(stream_id=stream_id or None, function_tag="planner", request_type="planner")
        try:
            return await planner_plan_original(self, *args, **kwargs)
        finally:
            pop_route_context(token)

    @functools.wraps(brain_plan_original)
    async def brain_plan_wrapped(self, *args, **kwargs):
        stream_id = str(getattr(self, "chat_id", "") or "").strip()
        _bind_stream_metadata_from_stream(stream_id)
        token = push_route_context(stream_id=stream_id or None, function_tag="planner", request_type="planner")
        try:
            return await brain_plan_original(self, *args, **kwargs)
        finally:
            pop_route_context(token)

    @functools.wraps(group_reply_original)
    async def group_reply_wrapped(self, *args, **kwargs):
        stream_id = str(kwargs.get("stream_id", "") or "").strip()
        if not stream_id:
            stream_id = str(getattr(getattr(self, "chat_stream", None), "stream_id", "") or "").strip()
        _bind_stream_metadata_from_stream(stream_id)
        request_type = str(getattr(getattr(self, "express_model", None), "request_type", "replyer") or "replyer")
        token = push_route_context(stream_id=stream_id or None, function_tag="replyer", request_type=request_type)
        try:
            return await group_reply_original(self, *args, **kwargs)
        finally:
            pop_route_context(token)

    @functools.wraps(private_reply_original)
    async def private_reply_wrapped(self, *args, **kwargs):
        stream_id = str(kwargs.get("stream_id", "") or "").strip()
        if not stream_id:
            stream_id = str(getattr(getattr(self, "chat_stream", None), "stream_id", "") or "").strip()
        _bind_stream_metadata_from_stream(stream_id)
        request_type = str(getattr(getattr(self, "express_model", None), "request_type", "replyer") or "replyer")
        token = push_route_context(stream_id=stream_id or None, function_tag="replyer", request_type=request_type)
        try:
            return await private_reply_original(self, *args, **kwargs)
        finally:
            pop_route_context(token)

    @functools.wraps(message_process_original)
    async def message_process_wrapped(self, *args, **kwargs):
        stream_id, platform, chat_type, group_id, user_id = _bind_stream_metadata_from_message_obj(self)
        token = push_route_context(
            stream_id=stream_id or None,
            platform=platform or None,
            chat_type=chat_type or None,
            group_id=group_id or None,
            user_id=user_id or None,
            request_type="message.process",
        )
        try:
            return await message_process_original(self, *args, **kwargs)
        finally:
            pop_route_context(token)

    _remember_and_patch(ActionPlanner, "plan", planner_plan_wrapped)
    _remember_and_patch(BrainPlanner, "plan", brain_plan_wrapped)
    _remember_and_patch(DefaultReplyer, "generate_reply_with_context", group_reply_wrapped)
    _remember_and_patch(PrivateReplyer, "generate_reply_with_context", private_reply_wrapped)
    _remember_and_patch(MessageRecv, "process", message_process_wrapped)

    _remember_and_patch(llm_api, "generate_with_model", _build_llm_api_wrapper(llm_api.generate_with_model, 2))
    _remember_and_patch(
        llm_api,
        "generate_with_model_with_tools",
        _build_llm_api_wrapper(llm_api.generate_with_model_with_tools, 3),
    )
    _remember_and_patch(
        llm_api,
        "generate_with_model_with_tools_by_message_factory",
        _build_llm_api_wrapper(llm_api.generate_with_model_with_tools_by_message_factory, 3),
    )

    _remember_and_patch(LLMRequest, "__init__", _build_llm_request_init_wrapper(LLMRequest.__init__))
    _remember_and_patch(LLMRequest, "_select_model", _build_llm_request_select_wrapper(LLMRequest._select_model))

    _PATCH_INSTALLED = True
    logger.info("[A_model_router] 运行时补丁安装完成")
    return True


def uninstall_patches() -> bool:
    global _PATCH_INSTALLED
    _restore_all()
    _PATCH_INSTALLED = False
    clear_all_context()
    clear_stream_metadata()
    logger.info("[A_model_router] 运行时补丁卸载完成")
    return True


def is_installed() -> bool:
    return _PATCH_INSTALLED


def get_runtime_status() -> Dict[str, Any]:
    router_snapshot = rule_engine.get_router_snapshot().get("router", {})
    defaults = router_snapshot.get("defaults", {}) if isinstance(router_snapshot, dict) else {}
    groups = router_snapshot.get("groups", {}) if isinstance(router_snapshot, dict) else {}
    return {
        "installed": _PATCH_INSTALLED,
        "router_enabled": rule_engine.is_router_enabled(),
        "strict_mode": rule_engine.strict_mode(),
        "log_decision": rule_engine.should_log_decision(),
        "ordered_strategy_name": rule_engine.ordered_strategy_name(),
        "defaults_keys": sorted(list(defaults.keys())) if isinstance(defaults, dict) else [],
        "groups_count": len(groups) if isinstance(groups, dict) else 0,
        "stream_metadata_size": len(list_stream_metadata()),
    }


def get_rules_snapshot() -> Dict[str, Any]:
    router = rule_engine.get_router_snapshot().get("router", {})
    return router if isinstance(router, dict) else {}


def _build_context_from_target(function_tag: str, target: str) -> RouteContext:
    tag = rule_engine.normalize_function_tag(function_tag)
    raw_target = str(target or "").strip()
    if not raw_target:
        return resolve_context(RouteContext(function_tag=tag))

    parts = raw_target.split(":")
    if len(parts) >= 3:
        platform = parts[0].strip()
        target_id = parts[1].strip()
        chat_type = parts[2].strip().lower()
        group_id = target_id if chat_type == "group" else ""
        user_id = target_id if chat_type != "group" else ""
        stream_id = ""
        try:
            from src.chat.message_receive.chat_stream import get_chat_manager

            chat_manager = get_chat_manager()
            if chat_manager and platform and target_id:
                stream_id = chat_manager.get_stream_id(platform, target_id, is_group=(chat_type == "group"))
        except Exception:
            stream_id = ""
        if stream_id:
            bind_stream_metadata(
                stream_id,
                platform=platform,
                chat_type=chat_type,
                group_id=group_id,
                user_id=user_id,
            )
        return resolve_context(
            RouteContext(
                stream_id=stream_id,
                platform=platform,
                chat_type=chat_type,
                group_id=group_id,
                user_id=user_id,
                function_tag=tag,
            )
        )

    stream_id = raw_target
    _bind_stream_metadata_from_stream(stream_id)
    meta = get_stream_metadata(stream_id)
    return resolve_context(
        RouteContext(
            stream_id=stream_id,
            platform=str(meta.get("platform", "")),
            chat_type=str(meta.get("chat_type", "")),
            group_id=str(meta.get("group_id", "")),
            user_id=str(meta.get("user_id", "")),
            function_tag=tag,
        )
    )


def explain_route(function_tag: str, target: str) -> Tuple[RouteContext, ResolvedRoute]:
    route_ctx = _build_context_from_target(function_tag, target)
    if not route_ctx.function_tag:
        return route_ctx, _empty_resolved(route_ctx, "invalid_function_tag")
    resolved = rule_engine.resolve_for_context(route_ctx)
    return route_ctx, resolved


def format_runtime_status_text() -> str:
    status = get_runtime_status()
    return (
        "A_model_router 状态\n"
        f"- 补丁已安装: {status['installed']}\n"
        f"- 路由开关: {status['router_enabled']}\n"
        f"- 严格模式: {status['strict_mode']}\n"
        f"- 决策日志: {status['log_decision']}\n"
        f"- 顺序策略名: {status['ordered_strategy_name']}\n"
        f"- 默认功能组: {status['defaults_keys']}\n"
        f"- 群组规则数量: {status['groups_count']}\n"
        f"- 已缓存流元数据数量: {status['stream_metadata_size']}"
    )


def format_explain_text(route_ctx: RouteContext, resolved: ResolvedRoute) -> str:
    ctx_data = asdict(route_ctx)
    return (
        "A_model_router 路由解释\n"
        f"- 上下文: {ctx_data}\n"
        f"- 聊天键(chat_key): {resolved.chat_key or '<未知>'}\n"
        f"- 功能组(function_tag): {resolved.function_tag or '<未知>'}\n"
        f"- 命中规则(hit_group_key): {resolved.hit_group_key or '<无>'}\n"
        f"- 选择器(selectors): {resolved.selectors}\n"
        f"- 模型列表(model_list): {resolved.model_list}\n"
        f"- 回退原因代码(fallback_reason): {resolved.fallback_reason or '<无>'}"
    )
