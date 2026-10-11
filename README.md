# 第二个真实 Agent：开发文档入口

日期：2026-10-11。项目目录：`D:\woker\trae`。Eyes 对照基线：`67d2195`，仓库：`D:\projects\eyes`。

本目录目前只包含开发文档，没有 Agent 实现，也没有运行或验收结果。目标是开发独立于 MewCode 的真实任务执行 Agent，接入 Eyes，完成“第二个真实 Agent 与双 Agent 并发验收”。不是此前的被动观测任务。

约定：ZCode + GLM-5.3-Flash 用于开发本项目；新 Agent 在运行时通过独立模型凭据默认调用 MiMo-V2.6-Flash，API 模型 ID 使用全小写 `mimo-v2.6-flash`。ZCode 是开发工具，不假设它提供可被 Eyes 调用的自动化 API，也不读取或复用其登录令牌。若实际想把 ZCode 本身作为被测 Agent，需先另行核实官方执行接口，不采用 UI 自动点击替代本项目。

项目临时标识为 `trae-agent`；目录名不意味着依赖 Trae IDE。新 Agent 自己拥有模型循环、工具执行、任务状态和产物，不借用 MewCode 的执行实现。

| 文档 | 用途 |
| --- | --- |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 架构、开发/运行模型配置、工具循环、协议、隔离及分阶段开发 |
| [ACCEPTANCE.md](ACCEPTANCE.md) | 单 Agent 闭环、与 MewCode 并发、恢复及可选能力验收 |
| [ZCODE_PROMPT.md](ZCODE_PROMPT.md) | 可直接交给 ZCode 的完整开发提示词 |

先把 [ZCODE_PROMPT.md](ZCODE_PROMPT.md) 交给 ZCode，要求它读取其他两份文档再开发。开发完成后由 Eyes 侧按验收清单发布新目标和执行真实批次。

Eyes 原有架构、API、数据库和评分器不在本项目修改范围。24 小时持续运行不是当前要求；协议中的终态最短 86400 秒保留指的是磁盘记录生命周期，不要求服务连续开机 24 小时。

本次没有安装依赖、调用模型、生成业务代码或创建 Git 提交。后续付费运行预算需要明确，本项目不默认继承 Eyes 其他任务中的预算。
