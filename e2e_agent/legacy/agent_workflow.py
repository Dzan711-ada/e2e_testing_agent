"""Native parent workflow with a real execution child and volatile checkpoints."""
import asyncio
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import uuid

from langgraph.graph import END, START, StateGraph

from .actions import observe
from .action_policy import public_action_ids, public_action_locators, safe_validation_message
from .agents import DesignAgent, DesignDecision, ExecutionAgent, RoleModel, action_cost_ledger, validate_design
from .domain import Suite
from .execution_graph import build_execution_graph, materialize_suite, recursion_limit
from .execution_runtime import RunRuntime
from .graph_checkpoint import AsyncGraphSession, AsyncNodeBridge, MemorySaver
from .graph_state import ExecutionState, initial_execution_state, redact_execution_state
from .replay import run_replay
from .sample_shop import CONTRACT, shop_server
from .suite_codegen import export_suite
from .resources import ConsoleProvider, Redactor, ResourceRequest, ResourceUnavailable, ScriptedProvider
from .site_profiles import get_site_profile, profile_contract, profile_for_suite


class ConsoleAnswers:
    def __init__(self, provider=None):
        self.provider = provider or ConsoleProvider()

    async def aanswer(self, questions):
        answers = {}
        for question in questions:
            try:
                request = ResourceRequest(uuid.uuid4().hex, question.id, "clarification", question.text,
                                          "design", question.id, "clarification", sensitive=False)
                value = await self.provider.acquire(request)
            except ResourceUnavailable:
                return None
            if not value:
                return None
            answers[question.id] = value
        return answers


class ScriptedAnswers:
    def __init__(self, answers):
        self.answers, self.requests = list(answers), []

    def answer(self, questions):
        self.requests.append([q.model_dump() for q in questions])
        if len(self.answers) < len(questions):
            return None
        return {q.id: self.answers.pop(0) for q in questions}


def combined_outcome(execution, replay_status):
    original = execution.get("initial_status", execution["status"])
    status = execution["status"] if execution["status"] != "passed" else replay_status
    if (execution["status"] == "passed") != (replay_status == "passed"):
        status = "error"
    return {"status": status, "stability": "consistent" if original == replay_status else "inconsistent",
            "repaired": execution.get("repaired", False), "repair_count": execution.get("repair_count", 0)}


class AgentState(ExecutionState, total=False):
    parent_run_id: str
    query: str
    answers: dict
    version: int
    observations: dict
    decision: dict
    status: str
    phase: str
    rounds: int
    error: str
    replay_result: dict
    previous_replay_run_id: str | None
    complete_script: bool
    diagnostics: dict
    diagnostic_execution: dict
    diagnostic_origin: str
    diagnostic_run_id: str | None
    agent_protocol_version: int
    graph_protocol_version: int
    resume_frozen: bool
    confirmed_sources: dict
    repair_enabled: bool
    repair_count: int
    stability: str
    plan_file: str
    checkpoint_suite: str
    site_profile: str
    final_diagnose_enabled: bool


async def observe_shop(settings, fixture):
    from playwright.async_api import async_playwright
    base, store = fixture
    namespace = "observe_" + uuid.uuid4().hex[:12]
    store.prepare(namespace)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(settings.browsers_path)
    observations = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chromium", headless=settings.headless)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            for path in ("login", "register", "products", "cart", "sms"):
                await page.goto(f"{base}/shop/{namespace}/{path}", wait_until="domcontentloaded", timeout=10000)
                observations[path] = await observe(page)
            await context.close()
        finally:
            await browser.close()
    return observations


async def observe_profile(settings, name):
    """Observe the selected public profile through the same bounded metadata tool."""
    from playwright.async_api import async_playwright
    profile = get_site_profile(name)
    if profile is None:
        raise ValueError("An explicit external site profile is required")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(settings.browsers_path)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chromium", headless=settings.headless)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(profile.login_url, wait_until="domcontentloaded", timeout=15000)
            return {"login": await observe(page)}
        finally:
            await browser.close()


class AgentGraphHost:
    """Immutable contract, model services and live runtime; no execution cursor."""

    def __init__(self, settings, transport, fixture, directory, resource_provider, *,
                 repair=False, frozen_suite=None, confirmed_sources=None, site_profile="local_shop", faults=None):
        self.settings, self.transport, self.fixture = settings, transport, fixture
        self.directory, self.resource_provider = Path(directory), resource_provider
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex
        self.site_profile, self.profile = site_profile, get_site_profile(site_profile)
        # A profile's known resources must be private even before Design has
        # confirmed a Suite and a runtime resource manager exists.
        self._bootstrap_redactor = Redactor()
        if self.profile is not None:
            from .practice_resources import public_demo_resources
            for value in public_demo_resources(self.site_profile).values():
                self._bootstrap_redactor.register(value)
            if hasattr(self.transport, "bind_runtime_redactor"):
                self.transport.bind_runtime_redactor(self._bootstrap_redactor.data)
        if self.profile is not None and faults:
            raise ValueError("Fault injection is restricted to the controlled local fixture")
        self.faults = deepcopy(faults or {})
        self.runtime = self.original_suite = None
        self.sources, self.answers, self.inventory = deepcopy(confirmed_sources or {}), {}, {}
        self.designer, self.executor = DesignAgent(transport), ExecutionAgent(transport)
        self.controller = None
        if repair:
            from .repairs import RepairController
            self.controller = RepairController(settings, transport, self.directory / "repairs")
        self._events = []
        if frozen_suite is not None:
            self.prepare_suite(frozen_suite, self.sources)

    def prepare_suite(self, suite, sources, *, answers=None):
        if self.original_suite is not None:
            if self.original_suite.model_dump() != suite.model_dump():
                raise ValueError("A confirmed execution contract cannot be replaced")
            return
        if suite.budget.max_actions > self.settings.max_actions:
            raise ValueError("Saved plan exceeds the current configured action cap")
        if any(sources.get(r.source.reference, {}).get("kind") != r.source.kind for r in suite.rules):
            raise ValueError("Saved plan has no confirmed source catalog; clarify it instead of inventing rules")
        resources = None
        if self.profile is not None:
            if profile_for_suite(suite) != self.site_profile:
                raise ValueError("The confirmed suite does not match the selected site profile")
            from .practice_resources import public_demo_resources
            resources = public_demo_resources(self.site_profile)
        self.runtime = RunRuntime(suite, self.directory / "generation", self.settings.browsers_path,
            fixture=self.fixture, headless=self.settings.headless, resource_provider=self.resource_provider,
            public_fictional=any(r.kind == "sms_code" for r in suite.resources), source_catalog=sources,
            resources=resources, faults=self.faults)
        # Bind static resources for validation, before the first checkpoint.
        # The runtime has no live browser until runtime_factory is called.
        initial_execution_state(suite, self.runtime.run_id, redact=self.runtime.manager.redactor.data)
        self.original_suite, self.sources = suite.model_copy(deep=True), deepcopy(sources)
        self.answers = deepcopy(answers or {})
        if hasattr(self.transport, "bind_runtime_redactor"):
            self.transport.bind_runtime_redactor(self.runtime.manager.redactor.data,
                public_ids=public_action_ids(suite), public_locators=public_action_locators(suite))

    async def runtime_factory(self, state):
        if self.runtime is None:
            raise ValueError("The execution contract is not confirmed")
        if self.runtime.browser is None:
            await self.runtime.open()
        return self.runtime

    def materialize(self, state):
        if self.runtime is None:
            raise ValueError("No confirmed execution contract is available")
        return materialize_suite(self.runtime, state)

    def safe(self, value):
        if self.profile is not None:
            value = self._bootstrap_redactor.data(value)
        if self.runtime is not None and hasattr(self.runtime, "safe_payload"):
            return self.runtime.safe_payload(value)
        if hasattr(self.transport, "safe_payload"):
            return self.transport.safe_payload(value)
        if self.runtime is not None:
            return self.runtime.manager.redactor.data(value)
        return json.loads(self.settings.redact(json.dumps(value, ensure_ascii=False)))

    def safe_state(self, state):
        if self.runtime is None:
            return self.safe(state)
        result = redact_execution_state(state, self.runtime.manager.redactor.data)
        for key in ("query", "answers", "observations", "decision", "error", "confirmed_sources",
                    "diagnostics", "diagnostic_execution", "replay_result"):
            if key in result:
                result[key] = self.safe({key: result[key]})[key]
        return result

    def write_json(self, filename, value):
        (self.directory / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_plan(self, state, *, complete=False):
        suite = self.materialize(state)
        self.write_json("partial-plan.json", {"artifact_kind": "canonical_plan", "graph_protocol_version": 4,
            "agent_protocol_version": 2, "suite": suite.model_dump(),
            "requirements_fingerprint": self.original_suite.requirements_fingerprint(),
            "answers": deepcopy(self.answers), "confirmed_sources": deepcopy(self.sources),
            "planned_checkpoints": state.get("planned_checkpoints", []), "complete_script": complete,
            "checkpoint_suite": "display_only", "site_profile": self.site_profile})
        if complete:
            self.write_json("plan.json", json.loads((self.directory / "partial-plan.json").read_text(encoding="utf-8")))
        return suite

    async def on_plan_ready(self, state, runtime):
        self.save_plan(state)
        self.write_json(f"action-plan-{state['scene_id']}-{state['checkpoint_id']}.json",
            self.safe({"scenario_id": state["scene_id"], "checkpoint_id": state["checkpoint_id"],
                       "actions": [action for action in state["effective_actions"][state["scene_id"]]
                                   if action["checkpoint_id"] == state["checkpoint_id"]]}))

    async def plan_actions(self, scene, checkpoint, current_observation, *, remaining, redact):
        if len(self.transport.calls) >= min(self.original_suite.budget.max_model_calls, self.transport.limit):
            raise ValueError("Suite model call budget exhausted")
        return await self.executor.plan_checkpoint(suite=self.original_suite, scene=scene,
            checkpoint=checkpoint, observations=self.inventory, current_observation=current_observation,
            remaining=remaining, redact=redact)

    async def close(self, *, clear_resources=False):
        if self.runtime is not None:
            await self.runtime.close(clear_resources=clear_resources)

    def clear_resources(self):
        if self.runtime is not None:
            self.runtime.manager.close()
        self._bootstrap_redactor.clear()

    def record_parent_update(self, node, state):
        self.write_json("agent-state.json", self.safe_state(state))
        event = {"graph_event_index": len(self._events) + 1, "run_id": self.run_id,
            "execution_run_id": state.get("run_id"), "node": node, "phase": state.get("phase"),
            "status": state.get("status"), "execution_status": state.get("execution_status"),
            "replay_status": state.get("replay_result", {}).get("status"),
            "diagnostic_run_id": state.get("diagnostic_run_id")}
        self._events.append(event)
        self.write_json("parent-events.json", self._events)
        with (self.directory / "parent-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_checkpoint(self, snapshot):
        def project(item):
            config = getattr(item, "config", {}) or {}
            return {"values": self.safe_state(dict(item.values)), "next": list(item.next),
                "configurable": {key: value for key, value in config.get("configurable", {}).items()
                                 if key in {"thread_id", "checkpoint_ns", "checkpoint_id"}},
                "tasks": [{"id": task.id, "name": task.name,
                    "error": None if task.error is None else self.safe(str(task.error)),
                    "state": project(task.state) if hasattr(task.state, "values") else self.safe(task.state)}
                    for task in getattr(item, "tasks", ())]}
        view = project(snapshot)
        self.write_json("agent-checkpoint.json", view)
        with (self.directory / "parent-checkpoint-history.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(view, ensure_ascii=False) + "\n")
        return view


def initial_agent_state(query, *, host=None, frozen_suite=None, confirmed_answers=None,
                        confirmed_sources=None, repair=False, final_diagnose_enabled=True):
    state = dict(query=query, answers=deepcopy(confirmed_answers or {}), version=frozen_suite.version if frozen_suite else 1,
        rounds=0, status="running", phase="observe", complete_script=False, agent_protocol_version=2,
        graph_protocol_version=4, resume_frozen=frozen_suite is not None,
        confirmed_sources=deepcopy(confirmed_sources or {}), repair_enabled=repair,
        repaired=False, repair_count=0, stability="unverified", checkpoint_suite="display_only",
        final_diagnose_enabled=final_diagnose_enabled, site_profile=host.site_profile if host else "local_shop")
    if host is not None:
        state["parent_run_id"] = host.run_id
    if frozen_suite is not None:
        if host is None:
            raise ValueError("Frozen graph initialization requires its live host for validation")
        host.prepare_suite(frozen_suite, state["confirmed_sources"], answers=state["answers"])
        host.answers = deepcopy(state["answers"])
        state.update(initial_execution_state(frozen_suite, host.runtime.run_id, redact=host.runtime.manager.redactor.data))
        host.save_plan(state)
    return host.safe_state(state) if host is not None else state


def build_agent_graph(settings, transport, answers_provider, fixture, directory, resource_provider, *,
                      bridge=None, host=None, interrupt_resources=True):
    bridge = bridge or AsyncNodeBridge()
    host = host or AgentGraphHost(settings, transport, fixture, directory, resource_provider)

    async def observe_node(state):
        observation = (await observe_shop(settings, fixture) if host.profile is None else
                       await observe_profile(settings, host.site_profile))
        host.inventory = deepcopy(observation)
        return {"observations": host.safe(observation), "phase": "execute" if state.get("resume_frozen") else "design"}

    async def design_node(state):
        if len(transport.calls) >= transport.limit:
            return {"status": "blocked", "phase": "complete", "error": "Model call budget exhausted"}
        payload = {"query": state["query"], "answers": state["answers"], "observations": state["observations"],
            "source_ids": ["user:request", "contract:shop-v1", *["answer:" + k for k in state["answers"]]],
            "contract": CONTRACT, "max_actions": settings.max_actions, "model_call_limit": transport.limit}
        payload["confirmed_execution_policy"] = {"registration_then_login": "Check registration success, then log in in the same chain when the user requests both",
            "product": "Agent Handbook is the product named in the explicit contract; inventory includes its current article",
            "refresh": "Navigate to the current cart URL again, then check retained product and quantity",
            "checks": "Coordinator executes acceptance checks automatically at the end of each checkpoint; no check actions are needed"}
        payload["execution_action_costs"] = action_cost_ledger(state["query"], state["answers"])
        payload["rule_scope_policy"] = "Each rule is a concrete coverage commitment and must be cited by an acceptance check. Do not copy the whole contract or invent untested SMS lifetime, product or UI rules. The contract supplies sources for the requested checks; login_success_ui supports the login rule, not a separate unreferenced rule. Use separate short rule/checkpoint IDs registration, login, cart, refresh, rejection and authentication for the scopes actually requested. Preserve every user-requested result and both quantity checks."
        payload["sources"] = {"user:request": {"kind": "user", "text": state["query"]},
            "contract:shop-v1": {"kind": "contract", "text": CONTRACT},
            **{"answer:" + key: {"kind": "clarification", "text": value} for key, value in state["answers"].items()}}
        payload["site_profile"] = host.site_profile
        if host.profile is not None:
            # A profile is an explicit bounded contract, never an inferred rule
            # from an untrusted page or a license to widen target origins.
            payload["contract"] = profile_contract(host.site_profile)
            payload["source_ids"] = ["user:request", host.profile.source_id, *["answer:" + k for k in state["answers"]]]
            payload["sources"] = {"user:request": {"kind": "user", "text": state["query"]},
                host.profile.source_id: {"kind": "contract", "text": payload["contract"], "url": host.profile.source_url},
                **{"answer:" + key: {"kind": "clarification", "text": value} for key, value in state["answers"].items()}}
            payload["confirmed_execution_policy"] = {"scope": "The selected profile has two frozen login cases in isolated contexts",
                "checks": "Program assertions run at checkpoint end; use only resource references for input"}
            payload["execution_action_costs"] = action_cost_ledger(state["query"], state["answers"], site_profile=host.site_profile)
            payload["rule_scope_policy"] = "Preserve the selected profile's complete positive and negative login contract; unsupported goals are conflicts"
        decision = await host.designer.design(payload)
        validate_design(decision, state["query"], state["answers"], state["observations"], settings.max_actions, transport.limit,
                        require_actions=False, site_profile=host.site_profile)
        host.write_json(f"design-round-{state['rounds'] + 1}.json", host.safe(decision.model_dump()))
        update = {"decision": host.safe(decision.model_dump()), "rounds": state["rounds"] + 1}
        if decision.status == "ready":
            suite = decision.suite.model_copy(update={"version": state["version"]})
            host.prepare_suite(suite, payload["sources"], answers=state["answers"])
            initial = initial_execution_state(suite, host.runtime.run_id, redact=host.runtime.manager.redactor.data)
            host.save_plan(initial)
            update.update(initial)
            update.update(phase="execute", confirmed_sources=host.safe(payload["sources"]),
                          plan_file=str(host.directory / "partial-plan.json"))
        elif decision.status == "conflict":
            update.update(status="blocked", phase="complete", error="; ".join(decision.conflicts))
        else:
            update["phase"] = "clarify"
        return update

    async def clarify_node(state):
        if state["rounds"] >= 3:
            return {"status": "blocked", "phase": "complete", "error": "Clarification round budget exhausted"}
        questions = DesignDecision.model_validate(state["decision"]).questions
        answers = await answers_provider.aanswer(questions) if hasattr(answers_provider, "aanswer") else await asyncio.to_thread(answers_provider.answer, questions)
        if not answers or any(not str(answers.get(q.id, "")).strip() for q in questions):
            return {"status": "blocked", "phase": "complete", "error": "Required clarification has no answer; no assumptions were substituted"}
        return {"answers": {**state["answers"], **answers}, "version": state["version"] + 1, "phase": "design"}

    async def export_node(state):
        await host.close(clear_resources=False)
        suite = host.save_plan(state)
        execution = deepcopy(state["execution"])
        execution["model_calls"] = len(transport.calls)
        execution["timing"] = {"active_seconds": host.runtime.clock.active_seconds, "human_wait_seconds": host.runtime.clock.wait_seconds}
        (host.directory / "generation" / "suite-result.json").write_text(json.dumps(execution, ensure_ascii=False, indent=2), encoding="utf-8")
        missing = [(scene.id, cp.id) for scene in suite.scenarios for cp in scene.checkpoints
                   if not any(a.checkpoint_id == cp.id for a in scene.actions)]
        if missing:
            return {"execution": execution, "phase": "analyze", "complete_script": False,
                "status": execution["status"] if execution["status"] != "passed" else "error",
                "error": "Execution stopped before every required action batch was generated; partial artifact only",
                "plan_file": str(host.directory / "partial-plan.json")}
        validate_design(DesignDecision(status="ready", suite=suite), host.original_suite.query,
            host.answers, host.inventory, settings.max_actions, transport.limit, site_profile=host.site_profile)
        if execution.get("repaired"):
            original = suite.model_dump()
            for repair in execution.get("repair_history", []):
                if repair.get("decision") == "approved" and repair.get("execution_status") == "passed":
                    added = {action["id"] for action in repair.get("proposed_plan", {}).get("proposed_actions", [])
                             if action["id"] != repair["action_id"]}
                    scene = next(item for item in original["scenarios"] if item["id"] == repair["scenario_id"])
                    scene["actions"] = [action for action in scene["actions"] if action["id"] not in added]
            for failed in execution.get("initial_failures", []):
                actions = next(s["actions"] for s in original["scenarios"] if s["id"] == failed["scenario_id"])
                for index, action in enumerate(actions):
                    if action["id"] == failed["action"]["id"]:
                        actions[index] = deepcopy(failed["action"])
            host.write_json("original-plan.json", original)
        export_suite(suite, host.directory, settings.browsers_path, headless=settings.headless,
                     public_fictional=any(r.kind == "sms_code" for r in suite.resources))
        host.save_plan(state, complete=True)
        return {"execution": execution, "phase": "replay", "complete_script": True, "plan_file": str(host.directory / "plan.json")}

    async def replay_node(state):
        previous = host.directory / "replay-suite/suite-result.json"
        previous_id = None
        if previous.is_file():
            try:
                previous_id = json.loads(previous.read_text(encoding="utf-8")).get("run_id")
            except (ValueError, OSError):
                pass
        try:
            replay = (await run_replay(settings, host.directory, resource_provider=resource_provider)).model_dump()
        except Exception as error:
            replay = {"status": "error", "detail": safe_validation_message(error)}
        return {"replay_result": host.safe(replay), "previous_replay_run_id": previous_id, "phase": "analyze"}

    async def analyze_node(state):
        execution, replay = state.get("execution"), state.get("replay_result")
        outcome = combined_outcome(execution, replay["status"]) if replay else {
            "status": state.get("status") if state.get("status") != "running" else execution["status"],
            "repaired": execution.get("repaired", False), "repair_count": execution.get("repair_count", 0), "stability": "unverified"}
        diagnostic, origin = None, "generation"
        if replay and replay["status"] != "passed":
            origin = "replay"
            path, latest = host.directory / "replay-suite/suite-result.json", host.directory / "replay-suite/execution-state.json"
            if path.is_file():
                candidate = json.loads(path.read_text(encoding="utf-8"))
                current = json.loads(latest.read_text(encoding="utf-8")) if latest.is_file() else {}
                if candidate.get("requirements_fingerprint") == host.original_suite.requirements_fingerprint() and (
                    candidate.get("run_id") and candidate.get("run_id") != state.get("previous_replay_run_id")
                    and current.get("run_id") == candidate.get("run_id")
                    and candidate.get("status") in {"failed", "error", "blocked", "cancelled"}):
                    diagnostic = candidate
            if diagnostic is None:
                diagnostic = {"schema_version": 2, "run_id": None, "suite_id": host.original_suite.id,
                    "status": "error", "scenarios": {}, "planned_scenarios": [s.id for s in host.original_suite.scenarios],
                    "requirements_fingerprint": host.original_suite.requirements_fingerprint(),
                    "error": "Replay produced no verified current structured failure record; investigate pytest or replay transport",
                    "replay_detail": replay.get("detail", ""),
                    "replay_artifacts": [str(host.directory / "pytest.log"), str(host.directory / "junit.xml")]}
        elif execution["status"] in {"failed", "error"}:
            diagnostic = deepcopy(execution)
        if diagnostic is not None:
            if not state.get("final_diagnose_enabled", True):
                return {**outcome, "phase": "complete", "diagnostics": {"status": "disabled", "automatic_repairs": 0,
                    "reason": "Final diagnosis is explicitly disabled for the recorded evaluation arm"}}
            diagnostic["source_catalog"] = host.safe({"source_catalog": host.sources})["source_catalog"]
            diagnostic["sanitized_plan"] = host.safe({"suite": host.materialize(state).model_dump()})["suite"]
            return {**outcome, "phase": "final_diagnose", "diagnostic_execution": host.safe({"execution": diagnostic})["execution"],
                    "diagnostic_origin": origin, "diagnostic_run_id": diagnostic.get("run_id")}
        return {**outcome, "phase": "complete"}

    async def final_diagnose_node(state):
        from .diagnostics import diagnose_execution
        if len(transport.calls) >= min(host.original_suite.budget.max_model_calls, transport.limit):
            report = {"status": "blocked", "attribution": "unknown", "error": "Suite model call budget exhausted",
                      "automatic_repairs": 0, "execution_status": state["diagnostic_execution"]["status"]}
        else:
            report = await diagnose_execution(settings, host.materialize(state), state["diagnostic_execution"],
                host.directory / "diagnostics" / uuid.uuid4().hex[:8], transport=transport)
        report.update(origin=state["diagnostic_origin"], execution_run_id=state.get("diagnostic_run_id"))
        return {"diagnostics": host.safe(report), "phase": "complete"}

    def wrapped(name, function):
        async def safe_node(state):
            try:
                return host.safe_state(await function(state))
            except Exception as error:
                return {"status": "error", "phase": "complete",
                        "error": host.safe(f"{type(error).__name__}: {safe_validation_message(error)}")}
        safe_node.__name__ = name
        return bridge.node(safe_node)

    graph = StateGraph(AgentState)
    for name, function in {"observe": observe_node, "design": design_node, "clarify": clarify_node,
            "export": export_node, "replay": replay_node, "analyze": analyze_node, "final_diagnose": final_diagnose_node}.items():
        graph.add_node(name, wrapped(name, function))
    graph.add_node("execute", build_execution_graph(runtime_factory=host.runtime_factory, bridge=bridge,
        action_planner=host.plan_actions, repair_controller=host.controller,
        interrupt_resources=interrupt_resources, on_plan_ready=host.on_plan_ready))
    graph.add_edge(START, "observe")
    graph.add_conditional_edges("observe", lambda s: END if s["phase"] == "complete" else s["phase"],
                                {"design": "design", "execute": "execute", END: END})
    for name in ("design", "clarify"):
        graph.add_conditional_edges(name, lambda s: END if s["phase"] == "complete" else s["phase"],
                                    {"design": "design", "clarify": "clarify", "execute": "execute", END: END})
    graph.add_edge("execute", "export")
    graph.add_conditional_edges("export", lambda s: END if s["phase"] == "complete" else s["phase"],
                                {"replay": "replay", "analyze": "analyze", END: END})
    graph.add_conditional_edges("replay", lambda s: END if s["phase"] == "complete" else "analyze",
                                {"analyze": "analyze", END: END})
    graph.add_conditional_edges("analyze", lambda s: "final_diagnose" if s["phase"] == "final_diagnose" else END,
                                {"final_diagnose": "final_diagnose", END: END})
    graph.add_edge("final_diagnose", END)
    return graph.compile(checkpointer=MemorySaver())


async def run_agent_suite(settings, *, query, run_dir=None, answers_provider=None, transport=None, resource_provider=None,
                          frozen_suite=None, confirmed_answers=None, confirmed_sources=None, repair=False,
                          site_profile="local_shop", final_diagnose_enabled=True, faults=None):
    if not query.strip():
        raise ValueError("A test goal is required")
    directory = (run_dir or settings.runtime_path / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-agents-" + uuid.uuid4().hex[:8])).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    profile = get_site_profile(site_profile)
    owns_resource_provider = resource_provider is None
    if resource_provider is None and profile is not None:
        from .practice_resources import public_demo_resources
        resource_provider = ScriptedProvider(public_demo_resources(site_profile))
    model, provider = transport, resource_provider or ConsoleProvider(allow_test_delivery=True)
    state, host, session, config = {"status": "running", "complete_script": False}, None, None, None
    try:
        model = transport or RoleModel(settings, audit_path=directory / "model-calls.json")
        with (shop_server() if profile is None else nullcontext((None, None))) as fixture:
            host = AgentGraphHost(settings, model, fixture, directory, provider, repair=repair, confirmed_sources=confirmed_sources,
                                  site_profile=site_profile, faults=faults)
            state = initial_agent_state(query, host=host, frozen_suite=frozen_suite, confirmed_answers=confirmed_answers,
                                        confirmed_sources=confirmed_sources, repair=repair,
                                        final_diagnose_enabled=final_diagnose_enabled)
            bridge = AsyncNodeBridge()
            graph = build_agent_graph(settings, model, answers_provider or ConsoleAnswers(provider if isinstance(provider, ConsoleProvider) else None),
                fixture, directory, provider, bridge=bridge, host=host)
            session = AsyncGraphSession(graph, bridge)
            config = {"configurable": {"thread_id": host.run_id},
                "recursion_limit": recursion_limit(frozen_suite) + 24 if frozen_suite else 128 + settings.max_actions * 10}
            value = state
            # Only native pause pumping lives outside the graph. Scene,
            # checkpoint, action, repair and outcome routing are graph edges.
            while True:
                async for namespace_update in session.astream(value, config, stream_mode="updates", subgraphs=True):
                    namespace, update = namespace_update
                    if not namespace:
                        for node, values in update.items():
                            if values:
                                state.update(values)
                                host.record_parent_update(node, state)
                                print(f"Agent {node}: {state.get('phase')}", flush=True)
                snapshot = await session.aget_state(config, subgraphs=True)
                host.record_checkpoint(snapshot)
                state.update(snapshot.values)
                if not snapshot.next:
                    break
                value = None
    except Exception as error:
        state.update(status="error", phase="complete", error=safe_validation_message(error))
    except BaseException:
        state.update(status="cancelled", error="Run interrupted; browser and local service are closed")
        if session is not None and config is not None:
            try:
                host.record_checkpoint(await session.aget_state(config, subgraphs=True))
            except Exception:
                pass
        raise
    finally:
        try:
            if host is not None:
                try:
                    await host.close(clear_resources=False)
                except Exception as error:
                    if state.get("status") != "cancelled":
                        state.update(status="error", error=host.safe(safe_validation_message(error)))
                if host.original_suite is not None:
                    # A parent paused inside its native child still has the
                    # pre-child values. Never overwrite a newer validated
                    # batch artifact with that old input on cancellation.
                    plan_path = directory / ("plan.json" if state.get("complete_script") else "partial-plan.json")
                    if state.get("execution") is not None:
                        canonical = host.save_plan(state, complete=state.get("complete_script", False))
                    else:
                        artifact = json.loads(plan_path.read_text(encoding="utf-8"))
                        canonical = Suite.model_validate(artifact["suite"])
                        if canonical.requirements_fingerprint() != host.original_suite.requirements_fingerprint():
                            raise ValueError("Saved canonical plan changed protected requirements")
                    state["suite"], state["plan_file"] = canonical.model_dump(), str(plan_path)
            state.update(run_dir=str(directory), model_calls=getattr(model, "calls", []))
            if "replay_result" in state:
                # LangGraph 0.2 forbids a channel named like its replay node.
                # Preserve the legacy Python/JSON result field at the boundary.
                state["replay"] = deepcopy(state["replay_result"])
            saved = host.safe_state(state) if host is not None else json.loads(settings.redact(json.dumps(state, ensure_ascii=False)))
            (directory / "agent-result.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            from .suite_reporting import write_suite_report
            sidecars = {}
            replay_path = directory / "replay-suite/suite-result.json"
            if host is not None and replay_path.is_file():
                replay_record = json.loads(replay_path.read_text(encoding="utf-8"))
                current_path = directory / "replay-suite/execution-state.json"
                current = json.loads(current_path.read_text(encoding="utf-8")) if current_path.is_file() else {}
                if (replay_record.get("run_id") and replay_record.get("run_id") == current.get("run_id")
                        and replay_record.get("run_id") != state.get("previous_replay_run_id")
                        and host.original_suite is not None
                        and replay_record.get("requirements_fingerprint") == host.original_suite.requirements_fingerprint()):
                    sidecars["safe_replay_execution"] = host.safe({"execution": replay_record})["execution"]
            for key, filename in (("safe_resource_events", "generation/resource-events.json"),
                                  ("safe_replay_resource_events", "replay-resource-events.json")):
                path = directory / filename
                if host is not None and path.is_file():
                    sidecars[key] = host.safe(json.loads(path.read_text(encoding="utf-8")))
            write_suite_report(directory, saved, **sidecars)
        except Exception:
            # Preserve the caller's cancellation even if a report write or
            # artifact read fails; the nested finalizer still clears secrets.
            if state.get("status") != "cancelled":
                raise
        finally:
            try:
                if host is not None:
                    host.clear_resources()
            except Exception:
                if state.get("status") != "cancelled":
                    raise
            finally:
                if owns_resource_provider:
                    provider.closed = True
    return state
