from typing import Optional, Tuple

from src.common.logger import get_logger
from src.plugin_system import BaseEventHandler, CustomEventHandlerResult, EventType, MaiMessages

from ..core import runtime_patcher

logger = get_logger("a_model_router.start_handler")


class ModelRouterStartHandler(BaseEventHandler):
    event_type = EventType.ON_START
    handler_name = "a_model_router_start_handler"
    handler_description = "启动时安装 A_model_router 运行时补丁"

    async def execute(
        self, message: Optional[MaiMessages]
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[MaiMessages]]:
        from src.plugin_system.core.plugin_manager import plugin_manager

        plugin = plugin_manager.get_plugin_instance(self.plugin_name)
        if plugin is None:
            logger.warning("[A_model_router] ON_START 未找到插件实例")
            return True, True, "未找到 A_model_router 插件实例", None, None

        runtime_patcher.configure_router(getattr(plugin, "config", {}) or {})
        if not runtime_patcher.is_installed():
            runtime_patcher.install_patches()
        return True, True, "A_model_router 补丁安装完成", None, None
