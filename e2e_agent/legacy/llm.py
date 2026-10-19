"""DeepSeek first, optional Qwen fallback; native async runnables."""

from langchain_core.output_parsers import StrOutputParser
from langchain_openai import ChatOpenAI

from .config import Settings
from .models import TestPlan
from .prompts import CODE_PROMPT, PLAN_PROMPT


class ModelPlanner:
    def __init__(self, settings: Settings):
        settings.require_model()
        common = dict(temperature=0, timeout=settings.request_timeout, max_retries=0)
        providers = []
        if settings.deepseek_api_key:
            providers.append(ChatOpenAI(model=settings.deepseek_model, api_key=settings.deepseek_api_key,
                                        base_url=settings.deepseek_base_url, **common))
        if settings.qwen_api_key:
            providers.append(ChatOpenAI(model=settings.qwen_model, api_key=settings.qwen_api_key,
                                        base_url=settings.qwen_base_url, **common))
        structured = [model.with_structured_output(TestPlan, method="function_calling") for model in providers]
        self.plan_chain = PLAN_PROMPT | self.combine(structured)
        self.code_chain = CODE_PROMPT | self.combine(providers) | StrOutputParser()
        self.max_actions = settings.max_actions

    @staticmethod
    def combine(items):
        return items[0].with_fallbacks(items[1:]) if len(items) > 1 else items[0]

    async def plan(self, query: str, target_url: str) -> TestPlan:
        return TestPlan.model_validate(await self.plan_chain.ainvoke({
            "query": query, "target_url": target_url, "max_actions": self.max_actions,
        }))

    async def code(self, *, query: str, action: dict, html: str, previous_code: str) -> str:
        return await self.code_chain.ainvoke({
            "query": query, "kind": action["kind"], "action": action["description"],
            "html": html, "previous_code": previous_code,
        })


class DemoPlanner:
    """Fixed registration example. Exercises the graph/browser, without model calls."""

    async def plan(self, query: str, target_url: str) -> TestPlan:
        return TestPlan(actions=[
            {"description": "填写测试用户名", "kind": "action"},
            {"description": "填写一致且至少六位的密码", "kind": "action"},
            {"description": "提交注册表单", "kind": "action"},
            {"description": "验证注册成功提示可见且表单隐藏", "kind": "assertion"},
        ])

    async def code(self, *, query: str, action: dict, html: str, previous_code: str) -> str:
        snippets = {
            "填写测试用户名": 'await page.get_by_test_id("usernameInput").fill("demo_user")',
            "填写一致且至少六位的密码": (
                'await page.get_by_test_id("passwordInput").fill("TestPass123!")\n'
                'await page.get_by_test_id("confirmPasswordInput").fill("TestPass123!")'
            ),
            "提交注册表单": 'await page.get_by_test_id("registerButton").click()',
            "验证注册成功提示可见且表单隐藏": (
                'await expect(page.get_by_test_id("successMessage")).to_be_visible()\n'
                'await expect(page.locator("#registrationForm")).to_be_hidden()'
            ),
        }
        return snippets[action["description"]]
