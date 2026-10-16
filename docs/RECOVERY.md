# 模型请求诊断与离线费用协调

2026-10-17，Windows/PowerShell开发，本机Linux容器验证。基线e6e0382。
用户在Eyes聊天中明确批准外部trae诊断/离线账本入口的实现及隔离验证，未授权应用到
原订单历史环境。原订单第二次评测的unknown、占额和¥0.184524预留仍未解决。

## 已修复的缺口

旧客户端HTTP错误只返回有限正文，未保留供应商响应头/响应ID；账本仅有聚合pending，
没有原请求身份或外部结算入口。协议层又以费用是否确认推导模型停止，混合了两项事实。

现在发送前在同一私有账本原子保存call_id、run/attempt/原幂等键摘要/call_index、
模型、Key的SHA-256、请求载荷SHA-256、起始时间、价率和预留；不保存完整模型载荷。
响应仅保留字符/长度受限的x-request-id/request-id、JSON响应id、HTTP状态和error.code。
不存在的ID不编造；含已知模型/服务凭据的ID丢弃。不保留任意响应头、cookie、认证头或错误正文。
诊断进入请求账目、ModelError/ModelResponse及模型边界事件，原有错误分类保持。
HTTP500、429、网络异常都不自动付费重试，未知停止/费用继续阻止新调用。

合法Chat完成的finish_reason与usage分别处理：完成响应可以确认该模型请求结束，
usage缺失仍保留未确认费用和预留；HTTP错误本身仍不证明停止。HTTP服务按本run的请求记录
判断停止，不再以费用结算代替停止。工作区清理仍是实际独立动作。
这些修改不重写已持久化的旧协议快照；旧原键GET仍只读原unknown，不会自动补账或改终态。

## 受支持的离线入口

在包含本次源码的新Linux运行环境中，以状态目录所有者运行：

```sh
# 容器内。evidence.json仅是元数据，原始证明保存在受控私有档案。
AGENT_STATE_DIR=/private/state python -m trae_agent.reconcile --evidence /private/evidence.json
```

命令不调用模型、供应商或Eyes API，不写协议SQLite，不清理工作区。
使用前停止本地HTTP服务及direct任务写入方，备份状态；这一步只保证本地互斥，绝非远端停止证明。
要求当前UID拥有状态目录、目录权限0700。入口以protocol.lock/direct.lock拒绝活动写入方，
并使用跨进程budget_ledger.lock重新读账；账本临时文件0600、fsync、原子替换及目录fsync。
操作者在私有档案人工核实供应商证明的真实性；来源字符串、摘要或管理员权限不是供应商签名验证。

Evidence的必需公共字段：format_version=1、evidence_id(UUID)、operation、call_id(UUID)、
identity（run_id、attempt_id、idempotency_key_sha256、call_index）、source、source_reference(UUID)、
evidence_sha256、observed_at（带时区）。direct任务的attempt/key摘要可为null。
identity必须逐项与原账目匹配；已知供应商ID必须匹配，确认停止或结算时要求provider_request_id。
如供应商明确证明该错误没有ID且能唯一对应原请求，才可在supplier_record中明确设置
provider_request_id_unavailable_confirmed=true；仍需原identity、证明来源/引用/摘要，不能因
本地未采集到ID就设置该标记。有已知ID时不允许使用此分支，也不把证据UUID冒充供应商ID。
来源仅supplier_record/operator_review；时间和引用须与私有原始证明对应。
usage只接收prompt_tokens、completion_tokens、prompt_tokens_details.cached_tokens，拒绝多余字段。
不在元数据里放Key、完整请求/响应、账单正文、URL令牌或客户敏感内容。

三个操作均保留evidence、载荷摘要、UID、登记时间、前后金额/请求事实和回执：

| operation | 前置与效果 |
| --- | --- |
| observe | 证据不足可仅登记；stopped_confirmed=true须supplier_record。停止确认不结算费用，pending不变 |
| settle | 须supplier_record、明确charged/not_charged、amount_rmb及供应商请求身份。charged还需usage与prices(input/cache/output，每百万token人民币)，必须与冻结价率及金额一致；not_charged必须明确金额0且无usage。只结算对应请求，不推导停止 |
| bind_legacy | 仅为旧聚合pending显式绑定，不结算也不确认停止，详见下节 |

同evidence_id同规范化内容重复返回原回执，不再次结算；异载荷或异身份拒绝。
已外部结算的请求不能换证据ID再次结算；账单与原usage/价率冲突拒绝。
费用已知而停止未知时，即使pending已结算，新付费请求仍被停止事实检查拦住。
当前不实现退款/折扣价差更正或替换旧证据；有冲突保留原事实并另行审查。
CLI成功退出0，校验/权限/身份/冲突错误退出2，仅输出固定错误码。

## 旧账本兼容与原个案

新文件format_version=2；旧无版本文件按1读取，读取不迁移、不改字节。
旧pending或unconfirmed_usage_calls继续阻止付费；不自动清零或按时间视为过期。
第一次正常新预留或经过审核的导入才写2。未知未来版本拒绝。
BudgetLedger/ModelError等旧导入路径保留；服务和CLI必须一起升级。
**v2写入后不得直接降级旧二进制**：旧客户端不认识独立停止记录，也不验证版本，不能保证安全。
恢复旧版本须人工审核完整备份和未解决请求；不把降级操作当作本轮已验收。
未来部署须冻结实际提交/配置并发布新的Agent目标版本，本轮没有部署或发布。

旧聚合pending没有原call_id时，先由操作人员明确准备bind_legacy证据：

- 提供绑定前ledger_sha256、protocol_sha256、原run/attempt/原键摘要/call_index，
  以及已人工核对的历史prices；payload及模型Key的原指纹无法追溯时记录null，不伪造。
- 只接受一个可归属的unknown预留、unconfirmed_usage_calls=0；原账本/协议字节须匹配。
  SQLite必须为本地正常停写快照，无非空WAL/journal；入口不帮原库做checkpoint或恢复。
- 校验原AgentTask/AgentRun契约、身份、预留金额、唯一最后未响应请求及错误序号、模型。
  模型不匹配、多个未知预留、已改动文件、已绑定或不完整记录均拒绝，不猜测归属。
- 绑定生成v2请求账目并保留全部既有spend/calls/pending。随后才可用独立供应商证明
  observe停止或settle账单。绑定自身不是供应商证据，也不是释放平台占额的依据。

原订单历史环境没有运行上述任何写入命令。供应商证据仍需用户取得：2026-10-16北京时间
23:16:12.688993～23:16:13.449372附近、mimo-v2.6-flash、非流式、max_completion_tokens=8192、
HTTP500/正文503的唯一请求ID/停止证明/最终usage与账单；前7次¥0.03102228尚需供应商对账。
只有余额变化不够。费用预留¥0.184524不是已确认消费或零消费。

取得证明后，先对私有副本用本入口校验/协调账目，再经单独历史应用授权处理原账目。
Eyes端使用新manage `/v1/attempts/{id}/reconciliation-evidence`分别登记事实；只有业务终态
及停止证据齐备才显式apply，费用不会替代业务或停止。账本入口不修改Eyes占额；
Eyes入口不结算本账本。原协议unknown不会由本CLI覆盖，工作区也需独立核查/清理。
官方依据：[Chat响应与usage](https://mimo.mi.com/docs/zh-CN/api/chat/openai-api)、
[错误码](https://mimo.mi.com/docs/zh-CN/api/guidance/error-codes)没有给出500/503停止或零计费保证。

## 本轮实际隔离验证

Windows PowerShell，本机Docker Desktop Linux；使用既有依赖镜像eyes-clean-control:20261011，
Python3.14.7，httpx0.28.1，Pydantic2.13.5。当前源码和verification只读挂载，/tmp临时材料，
`--network none`，没有真实Key或历史挂载。ModelClient使用真实httpx MockTransport接收
合成HTTP响应，无网络，不调用实际供应商/模型，也没有执行原订单或运行评分。

```powershell
docker run --rm --init --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m --memory 1g --pids-limit 128 --mount 'type=bind,source=D:\woker\trae\src,target=/work/src,readonly' --mount 'type=bind,source=D:\woker\trae\verification,target=/work/verification,readonly' -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work/src -w /work --entrypoint /app/.venv/bin/python eyes-clean-control:20261011 /work/verification/recovery.py
```

最终退出0，六个scenario/passed=true，末尾verification=complete：

- HTTP500/正文503、request-id保留，错误正文/cookie/认证头/已知Key及模型载荷不落盘；只发一次离线请求。
- run/attempt/原键摘要、供应商ID不匹配拒绝；证据不足保持未知；停止单独确认不结算。
- 计费先确认不推导停止，仍拦付费调用；有完成响应但缺usage，停止已知、费用未知。
- charged usage/冻结价率/金额一致才结算；错误金额拒绝；重复不重复计费，冲突拒绝。
- 新CLI进程重读后返回同回执；服务锁占用/目录非私有拒绝；审计前后事实保留。
- 正常usage仍正确结算；未知账本版本拒绝；旧账本读取不改字节，显式绑定保留金额、
  重复幂等、错误序号拒绝，绑定/结算不改变协议SQLite字节。
- 未采集ID本身不足以确认停止；明确供应商“无ID且唯一关联”证明可记录停止，预留仍保留。

初次驱动退出1是反例材料用了不合法哈希，提前被Schema拒绝而非预期的身份校验；修正材料
为合法但不匹配的哈希后通过。未改写该失败为首次通过。
`ruff check src scripts verification`通过；`ruff format --check src verification`通过；
`git diff --check`通过。扩大到scripts的format检查失败于**未修改**的scripts/submit_task.py两处
既有格式，未为本轮调整无关文件；未宣称全仓格式通过。
http_service/observe/loop容器导入通过（未构造应用/调用Agent），不是其真实业务闭环复验。

未覆盖：真实供应商错误与账单、默认Agent镜像Python3.14.8组合环境、长期/多进程压力、
磁盘断电/损坏、退款与更正、非唯一旧预留。没有修改原协议库、账本、占额或原始恢复材料。
