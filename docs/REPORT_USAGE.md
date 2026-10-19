# 统一套件报告

本模块为三 Agent / 结构化套件路径提供统一 Markdown 和本地 HTML 报告，保留 `report.md` 文件名，新增 `report.html` 与最小渲染输入 `report-input.json`。旧 URL / demo 的 `reporting.py` 不在此模块内改写。

报告是原执行记录的视图，不重新判定测试，不调用模型，不把诊断文字替代断言，也不将“预期故障被正确识别”改写为业务通过。G7 评测的 `evaluation_valid`、重复结果和消融比较应保存在独立评测 JSON 中，不能混入单次运行 `status`。

## 输入与 API

输入沿用当前安全 `agent-result.json` 及现有 sidecar 结构。不要把 `run_agent_suite` 的未投影 Python 返回值或 canonical 计划直接传入：该返回值中的 `suite` 可保留原公开业务语义，而报告只能接收经过既有资源脱敏边界的展示数据。

```python
from e2e_agent.suite_reporting import (
    render_suite_report, write_suite_report, is_generated_report,
)

markdown, local_html = render_suite_report(
    safe_result,
    safe_replay_execution=None,
    safe_resource_events=(),
    safe_replay_resource_events=(),
    safe_manual_evidence=None,
    artifact_root=None,
)

paths = write_suite_report(
    run_directory,
    safe_result,
    safe_replay_execution=safe_current_replay_record,
    safe_resource_events=safe_first_resource_events,
    safe_replay_resource_events=safe_replay_resource_events,
)
# paths: {"markdown": ".../report.md", "html": ".../report.html",
#         "input": ".../report-input.json"}
```

调用者在资源管理器清理前完成脱敏，例如使用已有 `host.safe_state` / `host.safe`。调用者负责 sidecar 确属当前 run；模块还会拒绝用与首次重复 run ID 或不同需求指纹的重跑结构化记录逐项对照。没有重跑明细时，只展示已有 Pytest 汇总并明确“逐检查点记录不足”，不会补造通过。

模块仅使用 Python 标准库，不自动读取 canonical 计划，不扫描任意运行目录或旧报告，不嵌入整个输入 JSON。资源的 `value`、`delivery_hint`、缓存、原始 DOM、模型原始完成文本和浏览器对象均不是报告字段。必须传入 JSON 数据；真实私密值在被选用的自由文本中仍必须由调用者先脱敏，本模块不会自称通用秘密识别器。

## 展示内容

HTML 的固定 section ID 可供验证工具核对：

| ID | 内容 |
| --- | --- |
| `summary` | 套件、版本、父/首次/重跑 run、原始和最终状态、稳定性、完整脚本、修复/调用/动作记录数 |
| `scenarios` | 业务链、依赖场景、两轮状态及 context 标识 |
| `checkpoints` | 检查点依赖、两轮最终状态、差异、阻塞/错误原因与遮罩截图链接 |
| `assertions` | 原记录的预期、实际、断言状态、需求引用、证据 ID 和条件指纹 |
| `coverage` | 规则来源、计划断言数、已记录/通过数与缺失情况 |
| `human` | 确认答案、主动/等待时间、实际人工证据是否明确提供 |
| `resources` | 首次/重跑资源请求的公开引用、scene/step、状态和 challenge/expiry |
| `blocked` | 阻塞、失败和未完成原因，保留前置依赖 |
| `initial_failures` | 修复前原始失败动作、目标与错误 |
| `repairs` | 每次提案、审核、执行结果、诊断归因、修复目标与停止原因 |
| `diagnosis` | 原诊断状态/归因、来源 run、证据/反证、不确定性及调查建议 |
| `replay` | 独立 Pytest 用例数、失败/错误/跳过/阻塞、退出码和说明 |
| `artifacts` | 受控当前运行目录内实际存在的证据和脚本链接 |

需求覆盖按计划的 `rule_id / target / operator / expected` 关联已记录断言。失败断言属于已执行覆盖，但不是通过；未执行或缺少匹配记录的检查不算通过。若原状态为通过而必需证据不足，报告提示核对原证据，仍不修改原状态。覆盖展示不证明规则自身完整或正确。

人工资源请求和等待秒数不等于真实人工参与。普通运行没有验收 sidecar 时明确显示来源未证明；只有调用者提供既有实际人工证据（`actual_console_input`、`ConsoleProvider`、父控制台、有界面等字段）时才展示相应证据状态。脚本化 provider 或固定答案不冒称真人。

## HTML 与证据扫描边界

HTML 不加载 JavaScript、外部 CSS、图像或网页；内容均通过 `html.escape`，表格的动态文本不作为标签/属性/脚本执行。固定 CSP 禁止脚本和外部资源。截图以链接方式提供，不嵌入原 DOM；链接只允许当前运行目录内存在的受控本地文件，拒绝脚本/data/file URL、远端地址、路径穿越和 SVG/任意 HTML 截图。

`report-input.json` 是固定 `e2e-suite-report` v1 格式的规范化展示视图：仅段落、单元格和受控链接，不是完整 agent-result 或执行计划。HTML 使用确定性字节输出，不包含时间戳或隐藏原始 JSON。

`is_generated_report(path)` 仅接受精确命名的 `report.html`，要求同目录存在格式正确的 `report-input.json`，校验所有 section 和本地链接，重新转义并生成 HTML，然后与现有文件逐字节比较。复制原 DOM 改名、加 marker、改 HTML、缺失/伪造格式输入或脚本 URL 都不能仅凭文件名得到豁免。即使攻击者在合法文本单元格中加入标签，重新渲染仍只得到转义文本。

这项检查只验证“是程序生成的安全报告格式”，不验证业务结果真实性，也不替代秘密扫描。资源隐私扫描只能对 `is_generated_report` 已验证的这个程序 HTML 作精确例外，其余原始 HTML/DOM/trace 继续拒绝；`report-input.json`、HTML、Markdown 和其他工件仍全部执行原私密哨兵/API key 扫描。

`write_suite_report` 只创建或更新上述三个报告工件，不改 `agent-result.json`、canonical 计划、截图、动作状态或历史证据。它不迁移/删除旧产物，也不提供聊天网页前端。

## 验证范围

离线测试使用现有 R4 首次/重跑/修复记录的可移植小型投影，覆盖状态不变、失败断言覆盖、缺失证据提示、取消/阻塞、人工与脚本化输入区分、重跑 run/指纹隔离、HTML/Markdown 注入、本地链接约束和精确生成报告识别。另可只读复用已有真实安全记录生成报告样本；不为报告重复调用浏览器或模型。

本文件说明报告模块边界；G7 完整交付仍需把报告接入最终生产入口并通过同源码完整阶段门槛，不能以纯报告测试代替 G7 验收。
