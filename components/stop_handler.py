from typing import Optional, Tuple

from src.common.logger import get_logger
from src.plugin_system import BaseEventHandler, CustomEventHandlerResult, EventType, MaiMessages

from ..core import runtime_patcher

logger = get_logger("a_model_router.stop_handler")


class ModelRouterStopHandler(BaseEventHandler):
    event_type = EventType.ON_STOP
    handler_name = "a_model_router_stop_handler"
    handler_description = "停止时卸载 A_model_router 运行时补丁"

    async def execute(
        self, message: Optional[MaiMessages]
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[MaiMessages]]:
        try:
            runtime_patcher.uninstall_patches()
            return True, True, "A_model_router 补丁卸载完成", None, None
        except Exception as exc:
            logger.error(f"[A_model_router] ON_STOP 执行失败: {exc}", exc_info=True)
            return False, True, f"A_model_router ON_STOP 执行失败: {exc}", None, None
