from typing import List, Tuple, Type, Union

from src.plugin_system import (
    BaseAction,
    BaseCommand,
    BaseEventHandler,
    BasePlugin,
    BaseTool,
    ComponentInfo,
    ConfigField,
    register_plugin,
)

from .components import ModelRouterCommand, ModelRouterStartHandler, ModelRouterStopHandler

_PLUGIN_INSTANCE = None


def get_plugin_instance():
    return _PLUGIN_INSTANCE


@register_plugin
class AModelRouterPlugin(BasePlugin):
    plugin_name = "A_model_router"
    enable_plugin = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name = "config.toml"
    config_section_descriptions: dict = {
        "plugin": "插件基础配置",
        "router": "模型路由配置",
    }
    config_schema: dict = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="1.1.0", description="配置文件版本"),
        },
        "router": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用模型路由"),
            "log_decision": ConfigField(type=bool, default=True, description="是否打印路由决策日志"),
            "strict_mode": ConfigField(type=bool, default=False, description="严格模式（选择器异常时是否整组回退）"),
            "ordered_strategy_name": ConfigField(
                type=str,
                default="ordered",
                description="顺序回退策略名（命中路由后写入 selection_strategy）",
            ),
            "defaults_rules": ConfigField(
                type=list,
                default=[
                    {"function_tag": "planner", "selectors": "planner"},
                    {"function_tag": "replyer", "selectors": "replyer"},
                    {"function_tag": "tool_use", "selectors": "tool_use"},
                    {"function_tag": "utils", "selectors": "utils"},
                    {"function_tag": "vision", "selectors": "vlm"},
                ],
                description="默认功能组规则（可逐条新增；selectors 支持逗号/分号/换行分隔）",
                item_type="object",
                item_fields={
                    "function_tag": {
                        "type": "string",
                        "description": "功能组（planner/replyer/tool_use/utils/vision）",
                    },
                    "selectors": {
                        "type": "string",
                        "description": "选择器列表（task 或 task:model，多个可用逗号/分号/换行分隔）",
                    },
                },
            ),
            "group_rules": ConfigField(
                type=list,
                default=[
                    {
                        "chat_key": "qq:123456789:group",
                        "planner": "planner, utils",
                        "replyer": "replyer",
                        "tool_use": "tool_use, utils",
                        "utils": "utils",
                        "vision": "vlm",
                    },
                    {
                        "chat_key": "*",
                        "planner": "planner",
                        "replyer": "replyer",
                        "tool_use": "tool_use",
                        "utils": "utils",
                        "vision": "vlm",
                    },
                ],
                description=(
                    "群聊/私聊路由规则（每条为一个 chat_key，内含五个功能映射；"
                    "各映射支持逗号/分号/换行分隔多个选择器）"
                ),
                item_type="object",
                item_fields={
                    "chat_key": {
                        "type": "string",
                        "description": "聊天键（platform:id:type）或 *",
                    },
                    "planner": {
                        "type": "string",
                        "description": "planner 选择器列表（task 或 task:model）",
                    },
                    "replyer": {
                        "type": "string",
                        "description": "replyer 选择器列表（task 或 task:model）",
                    },
                    "tool_use": {
                        "type": "string",
                        "description": "tool_use 选择器列表（task 或 task:model）",
                    },
                    "utils": {
                        "type": "string",
                        "description": "utils 选择器列表（task 或 task:model）",
                    },
                    "vision": {
                        "type": "string",
                        "description": "vision 选择器列表（task 或 task:model）",
                    },
                },
            ),
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        global _PLUGIN_INSTANCE
        _PLUGIN_INSTANCE = self

    def get_plugin_components(
        self,
    ) -> List[
        Union[
            Tuple[ComponentInfo, Type[BaseAction]],
            Tuple[ComponentInfo, Type[BaseCommand]],
            Tuple[ComponentInfo, Type[BaseTool]],
            Tuple[ComponentInfo, Type[BaseEventHandler]],
        ]
    ]:
        return [
            (ModelRouterStartHandler.get_handler_info(), ModelRouterStartHandler),
            (ModelRouterStopHandler.get_handler_info(), ModelRouterStopHandler),
            (ModelRouterCommand.get_command_info(), ModelRouterCommand),
        ]
