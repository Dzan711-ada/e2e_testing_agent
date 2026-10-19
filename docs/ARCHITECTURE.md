# 当前通用三 Agent 架构

本页对应 2026-10-18 的通用核心。旧商城协议与原父子图是历史版本，保存在 `e2e_agent/legacy/` 及本轮源码备份中。历史验收原文件不改写。

## 入口和依赖

`main.py → cli.py → core.workflow.run`，实际运行入口位于 `core/workflow/runtime.py`。普通 URL、商城快捷入口和公开练习站快捷入口共享同一引擎。示例环境从 CLI 独立启动，核心不导入 `sample_shop`、`site_profiles` 或 `demo_sites`。

| 模块 | 边界 |
| --- | --- |
| `core/contracts.py` | 开放业务词汇的计划、资源、断言、工具参数 |
| `core/agents/roles.py` | 三个 Agent 角色入口 |
| `core/agents/decisions.py`、`prompts.py` | 结构化决策与各角色提示词 |
| `core/agents/gateway.py` | 模型调用、结构化输出、调用预算与供应商备用 |
| `core/workflow/state.py` | 图状态定义 |
| `core/workflow/graph.py` | 唯一业务推进图，集中保存全部节点和条件边 |
| `core/workflow/runtime.py` | 运行入口、浏览器及服务所有者、原生暂停继续 |
| `core/tools/browser.py` | 有限 DOM 观察、唯一目标、同 origin 工具与实际断言 |
| `core/resources/vault.py` | 任意字符串资源、可用性、复用和实际值隔离 |
| `core/resources/interaction.py` | 单一控制台输入、取消/超时、已知值脱敏 |
| `core/delivery/scripts.py`、`report.py` | 完整性判断、最小工程导出与报告 |
| `core/delivery/replay.py` | 按保存配方执行，不调用模型 |
| `core/delivery/launcher.py` | 父控制台资源代理、新进程、JUnit 与收集完整性 |
| `graph_checkpoint.py` | 适配已锁定 LangGraph 的异步服务与原生检查点，不含业务决策 |

## 16 个实际工作节点

| 节点 | 职责与主要去向 |
| --- | --- |
| `observe_page` | 获取实际页面，交给规划或执行 |
| `design` | Design 判断探索、澄清、就绪或阻断 |
| `inspect_page` | 访问已观察的相关链接，再观察 |
| `clarify` | 原生暂停后获取业务回答，回设计 |
| `select_checkpoint` | 按计划选择场景/检查点，管理隔离上下文 |
| `plan_step` | Execution 选择下一步或绑定原断言 |
| `await_resource` | 原生暂停后取得字符串，继续原动作或执行决策 |
| `execute_step` | 执行一项受控工具，之后重新观察或诊断 |
| `verify_checkpoint` | 实际执行全部冻结断言，再推进或诊断 |
| `diagnose` | Diagnosis 给出归因；程序约束恢复或停止 |
| `revise_plan` | 保留旧版本，把业务缺口返回 Design |
| `finish` | 固定首次结果，判断配方是否完整可重跑 |
| `export_script` | 导出计划、动作和最小支持模块 |
| `replay_script` | 新进程无模型重跑，保留不一致 |
| `diagnose_replay` | 对重跑异常/不一致单独诊断，不反向改断言 |
| `report` | 写报告并结束 |

所有探索、角色切换、场景、检查点、动作、资源、修复及交付循环都由条件边控制。图外的循环仅承担进程 I/O、输入等待或原生暂停继续。

## 状态与资源

图状态只保存可序列化的目标、页面观察、回答、计划、游标、证据、预算及资源请求元数据。模型连接、浏览器、输入线程和资源实值保存在 `Host` 服务中。

澄清同时保存完整的原问题、提问理由和回答，并在后续探索、规划、修订及新运行恢复时保留；仅有问题 ID 与短回答不足以表达用户范围。语义由 Design 结合原问题判断，回答有歧义时可继续澄清，不使用回答关键词分支。

计划定义 `requirements → scenarios → checkpoints → assertions`；不固定任何业务 ID、路由、控件文案或资源类别。独立场景使用新 context；同一场景中的检查点共享会话。动作按当前页面逐步生成，不要求匹配固定动作序列。

资源 ID 与用途由 Agent 声明。执行中新增资源会受控登记到最终计划。实际字符串不进入模型可用性表；一次性资源消费后不能再次用于动作。断言的 `expected` 接受字面量或 `{"resource_id":"资源编号"}`，允许程序比较资源相关预期，不把私密值送给模型或写入导出计划。

断言来源引用必须有效，业务结果必须有断言覆盖。重新规划保留已确认要求与原断言；修订不能用来把失败改成通过。资源接收、敏感输入、目标唯一性、工具参数、origin 和预算是通用确定性约束。

## 完整工程与失败

导出要求所有原断言已实际尝试，且动作能够安全重现。未完成、未知已提交副作用等情况不生成完整脚本。恢复时保留原始失败；独立重跑后首次与重跑相反则标记不一致。

导出仅复制六个支持模块，保持 `tools/`、`resources/`、`delivery/` 子目录并生成最小包入口，不包含模型 SDK、图调度、用户配置或示例服务器。已生成的历史工程保留原目录和导入方式。资源经父控制台的临时本机代理传递；子进程不继承模型密钥。JUnit 测试身份、数量、退出码及跳过数共同决定结果。

`resume` 是从确认计划起点创建新运行，不恢复浏览器现场；单独 `diagnose` 生成新证据目录，不覆盖原执行。内存检查点不是持久浏览器恢复。

## 历史兼容

父包把 `legacy/` 加入旧模块搜索路径，保持原 `e2e_agent.domain` 等导入可用于历史记录。新业务代码只依赖 `core/`、配置和通用图桥。不要用 `e2e_agent.legacy.*` 再导入一套类型。

原阶段计划/记录继续作为历史资料；当前门禁见 `UNIVERSAL_REFACTOR.md`，实际结果以本轮 `final.json` 为准。
