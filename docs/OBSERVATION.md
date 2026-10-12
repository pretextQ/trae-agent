# 直接任务与 Eyes 被动观测

2026-10-12 已实现，真实运行和范围见本页末尾。统一 Agent HTTP `/events`、Eyes 主动 SDK 与被动 exporter 是不同路径。此入口直接运行已有 AgentLoop，不提交 Eyes Experiment/AgentTask，不需要 Runner、数据集或评分器；原 HTTP 服务保持原入口，不自动启用 exporter。

## 使用方式（Windows PowerShell + Docker Linux）

当前工具进程、任务锁与 exporter 使用 POSIX 语义，运行在 Docker Linux，不承诺 Windows 原生 CLI。使用现有 `.env` 的 MiMo 配置与**明确的新预算**；来源令牌由独立 Eyes 项目的 manage 身份在观测接入面板/API 创建。项目 read/manage 令牌不能替代来源令牌。将下面配置写入 Git 忽略的 `.env.observation`，不要提交令牌：

```dotenv
EYES_OBSERVATION_URL=http://host.docker.internal:18071/v1/observation
EYES_OBSERVATION_TOKEN=<实际来源令牌>
EYES_EXPORTER_STATE_DIR=/data/state/observation
AGENT_CAPTURE_MODEL_IO=true
```

URL 必须是可以从容器访问的实际 Observation API 基地址，不是 Agent HTTP 地址。默认 `AGENT_CAPTURE_MODEL_IO=false` 只发边界/元数据；true 会发送真实用户输入、模型文本输入/输出、工具参数/结果，已知密钥及常见敏感键脱敏，不保证自由文本没有其他秘密；隐藏 reasoning_content 不上报。

先把真实用户任务写入一个不含凭据的输入目录：`prompt.txt` UTF-8 文本；可选 `files.json` 是初始相对文件名/文本映射，不包含 expectations 或评分配置。该目录只读挂载。首次按当前源码构建已有镜像；使用一个**不同于 HTTP 服务的专用状态卷**：

```powershell
$eyesRoot = 'D:\projects\eyes'
$inputRoot = 'D:\woker\trae\state\user-input'
docker build -f deploy/Dockerfile -t trae-agent:local .
docker run --init --rm --env-file .env --env-file .env.observation `
  -e AGENT_STATE_DIR=/data/state -e PYTHONPATH=/opt/eyes/src `
  -v trae_direct_state:/data/state -v "${eyesRoot}/src:/opt/eyes/src:ro" `
  -v "${inputRoot}:/inputs:ro" trae-agent:local `
  python -m trae_agent.observe --prompt-file /inputs/prompt.txt `
  --files-json /inputs/files.json --session user-session-1
```

不使用初始文件时省略 `--files-json`。同一用户会话继续使用相同 `--session`；每次任务创建新 run_id/workspace，结果 JSON 与真实文件保留在专用状态卷，不重新执行 unknown。供应商凭据不传给工具子进程。直接任务按状态目录文件锁串行；不要让 HTTP 服务共用该卷/预算文件。

exporter 可关闭或不可用时不覆盖原任务返回/异常，日志明确 observation_disabled/capture_failure。正常退出尽力交付两秒，尚未确认的批次保留。下次以**同 URL、同来源令牌、同状态目录**重新启动，先重放旧批次再继续交付；更换身份拒绝复用 spool。仅补传、不执行任务/模型：

```powershell
docker run --init --rm --env-file .env --env-file .env.observation `
  -e AGENT_STATE_DIR=/data/state -e PYTHONPATH=/opt/eyes/src `
  -v trae_direct_state:/data/state -v "${eyesRoot}/src:/opt/eyes/src:ro" `
  trae-agent:local python -m trae_agent.observe --flush-only
```

该命令尽力等待 15 秒，不表示所有待发都已确认；核对 spool.sqlite3 的 batches/rejected 和 Eyes 实际记录。重传不会调用 AgentLoop 或 ModelClient。永久 4xx 保留 rejected 行并告警，408/425/429/5xx/网络错误留待重试，不能删除 rejected 冒充完整。

## 契约、边界及有界持久化

使用当前 Eyes ObservationBatch/Event 进行本地校验，batch/event 都显式 `schema_version: 1.0`；run/session/event ID、任务内递增 sequence、UTC 时间均在实时采集时固定，重传不重新生成。run_start/input、轮次、ModelClient.complete 尝试及响应/错误、真实工具 start/end、run_end 来自已有循环实际边界；图形是事件呈现，不声称底层隐藏过程全部可见。模型开始是本地调用尝试，预算拒绝会配对 model_end ERROR，不能当作已发供应商请求；使用 usage/模型账本识别实际付费调用。

一个来源/目录一个发送锁；SQLite FULL 同步事务落盘，数据库 max_page_count 限 8 MiB，批次 payload 总量及数量限 8 MiB/4096，包含 rejected，并为终态预留容量；小型事务 journal/锁文件另有开销，不承诺状态目录恰好 8 MiB。单事件正文超过 48 KiB 去掉正文并保留边界/元数据、truncated=true；写入失败或容量不足增加 dropped_events，序号不回退。终态报真实完成/失败/限额，不把缺失或崩溃强制改成 complete。强杀、磁盘失败或终态也无法落盘仍可能缺事件，Eyes 的缺口/失联不能证明任务停止。

预算与 exporter 账本独立。调用前保守费用预留、价格/输出上限及不确定费用停止策略见 [实施记录](IMPLEMENTATION.md#2026-10-12调用前预算缺口修正)。网络恢复仅重传 observation，不重试可能计费的模型调用。

## 本轮证据

独立 Eyes API/Web 18071/18087、新 PostgreSQL/项目/来源及私有卷。用户授权 ¥1，两次直接真实 mean 修复（既有业务输入）均 succeeded，累计 11 次模型调用、¥0.009092，usage 完整、预留=0。第一任务在 API 暂停时仍正常结束，51 批待发全部持久化；恢复后仅补传 pending=0，账本字节不变。第二任务在线上报 41 事件，同 session 新 run_id，模型/工具/正文在实际 Web 中核对。没有发起 Eyes 主动评测或修改任务/评分口径。

同项目另一来源的真实额度拒绝任务：calls=0、费用=0，limited/8 事件；它的一次本地模型尝试不代表供应商请求。40 个具名权限/去重/冲突请求与预期一致，当前项目 read 可读取多个来源，来源令牌只写本来源、不读项目。不修改该读取语义。

Ruff/format、字节编译通过；未新增/修改测试、用例、fixtures 或 mocks。原始正文/日志/账本留在 Eyes 忽略目录 `data/observation-trae-20261012/`。容量耗尽、强杀 exporter、慢盘、真实供应商 usage 缺失及全部网络故障组合未注入，不宣称完整故障矩阵或长期运行。最终页面/退出/重启核对见 Eyes 的 `docs/observation-trae-acceptance-20261012.md`。
