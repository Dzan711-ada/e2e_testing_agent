"""Live browser/resource services; business progress belongs to the caller.

No scene, checkpoint, action or repair scheduling is performed here. Objects in
this module are deliberately process-local and must not be checkpointed.
"""
from contextlib import AsyncExitStack, nullcontext
from dataclasses import dataclass
import json
import os
from pathlib import Path
import uuid

from playwright.async_api import async_playwright

from .actions import ActionExecutor, observe
from .action_policy import public_action_ids, public_action_locators, redact_role_payload
from .evidence import actual_condition, condition_fingerprint, public_url
from .resource_bridge import ReplayProvider
from .resources import ActiveClock, ConsoleProvider, ResourceManager
from .sample_shop import CONTRACT, shop_server


def aggregate(statuses):
    statuses = list(statuses)
    if not statuses:
        return "error"
    for status in ("error", "failed", "cancelled", "blocked"):
        if status in statuses:
            return status
    return "passed" if all(status == "passed" for status in statuses) else "error"


@dataclass
class SceneSession:
    scene_id: str
    namespace: str
    context_id: str
    context: object
    page: object
    executor: ActionExecutor
    network: list


class RunRuntime:
    """Own live handles and secrets without owning execution cursors or retries."""

    def __init__(self, suite, output_dir: Path, browsers_path: Path, *, resources=None,
                 fixture=None, faults=None, headless=True, resource_provider=None,
                 resource_wait_timeout=120, public_fictional=False,
                 external_resources=(), source_catalog=None):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path)
        self.suite, self.output_dir = suite, Path(output_dir)
        from .site_profiles import profile_for_suite
        self.site_profile = profile_for_suite(suite)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex[:12]
        self.clock = ActiveClock()
        self.values = {"username": "stage_user", "password": "TestPass123!", "wrong_password": "Wrong123!"} if suite.fixture == "local_shop" else {}
        self.values.update(resources or {})
        self.specs_by_id = {resource.id: resource for resource in suite.resources}
        self.external = set(external_resources) | {
            resource.id for resource in suite.resources
            if resource.acquisition == "runtime" and resource.kind in {"account", "password"}
        } | {
            action.resource_id for scene in suite.scenarios for action in scene.actions
            if action.kind == "request_resource" and self.specs_by_id[action.resource_id].kind in {"account", "password"}
        }
        for identity in self.external:
            if identity not in (resources or {}):
                self.values.pop(identity, None)
        provider = resource_provider or (ReplayProvider() if os.environ.get("E2E_RESOURCE_BRIDGE_ADDRESS") else ConsoleProvider(allow_test_delivery=True))
        self.manager = ResourceManager(suite.resources, self.run_id, provider, self.clock,
                                       self.output_dir, wait_timeout=resource_wait_timeout,
                                       public_fictional=public_fictional)
        self.manager.bind_static(self.values)
        self.sources = {"user:request": {"kind": "user", "text": suite.query}}
        if suite.fixture == "local_shop":
            self.sources.update({identity: {"kind": "contract", "text": CONTRACT}
                                 for identity in ("contract:shop-v1", "sample_shop.CONTRACT.v1")})
        self.sources.update(source_catalog or {})
        self.environment = nullcontext(fixture) if fixture is not None else (
            shop_server() if suite.fixture == "local_shop" else nullcontext((None, None)))
        self.faults, self.headless = faults or {}, headless
        self.base = self.store = self.browser = self.session = None
        self._stack = None
        self._native_pause = None

    def begin_native_pause(self):
        """Native graph suspension is human wait, not active execution time."""
        if self._native_pause is None:
            pause = self.clock.waiting()
            pause.__enter__()
            self._native_pause = pause

    def end_native_pause(self):
        """Release native wait before the resource provider owns its own wait."""
        if self._native_pause is not None:
            pause, self._native_pause = self._native_pause, None
            pause.__exit__(None, None, None)

    async def open(self):
        if self._stack is not None:
            raise RuntimeError("Execution runtime is already open")
        self._stack = AsyncExitStack()
        try:
            self.base, self.store = self._stack.enter_context(self.environment)
            playwright = await self._stack.enter_async_context(async_playwright())
            self.browser = await playwright.chromium.launch(channel="chromium", headless=self.headless)
            self._stack.push_async_callback(self.browser.close)
        except BaseException:
            await self.close(clear_resources=False)
            raise
        return self

    async def close(self, *, clear_resources=True):
        """Release live handles; retain redaction until records have been saved."""
        self.end_native_pause()
        try:
            try:
                await self.close_scene()
            finally:
                if self._stack is not None:
                    stack, self._stack = self._stack, None
                    await stack.aclose()
        finally:
            self.browser = None
            if clear_resources:
                self.manager.close()

    async def prepare_resource(self, identity, scene_id):
        """Acquire one preparation resource; callers decide when another is needed."""
        self.values[identity] = await self.manager.request(
            identity, scene_id, "prepare-" + identity, "Prepare user-supplied test account")

    async def open_scene(self, scenario):
        if self.session is not None:
            raise RuntimeError("Close the current scene before opening another")
        namespace = self.run_id + "_" + scenario.namespace
        if self.store:
            self.store.prepare(namespace, self.faults.get(scenario.id, ()))
            if scenario.initial_state == "registered_account":
                self.store.account(namespace, self.values["username"], self.values["password"])
        elif scenario.initial_state != "empty":
            raise ValueError("Initial state requires an explicit local fixture")
        start = scenario.start_url.format(base=self.base, namespace=namespace)
        context = await self.browser.new_context()
        try:
            context_id = uuid.uuid4().hex
            page = await context.new_page()
            network = []

            def response_metadata(response):
                if len(network) < 64:
                    network.append({"method": response.request.method, "url": public_url(response.url),
                                    "status": response.status, "resource_type": response.request.resource_type})

            def connection_metadata(request):
                if len(network) < 64:
                    network.append({"method": request.method, "url": public_url(request.url),
                                    "failure": request.failure, "resource_type": request.resource_type})

            page.on("response", response_metadata)
            page.on("requestfailed", connection_metadata)
            page.set_default_timeout(1500)
            page.set_default_navigation_timeout(15000 if self.site_profile == "practice-login" else 5000)
            url_policy = None
            if self.site_profile == "practice-login":
                from .site_profiles import validate_profile_url
                validate_profile_url(self.site_profile, start)
                url_policy = lambda url: validate_profile_url(self.site_profile, url)
            executor = ActionExecutor(page, start, self.manager, self.suite.budget,
                                      clock=self.clock, scene_id=scenario.id, fixture_store=self.store,
                                      url_policy=url_policy)
            self.session = SceneSession(scenario.id, namespace, context_id, context, page, executor, network)
        except BaseException:
            await context.close()
            raise
        return self.session

    async def close_scene(self):
        if self.session is not None:
            session, self.session = self.session, None
            await session.context.close()

    def _session(self):
        if self.session is None:
            raise RuntimeError("No live scene session is available")
        return self.session

    def safe_payload(self, value, *, suite=None):
        contract = suite or self.suite
        return redact_role_payload(value, self.manager.redactor.data,
            public_ids=public_action_ids(contract), public_locators=public_action_locators(contract))

    async def observe(self):
        return self.safe_payload(await observe(self._session().page))

    async def execute_one(self, action, check_conditions, *, pending_resource=None):
        executor = self._session().executor
        if pending_resource is not None:
            return await executor.execute(action, check_conditions, pending_resource=pending_resource)
        return await executor.execute(action, check_conditions)

    async def resource_request_metadata(self, action):
        """Only current challenge identity/lifetime; never a delivery/code value."""
        return await self._session().executor.resource_metadata(action.resource_id)

    async def check_conditions(self, checkpoint, outcome):
        session = self._session()
        for condition in checkpoint.acceptance:
            evidence = {"id": f"{session.scene_id}:{checkpoint.id}:{len(outcome['evidence'])}",
                        "rule_id": condition.rule_id, "target": condition.target,
                        "operator": condition.operator, "expected": condition.expected,
                        "condition_fingerprint": condition_fingerprint(condition)}
            outcome["evidence"].append(evidence)
            try:
                evidence["actual"] = await session.executor.check(condition)
                evidence["status"] = "passed"
            except Exception as error:
                try:
                    evidence["actual"] = await actual_condition(session.page, condition)
                except Exception as capture_error:
                    evidence["actual"] = {"unavailable": type(capture_error).__name__}
                evidence.update(status="failed", error=f"{type(error).__name__}: {error}")
                raise

    async def screenshot(self, path):
        page = self._session().page
        await page.screenshot(path=str(path), full_page=True, mask=self.manager.redactor.masks(page), timeout=5000)
        return str(path)

    async def capture_checkpoint(self, checkpoint, outcome):
        session = self._session()
        page = session.page
        outcome["observation"] = await self.observe()
        outcome["observation"]["messages"] = self.manager.redactor.data(
            [text[:1000] for text in (await page.locator('[role="alert"], [data-testid="message"]').all_text_contents())[:8]])
        if self.store and {"username", "password"} <= set(self.specs_by_id):
            outcome["fixture_state"] = {"account_ref": "username", "password_ref": "password",
                **self.store.account_state(session.namespace, self.values["username"], self.values["password"]),
                **self.store.stats(session.namespace), "origin": "read-only local test-store facts; no resource values or fault configuration"}
        screenshot = self.output_dir / f"{session.scene_id}-{checkpoint.id}.png"
        await self.screenshot(screenshot)
        outcome.update(screenshot=str(screenshot), url=page.url, context_id=session.context_id)
        return outcome

    def save_actions(self):
        session = self._session()
        (self.output_dir / f"{session.scene_id}-actions.json").write_text(json.dumps(self.safe_payload(
            {"run_id": self.run_id, "context_id": session.context_id, "events": session.executor.events}),
            ensure_ascii=False, indent=2), encoding="utf-8")

    def build_scene_record(self, session, checkpoints):
        return {"id": session.scene_id, "namespace": session.namespace, "context_id": session.context_id,
                "contexts_created": 1, "status": aggregate(checkpoint["status"] for checkpoint in checkpoints),
                "checkpoints": checkpoints, "events": session.executor.events,
                "complete": all(checkpoint["status"] == "passed" for checkpoint in checkpoints),
                "network": session.network,
                "stats": self.store.stats(session.namespace) if self.store else {}}

    def build_record(self, *, results, action_count, planned_suite, model_used=False,
                     repaired=False, repair_count=0, repair_history=(), initial_failures=()):
        status = aggregate(scene["status"] for scene in results.values())
        return {"schema_version": 2, "run_id": self.run_id, "suite_id": self.suite.id,
                "requirements_fingerprint": self.suite.requirements_fingerprint(),
                "planned_scenarios": [scene.id for scene in self.suite.scenarios],
                "scenarios": results, "action_count": action_count, "status": status,
                "model_calls": None if model_used else 0,
                "repaired": repaired, "repair_count": repair_count, "repair_history": list(repair_history),
                "initial_failures": list(initial_failures), "initial_status": "error" if initial_failures else status,
                "stability": "inconsistent" if initial_failures and status == "passed" else "unverified",
                "sanitized_plan": self.safe_payload(planned_suite.model_dump(), suite=planned_suite),
                "source_catalog": self.safe_payload(self.sources),
                "timing": {"active_seconds": self.clock.active_seconds, "human_wait_seconds": self.clock.wait_seconds},
                "evidence_policy": "masked screenshots and resource references; no raw DOM, network payload or trace"}
