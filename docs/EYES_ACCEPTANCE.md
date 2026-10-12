# Eyes 主动接入和双 Agent 验收（2026-10-12）

本轮复用现有 HTTP 接入和独立 AgentLoop，没有新建 Agent、Python Adapter 或业务循环。Eyes 运行基线 `4debab95b8a6b34f884df49e563d70982ff8dce8`；trae 初版 `27aa319`，实际断网接入修正 `357d2ecdd1652c4ec0adb5b8e1685b01fdf51053`。对照 Agent 是 MewCode `3f10408`，不把双 Agent 结果当作同 Agent 改善/退化。

实际 runtime：Linux CPython 3.14.8、UID 10001、httpx 0.28.1、FastAPI 0.143.0、Pydantic 2.14.0、Starlette 1.7.0、Uvicorn 0.54.0；依赖与当前锁定条目相符、pip check 通过。旧 IMPLEMENTATION 页的历史依赖版本仅指旧轮次，本次不沿用它们描述实际环境。默认/实际模型均 `mimo-v2.6-flash`，官方按量地址、thinking enabled；11 个实际加载源文件逐项核对，没有 MewCode 执行循环导入。

## 接入方法及独立发布

仍按 DEVELOPMENT、Eyes agent-http-runtime 和 integrations/mewcode README 使用 Docker Linux、单服务 worker、独立 SQLite/state/workspaces/凭据。HTTP factory `trae_agent.http_service:app`，输入 prompt/files，原三任务兼容。能力为 concurrency=1、session_isolation=true、environment_isolation=false、events/artifacts=true、cancellation/resources=false，终态至少保留 86400 秒；这是磁盘保留声明，不是要求连续运行一天。

能力发现命令为 `python -m eyes.adapters.agent_http <loopback-origin> --name <trae-target-name> --token-env AGENT_HTTP_TOKEN`，管理身份 POST `/v1/targets`；发布后的 config.discovery 冻结。不同能力/源码发布新版本，不能替换旧实验。Docker Runner 共享对应 Agent 网络命名空间使用认证 loopback origin；HTTP 服务名 origin 实际被 Eyes 拒绝，不能通过放宽协议解决。

本轮新项目 `2fb02d3f-a313-41de-a973-423d8e743079`，新数据库/卷/网络 `eyes-dual-20261012`，API 18072，trae 初版 19073/修正版本 19075，MewCode 19074。trae 初版目标 `15caec85-aa4d-40e3-bc92-3f569967e413`、修正版本 `8fad5a85-fcc8-4527-ac4d-e899c248cac0`；MewCode 独立目标 `95d4d3cb-d76f-4e3a-8321-03ac9e5a1790`。分别签发仅 execute/指定目标 Runner，score-only 令牌另签；各有私有状态卷。Agent 不拿平台/Runner 凭据、预期答案或评分配置。

## 真实结果和费用

原样使用 Eyes `integrations/deta/coding-smoke.jsonl` 与 `scorer.py`。首批次 `b2268f37-8d61-49f4-84fa-db48f1c68f87` 中两个 Agent 各 3/3 succeeded、独立 completed/pass 3/3、12/12 检查；修正版本再完成三任务 3/3、12/12。正常闭环均 sealed/cleanup succeeded，共 15 份真实产物由 Eyes API 下载、大小/摘要一致。

实际事件证明 trae fix-mean 10:58:22.263015–10:58:46.857143 与 MewCode mewcode.run 10:58:22.530640–10:58:28.907588 重叠 **6.376948 秒**。项目/全局执行额度 2，每目标/执行 Runner 1，评分槽 1；没有提高单目标并发，独立工作目录不冒充安全沙箱。

用户本轮总预算 ¥1，trae/MewCode 各 ¥0.50。trae 修正实例上限扣除初版已花费用，不重置预算。trae 两实例 40 次、¥0.03038504；MewCode 私有请求前预算网关 26 次、按高峰价 ¥0.04649608；总计 **66 次、¥0.07688112**，无未确认 usage 或待结算预留。网关仅费用门禁、原样转发 SSE，不改 MewCode 输出；该操作工具不等于 Eyes 有通用预算产品。请求前预留是保守文本估算，依赖供应商输出上限与标准计价，不能宣称供应商金额硬限额。计价来源：[MiMo](https://mimo.mi.com/docs/pricing)、[DeepSeek](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。

## 故障、权限与能力边界

- 20 个实际具名权限请求：本目标下载 200、另一目标 403；execute 令牌领取 score 403；误用 Runner/另一 Agent 凭据访问 Agent 401；read 不能取消。取消/资源端点真实返回 422 unsupported_capability，保持 false，不声称正向验收。
- trae 接入不可达造成原三任务准备 failed/ConnectError，MewCode 三项仍执行/独立评分通过。首次停 Agent 同时使共享网络的 Runner 不可达控制端，仅排队，不当作失败通过；后续独立可达控制端的故障 Runner 才复现准备失败。
- 强杀 trae execute Runner 后，取消批次：每目标两项排队 cancelled，活动工作停止未确认时保留 unknown/额度。新 Runner 按原幂等键查询同一远端 run `c8ebe3da-ca78-4579-a406-8e8bab0531e3`，原 Attempt 恢复 succeeded、只应用一次；两次重复提交 duplicate=true。旧 Runner 只补传三个旧事件。该故障批次无有效通过评分，完整证据补齐仍是缺口。
- 独立断网容器实际复现 HTTP stopped_confirmed 假终态，修正见 IMPLEMENTATION。当前 unknown 保留工作目录/预算/容量；原键重放和重启保持同一 run、calls=0、新提交 429，不重跑。没有用连接关闭、租约到期或本地退出证明供应商停止。
- 通用 Eyes 原结果 outbox 补传、竞争/迟到、unknown 占额及终态一次释放，直接沿用 Eyes `docs/runner-reliability-20261011.md` 的适用真实证据，本轮不伪造新注入。工具 kill_all 仍 best-effort；任意恶意子进程/供应商取消不在通过范围。

正常停止/重启后原批次、结果、全部原 run 快照、15 份产物摘要、预算账本一致；正常服务退出 0，故障 Runner 137 单独记录。结束本轮容器运行数 0，原环境未启动或改写，unknown/失败现场与历史保留。没有修改测试、评测用例、fixtures、mocks、外部业务输出，也没有做 24 小时运行。

完整 Eyes 记录：[dual-agent-acceptance-20261012.md](https://github.com/pretextQ/eyes/blob/main/docs/dual-agent-acceptance-20261012.md)。私有原始字节/请求/journal/预算在本机 Eyes `data/dual-agent-20261012/`，不提交。M1 只关闭第二 Agent 主动 HTTP 闭环缺口；独立宿主机/Python复现仍待验证。M2 保留完整证据恢复、取消/资源正向、更强环境隔离及其他故障组合，不宣布全部里程碑通过。
