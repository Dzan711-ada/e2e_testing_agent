# G7 整体验收与交付约定

用户已授权 G7。本文件规定本阶段实现与验收范围，不表示 G7 已通过；最终结论以绑定最终源码的 `runtime/development/stage-7/.../verification.json` 为准。

## 输入边界

- 上一阶段：`runtime/development/refactor/R5-20261017-181637-da9c79ea/verification.json`，必须保持真实通过记录、完整源码快照、全部 151 个测试身份和当次实际控制台两轮证据。
- G7 开发前快照：`runtime/development/stage-7/20261018-002329-baseline-1f2ed749/baseline.json`，状态为 `captured_not_gate_passed`。91 份源码与通过的 R5 一致；快照不是 G7 通过证据。
- 保留开始前用户对 IDE 配置的修改。最终验证以开始时当前 `.env`、`.idea`、`.run` 为保护基准，记录与历史差异，不恢复用户配置。
- 不重跑或更改历史 R5 的验收状态。G7 使用独立新目录、新完整源码 zip、逐文件摘要和最终配置/锁文件前后保护。

## 固定重复对照

`scripts/verify_comparison.py` 的输入版本为 1。模型服务、模型名、服务顺序、温度 0、请求超时、动作预算、每轮 20 次物理服务尝试上限等固定并留存；服务端仍可能不确定，不能把温度 0 称为可复现随机种子。端点仅记录摘要，不保存密钥。

| 样本 | 固定来源与输入 | 验证范围 |
| --- | --- | --- |
| `clear-goal` | 原 `verify_agents.INPUTS` 明确目标原文 | 真实 Design 与 Execution；每轮生成计划独立留存，三臂不能删除原需求 |
| `stale-target` | 原 `verify_repairs.case_suite` 失效定位器；冻结完整 Suite | 比较诊断与等价动作恢复，不作为 Design 能力样本 |
| `product-valid-login-denied` | 原完整 Suite 与 `valid_login_denied` 隐藏故障 | 正确凭据被拒绝的真实产品故障，不能由修复改成通过 |

故障样本在任何运行开始前统一补齐当前来源和字段角色协议，冻结同一 Suite、需求/输入、资源生命周期、预算和故障。各臂与各次重复必须使用相同原始输入摘要；失败后不修改输入。故障开关只进入运行服务与受控商城，不进入图状态或模型载荷。

三个可比配置为：

1. `planning_execution`：规划/执行；关闭最终诊断与修复，图状态明确诊断禁用，不能内部偷偷调用诊断模型。
2. `with_diagnosis`：增加最终诊断，仍不修复。
3. `with_repair`：诊断、Execution 修复提议、确定性程序批准和执行回边。

每个样本、每种配置都执行两次，共 18 次运行。固定样本顺序，每个样本第一次按配置正序，第二次反序；每轮独立目录、浏览器上下文与资源 run。全部成功、错误、拒绝、预算耗尽和模型服务尝试均留存，不选择最好一轮、不删除失败。最坏服务尝试总上限为 18×20=360；实际调用、用量（不可用时保留缺失）、耗时和备用服务差异逐轮记录。完整 G6 及公开站专项的调用另计。

原 S0 是单注册页的自然语言动作/代码协议，缺少商店 Suite、多场景与资源/故障契约。在这三个商店样本上明确标为 `n/a`，不能伪造横向提升。当前完整 G6 的原正常/故意失败固定兼容流程单列为参考；它们不是原 S0 模型消融，也不进入商店对照分母。

### 重复评测验收

- 正常样本每种配置的两次运行均须首次及独立重跑通过，实际调用 Design 和 Execution，脚本完整，无需修复。
- 失效定位器在未启用修复时不得误报通过；诊断配置须正确归因为 `agent_execution`。修复配置两次均须经实际图程序校验、执行语义等价动作、保留首次失败和原计划，原需求与保护输入不变，独立无模型重跑通过。
- 产品故障的三种配置均须首次业务断言失败，综合结果不得通过；诊断与修复配置须保留 `suspected_product`，不能自动修复。故障只作用于生成运行，独立重跑遇到健康实例可能成功，此时综合结果仍保留错误和不一致性。
- 缺样、重复样、输入或模型配置漂移、模型/动作预算越界、禁用角色被调用、秘密进入载荷或不等价修复均使本专项失败。所有 18 行仍留在结果中。
- 输出 `comparison-inputs.json` 与 `comparison-validation.json`。结果逐样本/配置列出两次观测、状态、归因、调用和耗时，不合并成任意网站总体准确率，不声称统计显著性或未使用的保留集表现。

## 公开练习站与报告

选定 `https://the-internet.herokuapp.com/login`。同一生产图 profile 固定普通正确登录与错误密码拒绝两例，各使用独立空上下文；冻结具体目标、来源、检查和资源协议。先验证本地协议镜像，再实际尝试公网；本地路由重写只是协议验证，不能冒充公网成功。

`public-site-validation.json` 必须明确本地与公网各自状态及两个精确身份 `valid-login`、`invalid-password`。只有实际公网两例通过才可记录 `compatibility_verified=true`。公网外部不可达时专项为 `blocked_public`、退出码 2；G7 继续完成本地评测与报告留证，但总体为 `blocked`，不得改成可选、不得进入人工后标记通过。

报告复用当次真实 `agent-result.json`、检查点、来源覆盖、首次/重跑、等待资源和修复历史，不改业务 JSON 或原状态。Markdown/HTML 包含 summary、scenarios、coverage、human、diagnosis、repairs、replay、artifacts；图和断言证据缺失、被阻塞、模型未知均明确保留。

`scripts/verify_delivery_reports.py` 在自动阶段消费本次 G6、18 轮对照和本地/公网产物。控制台人工通过后单独生成新的 `manual-reports`，不覆盖已冻结的自动报告。`report.html` 只有在与同目录规范化 `report-input.json` 确定性重算逐字节一致时才是受控报告；其余原始 DOM HTML、HAR 和 trace 仍受禁止，资源秘密哨兵检查保持。旧入口兼容产物的隐私范围单列，不能把新结构化路径的扫描标准冒称覆盖了历史自由代码入口。

## G7 最终门槛

`scripts/verify_delivery.py` 提供 `verify(baseline_path, previous_path=None, manual_path=None, wait_for_manual=0)`；`verify_stage.py --stage 7` 仅转发本阶段，不改变原 G6 实现。

1. 严格核对已通过 R5 的原始记录、完整 source.zip、自动证据摘要、全部测试身份及历史人工证据。历史 CORE 按其原 SHA 和 R5 源码清单验证；不能要求旧 R5 符合 G7 新 CORE，也不能修改旧人工记录。
2. 在当前最终源码上直接调用原 `scripts.verify_stage.verify(6, 原S0baseline)` 完整重跑原 17 项 G6 命令。原 S0 仍由历史通过 G6 的精确 baseline 路径及 SHA 绑定，不替换起点。
3. R5 的全部测试身份保留，零跳过；新增报告、公开站 profile 和重复评测离线测试身份强制存在。PyCharm 入口、非项目目录、旧示例兼容、依赖锁和所有前序专项仍须通过。
4. 新执行图 4 项、原生资源图 7 项、父子图 7 项重新运行；父子图引用本次 G6 新角色/修复产物，不能用旧 R4/R5 记录代替。
5. 全部 18 轮真实重复对照、公网站实际验证及当次报告集成通过。API 密钥、私密资源、原始页面证据和输入来源保护继续有效。
6. 固定自动证据清单涵盖本次完整 G6、三个图专项、对照、公开站、报告、命令日志与最终源码 zip；不包含会继续更新的本次 `verification.json`。验证期间源代码、配置和锁文件不得变化。
7. 自动全部通过后，仅记录 `automatic_status=passed/status=waiting_user`。使用当前 `scripts/manual_sms.py` 有界面真实 ConsoleProvider 首次运行及独立重跑，各输入当次新手机号/验证码；人工开始时间不得早于本次 G7。
8. 当前 `verify_stage.verify_manual`、当前 CORE 签名与最终完整源码清单共同验证新人工证据。随后在独立 `manual-reports` 生成真实人工报告并绑定摘要，再次核验全部自动证据与源码完整性，最后才能 `status=passed`。

命令可直接使用：

```text
python scripts/verify_delivery.py --baseline runtime/development/stage-7/20261018-002329-baseline-1f2ed749/baseline.json --wait-for-manual 0
python scripts/manual_sms.py --output <新的人工目录>
python scripts/verify_delivery.py --resume-final <本次verification.json> --manual-evidence <新的manual-evidence.json>
```

未提供新人工证据时保持 `waiting_user`，退出码非成功。续接只读取并重新校验已冻结的全部自动产物、旧 R5 边界、当前源码、配置与锁，不重跑模型，也不重建 source.zip 或证据清单以接纳新源码。任何变化须完整重新验收。所有失败尝试独立保留；交付结论和剩余范围以最终同一源码的机器记录为准。
