# trae-agent 实施与验证记录

状态(2026-10-11):**代码实现、静态检查、无费用协议检查、三任务真实执行与独立评分、幂等/重启恢复验证已完成;Eyes 控制面接入(目标发布/Runner/实验/双 Agent 并发)未开始,为剩余门槛。** 本页只写脱敏事实;原始事件、产物字节、报告与账本在本地忽略目录 `state/`。

## 1. 版本与环境

| 项 | 实际值 |
| --- | --- |
| 本项目提交 | `c1c10d5`(实现)、`1e9a7c8`(fastapi 依赖修复),见 git log |
| Eyes 基线 | `67d2195`(挂载 `D:\projects\eyes`,git status 干净,只读) |
| 运行环境 | Docker Desktop 29.8.2,Linux 容器 `python:3.14-slim`,非 root uid 10001 |
| 依赖锁定 | `uv.lock`;httpx 0.28.x、fastapi 0.129.x/starlette 1.7.0、pydantic 2.12.x、uvicorn 0.54.0(以锁文件为准) |
| 静态检查 | `ruff`(E/F/I/UP/B,py314,line 100)全部通过;`python -m compileall` 通过 |
| 服务端口 | 19070,单 worker,命名卷 `deploy_trae_agent_state` |

## 2. 模型配置与费用

- 模型:`mimo-v2.6-flash`(全小写),通道 `https://api.xiaomimimo.com/v1`,`thinking=enabled`,非流式;`/v1/models` 实测该 ID 存在(2026-10-11)。
- 计价(官方按量页,2026-10-11 查阅):输入 ¥1/M tokens(缓存命中 ¥0.02/M),输出 ¥2/M tokens;账本按 usage 中 `prompt_tokens_details.cached_tokens` 分列计价。
- 预算:`AGENT_RUN_BUDGET_RMB=1`(用户明确);预算台账进程内共享、持久化于状态卷,每笔调用后落盘,`ensure_can_spend` 在每次调用前强制检查,无预算或超预算拒绝发起付费调用。

实际账本(4 次计费运行:3 个任务 + 1 次幂等验证运行;重启实验运行 0 次计费调用):

| 指标 | 值 |
| --- | --- |
| 模型调用 | 25 次 |
| 输入 tokens | 37,798(其中缓存命中 26,176) |
| 输出 tokens | 4,694 |
| 累计金额 | ¥0.021533(占预算 2.15%) |
| usage 缺失 | 0 次 |

## 3. 实际命令(宿主 PowerShell/Git Bash;服务命令在容器内)

```sh
# 构建并启动
docker compose --env-file .env -f deploy/compose.yaml up -d --build

# 提交真实任务(协议客户端:提交/轮询/产物 SHA-256 核验/报告)
uv run python scripts/submit_task.py --dataset D:\projects\eyes\integrations\deta\coding-smoke.jsonl --case fix-mean

# 用 Eyes 原评分器独立评分(评分器依赖 POSIX,在容器内运行)
docker run --rm -v D:\projects\eyes:/opt/eyes:ro -v D:\woker\trae\state\downloads:/data:ro \
  -v D:\woker\trae\scripts:/app/scripts:ro -v D:\woker\trae\state\reports:/reports \
  --entrypoint /app/.venv/bin/python trae-agent:local /app/scripts/evaluate_local.py \
  --dataset /opt/eyes/integrations/deta/coding-smoke.jsonl --case fix-mean --artifacts /data/<幂等键目录>
```

## 4. 无费用协议检查(10/10 通过,0 次模型调用)

capabilities(200,`agent_id=trae-agent`、并发 1、无取消/资源、事件/产物 true、保留 86400);无/错凭据 401 `unauthenticated`;空 prompt 与多余字段 422 `invalid_input`;`protocol_version: 9.9` 400 `invalid_request`;过期 deadline 422 `deadline_expired`;`files: {"../evil": ...}` 422 `invalid_input`(输入校验拒绝路径穿越);未知 by-key 与未知 run 404 `not_found`。

## 5. 三任务真实执行与独立评分

三次提交均 202 接受后真实执行,终态 `succeeded`、清理 `succeeded`、evidence `sealed`;产物经协议下载,字节数与 SHA-256 全部核验一致;事件序列完整(task.started → 每次模型请求/响应(含 usage/时延/费用)→ 工具调用/结果 → artifact.published → task.ended)。评分用 **Eyes 仓库原封 scorer.py**(容器内加载,未改动),结果均为 `completed / pass,4/4 checks`:

| 任务 | run_id | 产物(核验一致) | 模型/工具调用 | 评分 |
| --- | --- | --- | --- | --- |
| fix-mean | `afd52e13…79702` | stats.py 127B | 8 / 9 | pass 4/4 |
| stable-unique | `29bca534…99b7` | unique.py 1712B | 6 / 5 | pass 4/4 |
| integer-cli | `f7731530…515f` | total.py 519B | 3 / 4 | pass 4/4(含 10^19 大整数 argv 检查) |

执行内容为真实模型/工具循环(读取→修改→运行验证→修正→总结),无按任务名硬编码;`replace_text` 计数不符时如实报错不改写;工具子进程环境为白名单(PATH/HOME/TMPDIR/LANG 等),不含任何凭据。

## 6. 幂等、身份与容量

- 同键同参重放 → 200,返回原 run(revision 不变语义),**模型调用数 25→25,零新调用**;同键不同 attempt → 409 `idempotency_conflict`;同 attempt 换键 → 409 `attempt_conflict`;错凭据读取运行 → 401。
- unknown 占额:重启产生的 unknown 运行未确认停止前,新提交返回 429 `capacity_exceeded`(符合协议:unknown 不释放额度)。

## 7. 重启恢复

任务运行中重启服务:该运行转为 `unknown`,error=`service_restarted`,`stopped_confirmed=false`;等待后账本调用数不变(不重放业务);原键重放返回 200 且同一 run(映射保留);重启前已完成运行按原键查询 200 `succeeded`,产物仍可下载且 SHA-256 一致。未完成任务的执行工作目录保留在状态卷(清理未执行,如实不覆盖任何结果)。

## 8. 已声明的能力边界(与实现一致)

`cancellation=false、resources=false`(对应端点返回 `unsupported_capability`);`environment_isolation=false`——工具子进程与服务共享容器内核,按最小环境变量运行,属记录在案的可信代码假设,非安全沙箱;`max_concurrency=1`(隔离验证前不提高)。未实现取消/资源能力,未伪装参考外壳已替本 Agent 完成停止确认或隔离。

## 9. 剩余门槛(Eyes 控制面接入,本页仅列步骤,未执行)

1. 按 [agent-http-runtime.md](D:\projects\eyes\docs\agent-http-runtime.md) 做能力发现:`uv run python -m eyes.adapters.agent_http http://127.0.0.1:19070 --name trae-agent-v1 --token-env AGENT_HTTP_TOKEN > data/agent-target.json`。
2. 以项目管理身份 POST `/v1/targets` 发布新目标(adapter=`agent_http`,发现快照冻结进 `config.discovery`);能力变化需重新发现并发布新版本。
3. 签发限定该目标的 Runner 凭据(独立于 MewCode 与管理凭据),Runner TOML:`allowed_http_origins=["http://127.0.0.1:19070"]`、`secret_env_allowlist=["AGENT_HTTP_TOKEN"]`、`execution_slots=1`、`score_slots=1`;Runner 安装环境须升级到与控制端相同版本。
4. 导入既有 JSONL、发布既有评分器、创建独立项目/实验(不覆盖旧验证环境),MewCode 1 槽 + 本 Agent 1 槽同时运行,以真实执行区间证明双 Agent 并发。
5. 按 ACCEPTANCE.md 补齐:失败隔离、Runner 退出恢复、额度/队列、双 Agent 结果分别呈现; unknown 运行需按平台核对路径处理(参考外壳提供进程内 `app.state.resolve_run`,无 HTTP 修改入口)。


## 2026-10-12：调用前预算缺口修正

原 `ensure_can_spend` 只检查历史金额，不能保证下一请求落在剩余额度内；传输重试也不能证明供应商未执行。保留上面的历史运行与费用，不将旧文字解释为已有请求前预留。

当前 ModelClient 在发送前先原子持久化保守文本请求预留，限制序列化请求 128000 字节，以每 UTF-8 字节 4 token 加 4096 framing token、完整 max_completion_tokens 输出上限和配置价格计算。此文本边界是保守工程估算，依赖供应商遵守输出上限/计价，不冒充供应商金额硬限额。预算包含已花金额与预留；超额拒绝发请求。未确认请求/usage、损坏账本均拒绝后续调用，网络异常不自动重试；重启不会释放未确认预留。已确认 usage 才替换预留为实际费用。当前仅支持已配置文本/本地工具，不含图片、音视频或供应商收费工具。

2026-10-12 官方[计价页](https://mimo.mi.com/docs/pricing)仍列标准 Flash 输入 ¥1/M、输出 ¥2/M，保守使用标准价，不依赖时段优惠。本轮新预算用户明确为 ¥1。静态 Ruff/format 和字节编译通过；截至此修正提交尚未发模型请求，真实运行另记。不修改仓库测试、用例或评分器。


## 2026-10-12：直接任务与被动观测

新增 `observe.py` 直接任务入口和 `observation.py` 有界持久 exporter；扩充原循环的可选正文/错误埋点，不重建模型/工具循环。两次真实任务 succeeded，API 暂停保留 51 批、恢复补传归零，累计 ¥0.009092；实现/检查/真实证据与未覆盖项见 [接入说明](OBSERVATION.md)。主动 HTTP events 和被动 exporter 严格区分，双 Agent 并发本轮未执行。
