# 迁移记录

来源：Nir Diamant / GenAI_Agents，`all_agents_tutorials/e2e_testing_agent.ipynb`。保留 LangGraph、Playwright、自然语言步骤规划、逐步代码生成与最终测试报告。沿用根目录 LICENSE。

## 修复与调整

- 去掉 notebook 安装单元、IPython 展示、ipytest、nest_asyncio 和 Flask 后台进程单元。程序通过 asyncio.run 进入，Flask 生命周期由上下文管理。
- 原版最后一步判断把动作字符串和数字索引比较。本项目在结构化计划中明确标记 assertion，最后一步必须检查结果。
- 原失败报告读取不存在的 actions_taken。本项目统一使用 actions 与 events，异常也生成报告。
- 原测试名称生成提示词未包含实际需求，无法保证 pytest 发现测试。本项目使用确定的 test_generated_scenario 名称。
- 原脚本主要在 notebook 全局命名空间执行。本项目将生成执行放入独立浏览器进程，最终测试用独立 Pytest 进程执行。
- 原版每步从头执行脚本。本项目在生成期间保留浏览器和变量，最终完整重跑一次。
- 使用完整源码结构化导出，不再用正则替换代码标记，避免缩进、替换字符和截断问题。
- 显式导入 expect，拒绝缺少断言的结果检查片段。AST 校验不代表已经证明断言业务正确，也不是安全沙箱。
- 直接读取 JUnit 与进程退出码判定结果，不依赖控制台正则或模型宣告成功。0 用例、跳过、超时均不算通过。
- 最终断言失败也会独立重跑，保留失败证据；中途失败不把未完成片段标为完整测试。
- 示例页面从包内绝对路径读取，支持任意工作目录。保留原 HTML 内容与前端交互，没有引入账号后台。
- 默认 DeepSeek 主模型、千问可选备用。结构化输出显式采用 function_calling，避免兼容服务不支持 json_schema。
- 真实模型复验发现生成了 JavaScript 风格的 toBeVisible。代码提示补充 Python snake_case API 与断言示例后，完整真实模型流程复验通过；最初失败记录仍保留在 runtime，未改为通过。
- 新项目拥有自己的 Python、依赖、Chromium、配置和产物目录。密钥只在本机 .env 中保存，不包含在示例与报告中。

## 当前边界

保留单页面顺序执行与完整 DOM 输入；没有实现自动失败修复、持久化恢复或测试语义正确性证明。模型规划与代码仍可能错误。执行与重跑都可能改变被测网站状态，需使用可重复的测试数据。

安装依赖的锁定版本见 uv.lock。验证结果见 runtime/migration_validation.json；真实模型检查由 scripts/smoke_test.py 显式执行。

## 已完成的验证（2026-10-16）

- 27 个离线测试全部通过，不使用密钥或远程模型。
- 项目专用 Chromium 实际启动通过；采用 chromium channel，有界面和无界面共用同一份安装。
- 固定注册用例在真实浏览器执行成功，独立 Pytest 重跑通过。
- 故意填写不同密码，再检查成功提示的用例，在生成和独立重跑阶段均正确失败；未修改断言使其通过。
- 使用本机模型配置完成结构化规划、逐步代码生成、6 步实际执行及独立 Pytest 重跑。
- 从原仓库工作目录运行新项目 main.py demo 成功；示例服务关闭后通过 replay 再次重跑成功。
- 原注册 HTML 与 LICENSE 字节一致；原 E2E notebook 保持未修改。

离线日志：runtime/unit_tests.log。真实接口与浏览器检查：runtime/smoke_validation.json。以上证明样例流程和约束可运行，不代表已经通过任意网站的泛化评测。
