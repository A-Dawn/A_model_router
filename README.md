# A_model_router

> `A_model_router` ，一款基于运行时补丁（monkey patch）的模型路由插件，用于按聊天来源（群/私聊）和功能分组（`planner/replyer/tool_use/utils/vision`）精细控制模型候选链。


## 功能概览

- 按 `chat_key` 分组配置路由规则（支持通配 `*`）
- 每个分组固定 5 个功能映射：
  - `planner`
  - `replyer`
  - `tool_use`
  - `utils`
  - `vision`
- 每个映射支持多个 selector（逗号/分号/换行分隔）
- selector 支持以下两种填写方式：
  - `task`
  - `task:model`
- 命中后按列表顺序回退（`ordered`），并保序去重
- 目前有以下调试命令：
  - `/mr status`
  - `/mr rules`
  - `/mr explain <功能组> <platform:id:type|stream_id>`

## 版本与兼容

- 插件版本：`0.1.0`（见 `_manifest.json`）
- 配置版本：`1.1.0`（见 `config.toml`）
- 宿主最低版本：`0.12.1`

## 配置结构

配置文件：`plugins/A_model_router/config.toml`

```toml
[plugin]
enabled = true
config_version = "1.1.0"

[router]
enabled = true
log_decision = true
strict_mode = false
ordered_strategy_name = "ordered"

[[router.defaults_rules]]
function_tag = "planner"
selectors = "planner"

[[router.defaults_rules]]
function_tag = "replyer"
selectors = "replyer"

[[router.defaults_rules]]
function_tag = "tool_use"
selectors = "tool_use"

[[router.defaults_rules]]
function_tag = "utils"
selectors = "utils"

[[router.defaults_rules]]
function_tag = "vision"
selectors = "vlm"

[[router.group_rules]]
chat_key = "qq:123456789:group"
planner = "planner:modelA, planner:modelB, utils:modelC"
replyer = "replyer:modelR1"
tool_use = "tool_use:modelT1, utils:modelU1"
utils = "utils:modelU2"
vision = "vlm:modelV1, vlm:modelV2"

[[router.group_rules]]
chat_key = "*"
planner = "planner"
replyer = "replyer"
tool_use = "tool_use"
utils = "utils"
vision = "vlm"
```

## 字段说明

### `[router]`

- `enabled`: 路由总开关
- `log_decision`: 是否打印路由决策日志
- `strict_mode`: 严格模式
  - `false`: 非法 selector 跳过并告警
  - `true`: 当前规则组解析失败时回退
- `ordered_strategy_name`: 命中路由后写入 `selection_strategy` 的策略名（默认 `ordered`）

### `[[router.defaults_rules]]`

- `function_tag`: 必须是 `planner/replyer/tool_use/utils/vision`
- `selectors`: selector 列表字符串（支持逗号/分号/换行分隔）

### `[[router.group_rules]]`

- `chat_key`: `platform:id:type`，例如：
  - 群聊：`qq:2584059816:group`
  - 私聊：`qq:114514:private`
- 五个功能映射字段：
  - `planner`
  - `replyer`
  - `tool_use`
  - `utils`
  - `vision`

每个映射字段的值都是 selector 列表字符串。

## Selector 语义

仅支持以下两种：

1. `task`
   - 展开对应任务当前 `model_list`（保持顺序）
2. `task:model`
   - 直接引用指定模型名

不支持“纯模型名”。

### 示例

- `planner`：使用任务 `planner` 的模型列表
- `planner:gpt_4o`：强制引用模型 `gpt_4o`
- `planner, planner:gpt_4o_mini, utils`：顺序候选链

## 路由行为

1. 优先匹配 `group_rules` 的 `chat_key`
2. 分组中对应功能缺失时回退到 `defaults_rules`
3. 解析后进行保序去重
4. 命中路由时强制使用 `ordered` 顺序策略
5. 若全部不可用则按原链路失败处理

## 调试命令

- `/mr status`：查看补丁与路由状态
- `/mr rules`：查看当前生效规则快照
- `/mr explain <功能组> <platform:id:type|stream_id>`
  - 示例：`/mr explain planner qq:2584059816:group`
  - 示例：`/mr explain replyer <stream_id>`

## 生效链路（运行时补丁）

插件会包装以下入口并注入上下文：

- `ActionPlanner.plan`
- `BrainPlanner.plan`
- `DefaultReplyer.generate_reply_with_context`
- `PrivateReplyer.generate_reply_with_context`
- `MessageRecv.process`
- `llm_api.generate_with_model*`
- `LLMRequest.__init__`
- `LLMRequest._select_model`

## 使用建议

1. 先保证任务模型本身可用（`planner/replyer/tool_use/utils/vlm`）
2. 再逐步在 `group_rules` 中加 `task:model` 精细引用
3. 建议保留 `chat_key="*"` 兜底规则
4. 修改配置后重载插件或重启进程

## 测试

已包含单元/集成测试，含模拟聊天流：

- `tests/plugins/a_model_router/test_selector_parser.py`
- `tests/plugins/a_model_router/test_group_function_mapping.py`
- `tests/plugins/a_model_router/test_ordered_strategy.py`
- `tests/plugins/a_model_router/test_runtime_patch_lifecycle.py`
- `tests/plugins/a_model_router/test_chain_smoke.py`
- `tests/plugins/a_model_router/test_simulated_chat_flow.py`

运行：

```powershell
pytest -q tests/plugins/a_model_router
```

## 常见问题

1. WebUI 中配置项显示异常或不可编辑
   - 确认插件已启用并成功加载
   - 重载插件/重启后重新打开配置页
2. 路由未命中
   - 检查 `chat_key` 是否正确（`platform:id:type`）
   - 用 `/mr explain` 看实际命中与回退原因
3. `task:model` 无效
   - 模型名必须存在于全局模型注册中
   - 建议先用纯 `task` 验证链路，再加具体模型
