# 第二个真实 Agent:trae-agent

日期:2026-10-11。项目目录:`D:\woker\trae`。Eyes 对照基线:`67d2195`,仓库:`D:\projects\eyes`。

**2026-10-11 历史状态**:Agent 已实现并完成本机真实验证——三任务原样执行全部 `succeeded`,Eyes 原评分器独立评分 4/4 全部 pass,费用 ¥0.0215(预算 ¥1)。该轮实际数字见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)；当时 Eyes 控制面接入未开始。

2026-10-12 最新状态：主动 HTTP 目标发布、原三任务闭环、与 MewCode 的真实重叠执行及定向恢复已完成；接入未确认请求缺陷修正 `357d2ec`。上段保留为 10-11 历史结论。实际范围、费用、失败和 M1/M2 剩余条件见 [Eyes 接入/验收说明](docs/EYES_ACCEPTANCE.md)，不宣布全部里程碑通过。

trae-agent 是独立于 MewCode 的真实任务执行 Agent:自己的模型循环、工具执行、任务状态和产物,接入 Eyes 统一 Agent 协议 v1。运行时默认调用 MiMo-V2.6-Flash(全小写 `mimo-v2.6-flash`);ZCode + GLM-5.3-Flash 仅是开发工具,不参与运行时。

## 直接使用与被动观测

已有 AgentLoop 可通过 `python -m trae_agent.observe` 直接执行用户任务；可选 exporter 使用 ObservationBatch/Event 上报 Eyes，无需实验、测试集、评分器或 Runner。当前运行在 Docker Linux，配置、真实接入及补传边界见 [被动观测说明](docs/OBSERVATION.md)。原 HTTP 接入不自动启用该 exporter。

## 快速开始

```sh
# 0) 复制 .env.example 为 .env,填入 AGENT_MODEL_API_KEY、AGENT_RUN_BUDGET_RMB、
#    AGENT_HTTP_TOKEN(≥32 字符随机值)与 EYES_REPO(Eyes 检出路径)
# 1) 构建并启动(参考外壳依赖 POSIX 文件锁,须在 Linux 容器内运行)
docker compose --env-file .env -f deploy/compose.yaml up -d --build
# 2) 提交真实任务并核验产物
uv run python scripts/submit_task.py --dataset D:\projects\eyes\integrations\deta\coding-smoke.jsonl --case fix-mean
# 3) 用 Eyes 原评分器独立评分(容器内运行)
#    命令见 docs/IMPLEMENTATION.md 第 3 节
```

## 文档

| 文档 | 用途 |
| --- | --- |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 架构、开发/运行模型配置、工具循环、协议、隔离及分阶段开发 |
| [ACCEPTANCE.md](ACCEPTANCE.md) | 单 Agent 闭环、与 MewCode 并发、恢复及可选能力验收 |
| [ZCODE_PROMPT.md](ZCODE_PROMPT.md) | 可直接交给 ZCode 的完整开发提示词 |
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | 实施与验证的实际记录(脱敏) |

## 约束(继续有效)

Eyes 原有架构、API、数据库和评分器不在本项目修改范围。24 小时持续运行不是当前要求;协议中的终态最短 86400 秒保留指磁盘记录生命周期。凭据仅存于被 `.gitignore` 排除的私有 `.env`;付费调用前必须配置 `AGENT_RUN_BUDGET_RMB`,无明确预算不发起任何模型调用。
