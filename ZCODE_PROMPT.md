# 交给 ZCode 的开发提示词

复制以下提示词到 ZCode，在 `D:\woker\trae` 开发。建议先阅读其他文档再执行。

```text
你负责开发一个独立的真实任务执行 Agent，项目目录 D:\woker\trae，暂名 trae-agent。目标是成为 Eyes 的第二个真实 Agent，与现有 MewCode 完成双 Agent 并发与恢复验收。不是开发被动观测 exporter，也不是给 MewCode 换一个模型名称。

先完整阅读本目录 README.md、DEVELOPMENT.md、ACCEPTANCE.md，再定向读取 D:\projects\eyes 的 AGENT.md、统一 Agent 协议、agent-http-runtime、agent_protocol.py、参考 HTTP 服务和现有任务/评分器。Eyes 文档仅用来确定对接约束；不要修改 Eyes 核心源码、数据库、API 或现有评分口径。

ZCode + GLM-5.3-Flash 是开发方式。新 Agent 运行时默认调用 MiMo-V2.6-Flash，API 模型 ID 为全小写 mimo-v2.6-flash。使用独立、合法授权的 MiMo 凭据，不读取 ZCode 登录凭据，也不复用 GLM 通道和密钥。实际模型权限、API 通道和预算先核实；无凭据或预算时继续无费用实现，不发起付费任务。

实施：
1. 创建独立 Python 项目、锁定依赖和 Linux 容器方案；优先复用 eyes.agent_service.http.create_app 及公共契约，不重写持久化协议层，也不复用 MewCode 业务执行循环。
2. 实现 prompt/files 输入、MiMo 工具调用循环、任务内读写/编辑/Python验证工具，实际生成文件。thinking、输出 token 与消息参数按 MiMo 当前官方契约处理，不套用 GLM 特有规则。不要按任务名硬编码答案，不把建议当作已修改文件。
3. 实现独立 runtime、workspace、事件和真实产物采集、截止时间、清理及限额。密钥不传入工具进程，不允许工具访问其他任务或协议数据库。
4. 接入必需 HTTP 生命周期、查询、幂等、事件与产物。protocol_version 和 schema_version 保持不同；按实际能力声明，初始单目标并发 1。
5. 能力不足时 cancellation/resources 为 false。后续实现取消或资源必须提供真实停止/字节核对，不假装参考外壳已替你完成工具停止或安全隔离。
6. 预算明确后原样执行 Eyes 既有三任务，由 Eyes 原评分器独立评价真实代码。模型输出不得看到评分预期。失败、缺失和费用保留真实记录。
7. 接入 Eyes 时使用新目标、独立 Runner/Agent 凭据与状态目录。MewCode 1 槽和本 Agent 1 槽实际同时运行，通过真实执行区间证明并发；不要修改旧目标能力凑数字。
8. 按 ACCEPTANCE.md 做幂等、权限、产物、并发/额度、失败隔离和原操作恢复。未知执行不重跑，不凭本地退出释放额度。当前要求按需启动，不要求运行24小时。

边界：不新增或修改 Eyes 仓库测试/任务/评分器，不创建模拟 Agent 输出或评分。新 Agent 也不新增测试文件/fixtures/mocks，除非我另行明确授权；使用已有检查及真实任务验收。只在本项目中开发，不扫描无关目录、不影响已有服务，不安装无关依赖。不要用 ZCode UI 自动化冒充可重复执行接口。

分阶段推进：无费用项目/协议/工具准备 → 单任务真实闭环 → 原三任务 → 双 Agent 并发与恢复 → 可选取消/资源。每阶段记录实际完成、检查、失败、未覆盖项。需要凭据时只询问环境变量名或私有配置路径，不索取密钥正文。

交付项目源码、锁文件、容器/配置模板、接入说明和脱敏验收记录；更新本目录文档为实际状态。保持需求范围，不开发UI、长期运行、自动修复或新评分体系。当前没有 Git 远端；在未获得明确仓库和发布授权前，不初始化 Git、Commit 或 Push。开发完成后报告如何接入 Eyes、实际验证和剩余门槛。
```
