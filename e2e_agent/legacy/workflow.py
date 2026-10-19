"""LangGraph migration of the notebook's plan/generate/execute/report workflow."""

from pathlib import Path
from typing import Literal

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .codegen import render_test, validate_snippet
from .models import TestPlan


class TestState(TypedDict, total=False):
    query: str
    target_url: str
    sample_site: bool
    actions: list[dict]
    index: int
    snippets: list[str]
    current_code: str
    html: str
    events: list[dict]
    status: Literal["running", "passed", "failed", "error"]
    phase: str
    error: str
    replay: dict


def build_graph(settings, planner, browser, run_dir: Path, replay_runner):
    async def plan(state):
        output = TestPlan.model_validate(await planner.plan(state["query"], state["target_url"]))
        if len(output.actions) > settings.max_actions:
            raise ValueError(f"规划超过 MAX_ACTIONS={settings.max_actions}。")
        return {"actions": [a.model_dump() for a in output.actions], "phase": "navigate"}

    async def navigate(state):
        result = await browser.start(state["target_url"])
        if not result.ok:
            return {"status": "error", "error": result.error, "phase": "navigate"}
        (run_dir / "page-00.html").write_text(result.html, encoding="utf-8")
        return {"html": result.html, "phase": "generate"}

    async def generate(state):
        action = state["actions"][state["index"]]
        code = await planner.code(query=state["query"], action=action, html=state["html"],
                                  previous_code="\n\n".join(state["snippets"]))
        return {"current_code": code, "phase": "validate"}

    def validate(state):
        action = state["actions"][state["index"]]
        try:
            code = validate_snippet(state["current_code"], assertion=action["kind"] == "assertion")
            return {"current_code": code, "phase": "execute"}
        except (SyntaxError, ValueError) as error:
            return {"status": "error", "error": str(error), "phase": "validate"}

    async def execute(state):
        index = state["index"]
        action = state["actions"][index]
        result = await browser.execute(state["current_code"], index + 1, action["kind"] == "assertion")
        event = {"step": index + 1, **action, "ok": result.ok, "error": result.error,
                 "error_type": result.error_type, "screenshot": result.screenshot}
        update = {"events": [*state["events"], event], "html": result.html,
                  "snippets": [*state["snippets"], state["current_code"]], "index": index + 1}
        (run_dir / f"page-{index + 1:02d}.html").write_text(result.html, encoding="utf-8")
        if not result.ok:
            # A final assertion failure is replayed independently, never weakened.
            complete_assertion = index == len(state["actions"]) - 1 and action["kind"] == "assertion"
            if complete_assertion and result.error_type == "AssertionError":
                update.update(phase="export", error=result.error)
            else:
                update.update(status="error", phase="execute", error=result.error)
        else:
            update["phase"] = "export" if index + 1 == len(state["actions"]) else "generate"
        return update

    def export(state):
        script = render_test(target_url=state["target_url"], snippets=state["snippets"],
                             headless=settings.headless, action_timeout_ms=settings.action_timeout_ms,
                             browsers_path=settings.browsers_path)
        (run_dir / "test_generated.py").write_text(script, encoding="utf-8")
        return {"phase": "run_replay"}

    async def replay(state):
        result = await replay_runner(settings, run_dir)
        if result.status == "passed" and any(not e["ok"] for e in state["events"]):
            return {"status": "error", "replay": result.model_dump(), "phase": "complete",
                    "error": "生成阶段断言失败，但独立重跑通过；结果不一致，请查看两次执行轨迹。"}
        return {"status": result.status, "replay": result.model_dump(), "phase": "complete",
                "error": result.detail or state.get("error", "")}

    def route(state):
        return END if state["status"] == "error" else state["phase"]

    graph = StateGraph(TestState)
    for name, node in dict(plan=plan, navigate=navigate, generate=generate, validate=validate,
                           execute=execute, export=export, run_replay=replay).items():
        graph.add_node(name, node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "navigate")
    for name in ("navigate", "generate", "validate", "execute"):
        graph.add_conditional_edges(name, route)
    graph.add_edge("export", "run_replay")
    graph.add_edge("run_replay", END)
    return graph.compile()
