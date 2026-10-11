# trae-agent 开发设计

## 1. 目标与责任

开发一个真实编码 Agent，接收 `prompt` 与初始文件，默认调用 MiMo-V2.6-Flash、自主选择工具、实际读取/修改文件和执行验证命令，再返回真实结果及文件产物。Eyes 负责调度、独立评分和证据保存；Agent 不读取预期答案、评分配置或 Eyes 数据库。

应能处理现有编码冒烟任务：修复 Python 文件、实现函数、创建 CLI。它必须是通用模型/工具循环，不能根据 `fix-mean`、`stable-unique` 或 `integer-cli` 等标识直接返回模板答案。复用协议外壳不影响作为第二个 Agent；复用 MewCode 的执行循环则不满足独立 Agent 目标。

## 2. 技术路线

Windows/PowerShell 开发，Python >=3.14；运行在 Docker Desktop Linux 容器。优先复用 Eyes 的 `eyes.agent_service.http.create_app` 提供协议持久化外壳，自己实现输入校验、模型循环、工具、隔离、截止/停止处理和产物采集。

参考服务依赖 POSIX 文件锁，不承诺原生 Windows 运行。每个存储文件只允许一个 HTTP 服务进程，Uvicorn workers=1；协议状态使用独立 SQLite，不连接 Eyes PostgreSQL。不要重写现有 HTTP 去重/快照层或引入消息中间件。

建议目录是后续实施方案，下面文件目前尚不存在：

```text
src/trae_agent/
  config.py             模型、额度、路径及超时配置
  model_client.py       MiMo 调用与响应/工具调用规范化
  loop.py               有界任务循环
  tools.py              文件与受限进程工具
  workspace.py          任务目录、输入准备、清理和产物
  runtime.py            活动任务、停止令牌及工具进程管理
  http_service.py       输入 schema、能力及 create_app 回调
deploy/
  Dockerfile            锁定环境、非 root 服务身份
  compose.yaml          独立服务/状态卷及显式端口
docs/                   实施和实际验证记录
pyproject.toml
uv.lock
```

独立锁定新项目依赖，固定 Eyes 依赖版本/源码基线。不要让安装新模型 SDK 覆盖 Eyes 已冻结依赖；组合环境必须做依赖一致性检查。别把未来计划目录宣称为已实现。

## 3. 开发模型与 MiMo 运行配置

ZCode 的开发模型保持 GLM-5.3-Flash，新 Agent 的运行模型默认 MiMo-V2.6-Flash。小米官方列出的 API ID 为全小写 `mimo-v2.6-flash`，不要使用混合大小写 `mimo-v2.6-Flash` 作为默认请求值；实际账号权限、访问通道仍需运行前核实。模型别名不等于冻结模型权重，记录未知限制。

配置由环境变量或私有配置文件提供，建议名称如下；这是本项目待实现的配置约定，不是已存在的命令：

| 配置 | 用途 |
| --- | --- |
| `AGENT_MODEL_ID` | 默认 `mimo-v2.6-flash`，不静默改为 Pro、GLM 或其他模型 |
| `AGENT_MODEL_BASE_URL` | 配置化；小米官方按量 API 示例为 `https://api.xiaomimimo.com/v1`，其他通道须按实际授权核实 |
| `AGENT_MODEL_API_KEY` | 新 Agent 可合法使用的模型密钥，仅进程内访问 |
| `AGENT_HTTP_TOKEN` | 独立的高熵 Agent 服务 Bearer 凭据，至少 32 字符 |
| `AGENT_STATE_DIR` | 私有协议状态、任务与产物目录 |
| `AGENT_VERSION` | 项目提交号加实际源码/配置摘要 |
| `AGENT_MAX_CONCURRENCY` | 首阶段 1，后续只在隔离已验证时提高 |
| `AGENT_MAX_MODEL_CALLS` | 每任务的模型请求数量上限 |
| `AGENT_MAX_TOOL_CALLS` | 每任务的工具调用数量上限 |
| `AGENT_MAX_OUTPUT_TOKENS` | 单次输出上限，遵循当前供应商真实语义 |
| `AGENT_RUN_BUDGET_RMB` | 本轮总模型预算；无明确值时不得发起付费运行 |

不要把 ZCode/GLM 登录或体验额度视为新 Agent 的 MiMo API 权限，不提取桌面客户端令牌。新 Agent 使用独立 MiMo 凭据；小米按量 API 与 Token Plan 的密钥/通道按实际账号分别核实，不复用 GLM 的 API 地址或密钥。

MiMo 官方模型页列出工具调用能力，其 OpenAI 兼容示例使用 `max_completion_tokens`，并通过 `thinking.type` 配置思考。不要套用 GLM 的“thinking 不能禁用”规则，也不要把 MewCode 的布尔 `thinking=false` 原样发给 MiMo；参数按实际通道官方契约校验。正确保留工具调用所需的消息字段，不把隐藏推理作为 Eyes 任务完成证据。第一版建议非流式调用，先保证工具调用、usage、结束/截断处理正确，再考虑流式优化。

设请求超时、任务墙钟上限、上下文/输出/工具次数限额；截断、429、网络异常和缺 usage 都明确记录。请求次数和 token 上限不自动等于人民币硬上限；需要当前计费依据、请求前预算预留及跨并发共享总账，供应商 usage 不完整时停止付费重试并标记金额无法确认。开发工具费用与运行时费用分别计量。

官方参考（2026-10-11 查阅，实施时再次核实）：

- [MiMo-V2.6-Flash 官方模型及调用示例](https://mimo.mi.com/models/zh-CN/mimo-v2.6-flash)
- [MiMo 官方模型列表](https://mimo.mi.com/docs/en-US/api/model/list-models)
- [ZCode 开发模型配置与通道说明](https://zcode.z.ai/cn/docs/configuration)

## 4. 输入与真实执行循环

业务输入保持与现有数据集兼容：`prompt` 为非空字符串，`files` 为相对逻辑路径到 UTF-8 文本的映射，缺省为空对象。不新增必填业务字段；输入校验不能启动任务或产生副作用。

执行过程：

1. 协议层持久化接受任务及原幂等映射后，创建本任务独立 runtime/workspace。
2. 安全物化初始文件；每个请求使用自己的消息历史、工具注册、事件序列和进程集合。
3. 发送任务和工具定义给模型；模型返回工具调用时实际执行，再将原调用 ID 对应的真实结果加入历史。
4. 重复上述过程直到模型正常完成、截止、停止或限额。未知工具、无效参数和不合法路径返回明确错误，不伪造工具成功。
5. 工具执行结束后收集实际代码文件，发布协议产物并记录摘要，再清理临时执行资源、返回一致的结果。

模型宣布完成仅表示循环结束；文件是否满足任务由 Eyes 的独立评分器判断。模型回复中的建议不等于修改已发生。发生明确模型/API错误时可报告 stopped_confirmed 的 failed，前提是本地 Agent 和工具确实结束；不能确认副作用或工作是否结束时保留 unknown。

最小工具：

| 工具 | 实施要求 |
| --- | --- |
| `list_files` | 只列本任务目录，有数量/深度上限 |
| `read_file` | 有大小/编码限制，返回实际内容 |
| `write_file` | 有字节上限和原子写入，仅在本任务目录 |
| `replace_text` | 校验命中数量，不盲目覆盖 |
| `run_python` | 先支持 argv 方式运行指定任务内 Python 文件或有限验证脚本，返回真实 exit/stdout/stderr |

第一版不需要任意 shell。禁止模型执行器读取服务密钥、协议数据库、其他任务目录及 Eyes 私有数据。清理后的固定协议产物仍可下载。工具子进程环境必须移除模型/Agent/Eyes 凭据；仅限制模型工具路径不是针对任意恶意 Python 的安全沙箱。

## 5. Eyes 统一 HTTP 契约

权威来源是当前 Eyes 仓库的以下文件，实施前必须读取，本文不复制完整 Schema：

- `D:\projects\eyes\docs\agent-protocol.md`
- `D:\projects\eyes\docs\agent-http-runtime.md`
- `D:\projects\eyes\src\eyes\contracts\agent_protocol.py`
- `D:\projects\eyes\docs\agent-protocol-v1.schema.json`
- `D:\projects\eyes\src\eyes\agent_service\http.py`

服务基地址下的接口：

| 类型 | 路径 | 当前协议要求 |
| --- | --- | --- |
| 必需 | `GET /agent/v1/capabilities` | 真实能力及输入 schema |
| 必需 | `POST /agent/v1/runs` | AgentTask；首次 202，同请求重放 200 |
| 必需 | `GET /agent/v1/runs/by-key/{idempotency_key}` | 原键查询，不产生新的业务执行 |
| 必需 | `GET /agent/v1/runs/{run_id}` | 原身份、递增 revision 的快照 |
| 必需 | `GET /agent/v1/runs/{run_id}/result` | 终态/unknown 的 AgentResult；未结束为 202 快照 |
| 首版实现 | `GET /agent/v1/runs/{run_id}/events` | 稳定 ID、运行内序列、可重放 cursor |
| 首版实现 | `GET /agent/v1/runs/{run_id}/artifacts/{artifact_id}` | 本运行真实产物字节 |
| 后续真实支持时 | `POST /agent/v1/runs/{run_id}/cancel` | 请求接受与停止确认分开 |
| 后续真实支持时 | `PUT /agent/v1/resources/{resource_id}` | 大小/摘要校验后才允许任务引用 |

Agent 消息带 `protocol_version: "1.0"`；Eyes 控制 API 使用独立的 `schema_version: "1.0"`。不要把被动 observation 协议、内部 ExecutionEvent 或两种版本字段混入外部消息。

优先用 `create_app(database=..., bearer_token=..., capabilities=..., execute=..., validate_input=..., cancel=...)`。协议外壳承担持久幂等、快照/事件/产物接口；execute(task, context) 自己调用真实 Agent，返回符合契约的 AgentResult。用 context.event、context.artifact、context.resource 持久化本运行事实；返回产物必须来自本运行已发布记录。

初版能力：agent_id=`trae-agent`，max_concurrency=1，独立会话已实现才设 session_isolation=true；environment_isolation 在完成实际隔离前为 false；events/artifacts 实现后为 true，cancellation/resources 未实现时为 false。agent_version 保存实际版本，terminal_retention_seconds 至少 86400。

不支持的可选能力不能返回假成功；取消能力必须与 cancel 回调匹配。当前参考服务会提供能力对应的路由，但资源安全物化、取消停止控制仍需自己实现，不因复用外壳自动通过验收。

同键相同参数必须返回原运行；同键不同参数、同 Attempt 换键必须冲突。服务重启后未结束任务保守 unknown，不重放业务。所有协议读取也要求 Agent Bearer 凭据，不能通过知道 UUID 越权访问。

## 6. 隔离、停止与证据

第二个 Agent 不必一开始单目标并发 2；MewCode 1 槽与 trae-agent 1 槽仍能验证双目标同时执行。新增并发须先验证会话/环境隔离，并检查供应商总额度。

Agent 任务目录与 MewCode、Eyes 状态、其他任务完全分离。模型消息列表、工具状态、取消令牌不使用跨任务可变全局对象。执行任意生成的 Python 时优先放在不挂载服务密钥/协议状态的独立受限任务容器，按最小网络、挂载及资源配置运行。仅使用独立工作目录不能证明 environment_isolation 或安全沙箱，未落实则 max_concurrency 保持 1、能力为 false，并记录可信代码假设。

本项目按需启动。正常停止持久化状态，下一次启动可查询旧结果/下载产物；不得自动重跑 unknown。活动和 unknown 记录不按终态期限删除；清理未完成不应覆盖任务结果，清理状态如实记录。

可选取消：先设置停止信号，禁止新模型/工具调用，终止本任务工具进程并核对退出；关闭模型连接不能证明供应商计算已停止。不能确认任务或相关副作用停止时保持 cancel_requested/unknown，不能伪造 stopped_confirmed。竞争正常成功时保留原成功结果，不覆盖为 cancelled。

事件至少记录任务、模型请求/响应边界、工具调用/结果、异常、产物发布、清理；类型为本项目语义名称并在 data 保留 tool_call_id、来源、时间/耗时与已知 usage，不伪装成 SDK span。输入输出按配置采集、脱敏、截断计数可见；sealed 仅指声明范围完整，采集失败、丢弃或缺失时 partial，未知计数保留 null。span/执行区间必须来自实际执行，不能用任务排队时刻伪造重叠。

## 7. 开发阶段与完成条件

1. **无模型费用准备**：创建项目/锁定环境、读取协议、实现配置/输入/工具边界和 HTTP 持久化外壳。能力发现/认证/去重可检查，但不提供虚假的 execute 返回值。
2. **单任务真实闭环**：预算明确后实际调用 MiMo-V2.6-Flash；生成文件、发布事件/产物，接入 Eyes 并独立评分。失败记录保留，不能硬编码答案。
3. **已有三任务**：原样运行现有测试集和评分器，记录每项执行/评分及引用。修复通用执行缺陷，不针对隐藏评分条件写分支。
4. **双 Agent 与恢复**：按 ACCEPTANCE.md 验证并发、额度、原操作查询、失败隔离和停止/重启。
5. **可选能力**：根据真实需求实现并验证取消/资源；不支持时保留明确缺口。

所有代码/依赖/容器/配置摘要和失败应可追溯。不得因为文件生成或服务启动就宣布任务三整体通过。2026-10-11 经用户授权，本项目已初始化 Git 并发布到 https://github.com/pretextQ/trae-agent.git；后续每个独立完成且检查通过的改动分别提交推送，只包含相关代码和脱敏文档，凭据仅存于被 `.gitignore` 排除的私有 `.env`。
