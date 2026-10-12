# 接入 Eyes 与双 Agent 验收清单

2026-10-12 最新实测：目标发布、独立 Runner、两 Agent 原三任务闭环/真实重叠、准备失败隔离、批次取消和 trae 原操作查询/重复核对已通过定向范围；未确认模型请求缺陷已修正。完整事实、失败和未覆盖项见 [docs/EYES_ACCEPTANCE.md](docs/EYES_ACCEPTANCE.md)。下文 10-11 “尚未执行”是历史状态，不表示当前没有主动验收；取消/资源正向、完整故障/证据恢复矩阵继续保留。

下文是原验收清单,不能把清单本身当作通过记录；最新实际范围见上方链接。源代码/协议以 Eyes 当前 HEAD 为准;原清单编写时基线为 `67d2195`。Agent 默认运行模型为 `mimo-v2.6-flash`,ZCode 开发模型保持 GLM-5.3-Flash;模型请求产生费用前先落实独立 MiMo 预算及凭据。

状态(2026-10-11):第 1 节三任务已由 trae-agent 真实执行并通过原评分器 4/4(见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md));第 2 节起的 Eyes 控制面发布、Runner 签发、双 Agent 并发、失败隔离与 Runner 恢复验收**尚未执行**,仍以本清单为准。服务侧已实测:幂等重放零新调用、同键/同 attempt 冲突 409、错凭据 401、产物 SHA-256 一致、重启后未完成转 unknown 且不重跑、unknown 占额返回 429。

## 1. 固定任务与评分

复用 `D:\projects\eyes\integrations\deta\coding-smoke.jsonl` 和 `integrations\deta\scorer.py`，不要创建替代答案、修改评分阈值或新增任务来制造通过。

| 任务 | 输入/产物 | 实际评分方向 |
| --- | --- | --- |
| fix-mean | 输入错误 stats.py，输出修复的 stats.py | 平均值、单元素、负数、空输入异常 |
| stable-unique | 输出 unique.py | 顺序、空输入、字符串、不可哈希元素 |
| integer-cli | 输出 total.py | 实际 argv/stdout JSON，含大整数 |

业务 Agent 只能收到 prompt/files，不读取 expectations、scorer 或预期答案。独立评分在 Eyes 的可信评分进程完成。目标的 succeeded、评分 pass 和证据 sealed 是三个独立结论；失败也能提供可信验收材料，不把第二个 Agent 接入做成硬编码解题器。

## 2. 部署与接入准备

使用新 Agent 独立服务凭据、状态卷、任务容器及建议端口 19070（实施时确认未占用）。容器内 localhost 指自己，Runner 与 Agent 的实际 origin 按 Docker 网络配置；不要为了连接而放宽认证。

读取 Eyes 的 agent-http-runtime.md，使用已有发现命令生成 TargetPublish，再以项目管理身份发布新目标版本：adapter 为 agent_http，真实发现放入 config.discovery。能力改变时重新发现并发布，不改已冻结实验。

分别签发 MewCode 与 trae-agent 的限定目标 Runner 凭据；每个 Runner 的 state_dir 不同。独立评分 Runner 复用原评分器，并核对实现摘要和依赖。所有控制请求及嵌套契约携带 schema_version，外部 Agent 消息使用 protocol_version。

两目标 initially concurrency=1，每个 execute Runner execution_slots=1；项目/全局执行额度至少 2，但先核对预算和资源。不要为了验证总并发 2 而把未隔离目标单体并发设成 2。execute/score 工作类型容量分别记录。

新建独立项目、数据库/证据材料、批次和端口，不覆盖旧验证环境，不修改 Eyes 核心。一次干净接入的必要配置、安装命令和错误应写回新项目实施文档，不能仅提供开发机私有脚本。

## 3. 必需验收

| 场景 | 通过依据 |
| --- | --- |
| 新 Agent 身份 | 独立项目源码/提交/摘要；未调用 MewCode 执行循环；运行时实际模型配置已记录 |
| 真正的模型/工具执行 | 模型请求实际发生；工具输出、真实代码文件和验证命令可定位，非预设结果 |
| 单 Agent 闭环 | 三任务各自有 Experiment/CaseRun/Attempt、固定 ScoreRun、事件/产物/清单及结果；记录 pass/fail/缺口而非笼统成功 |
| 幂等与身份 | 同键重放不新增 run/Attempt/模型调用；同键不同参数及同 Attempt 换键冲突；无/错凭据拒绝 |
| 产物真实性 | API 下载的字节、大小和 SHA-256 与任务真实文件相同，评分引用属于固定清单 |
| 双 Agent 同时执行 | 真实执行边界/工具或模型时间区间证明重叠；两个来源、任务目录和状态身份不同 |
| 额度与队列 | 全局/项目/目标/实验/Runner 额度不超限；无匹配能力保持排队；未知占额没有被新批次绕过 |
| 失败隔离 | 一个真实成员发生明确失败时，另一成员继续；不手写假失败/评分来凑场景 |
| Runner 退出恢复 | 独立故障环境中查询原远端 run/幂等键，旧结果补传；unknown 不自动重跑，可信终态只释放一次 |
| 接入服务重启 | 旧终态和产物可查询；未完成转 unknown 且不重新调用模型；保留原幂等映射 |
| 停止/重启周期 | 正常退出没有未解释的工具进程残留；再次启动历史数据可读，不要求 24 小时在线 |

优先复用已有充分且仍适用的 Eyes 安全恢复证据，只补与新 Agent 特性相关的场景。双 Agent 结果分别展示，不合并不同口径质量；它们不是同一 Agent 的版本改善/退化证据。

## 4. 可选能力

如果 cancellation=true，实际验证主动取消、取消与自然完成竞争、工具及后台任务退出。只关闭客户端/HTTP 连接不算停止确认；未确认停止保持 unknown/占额。不能为了能力验收改写自然成功为 cancelled。

如果 resources=true，使用真实任务文件的隔离副本验证完整上传、大小/摘要、同 ID 重放/冲突、提交引用及越权拒绝。name 仅为逻辑名称，拒绝越界路径；不能把 Windows 绝对路径交给 Agent。

如果可选能力未完成，能力设 false并明确记录，不把负向拒绝当作正向流程通过。后续被动观测仍是另一个任务：这里的外部 AgentEvent 经 Runner 进入评测证据，不等同于 observation exporter 已接入。

## 5. 记录与交付

实际运行后建立结果记录，至少包含：版本/源码/镜像/依赖、模型 ID 与未知限制、任务和评分口径摘要、行政对象身份、实际命令和时间、原始结果、产物下载核对、并发区间、故障前后状态/额度、费用、失败及未覆盖项。

原始事件、日志、凭据、数据库、模型正文和产物保存在私有忽略目录；公开文档仅写脱敏统计和复查入口。不新增仓库测试、fixtures、mocks 或模拟业务结果，除非用户后续明确授权。

完成状态分别列“代码实现、静态检查、真实验证、阻塞/未覆盖”，给出本项目通过范围及 Eyes 任务三剩余门槛。不得自动把 M1/M2/M4 标为全部通过。
