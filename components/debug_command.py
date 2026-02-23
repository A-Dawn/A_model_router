import json
from typing import Optional, Tuple

from src.common.logger import get_logger
from src.plugin_system import BaseCommand

from ..core import runtime_patcher

logger = get_logger("a_model_router.debug_command")


class ModelRouterCommand(BaseCommand):
    command_name = "model_router"
    command_description = "查看模型路由状态与规则"
    command_pattern = r"^/mr(?:\s+.+)?$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        text = str(getattr(self.message, "processed_plain_text", "") or getattr(self.message, "raw_message", "")).strip()
        parts = [part for part in text.split(" ") if part]

        if len(parts) < 2:
            msg = "用法：/mr status | /mr rules | /mr explain <功能组> <platform:id:type|stream_id>；如/mr explain planner qq:123456:reply 或 /mr explain planner 789012"
            await self.send_text(msg)
            return True, msg, 1

        sub_cmd = parts[1].lower()
        if sub_cmd == "status":
            msg = runtime_patcher.format_runtime_status_text()
            await self.send_text(msg)
            return True, msg, 1

        if sub_cmd == "rules":
            rules = runtime_patcher.get_rules_snapshot()
            msg = "A_model_router 路由规则\n" + json.dumps(rules, ensure_ascii=False, indent=2)
            await self.send_text(msg)
            return True, msg, 1

        if sub_cmd == "explain":
            if len(parts) < 4:
                msg = "用法：/mr explain <功能组> <platform:id:type|stream_id>；如/mr explain planner qq:123456:reply 或 /mr explain planner 789012"
                await self.send_text(msg)
                return False, msg, 1
            function_tag = parts[2]
            target = parts[3]
            route_ctx, resolved = runtime_patcher.explain_route(function_tag=function_tag, target=target)
            msg = runtime_patcher.format_explain_text(route_ctx, resolved)
            await self.send_text(msg)
            return True, msg, 1

        msg = f"未知子命令：{sub_cmd}"
        await self.send_text(msg)
        return False, msg, 1
