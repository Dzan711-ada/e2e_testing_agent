"""Browser/service ownership, native checkpoint pumping and saved-run entry points."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import uuid
from playwright.async_api import async_playwright
from ..agents import DesignAgent, DiagnosisAgent, ExecutionAgent, ModelGateway
from ..contracts import Plan
from ..resources import ResourceVault
from ..tools.browser import BrowserTools, ToolError, allowed_url
from ...config import validate_url
from ...graph_checkpoint import AsyncGraphSession, AsyncNodeBridge
from .graph import build_graph
from .state import RunState


class Host:
    def __init__(self, settings, directory, provider, gateway, *, max_decisions=40):
        self.settings, self.directory = settings, Path(directory)
        self.vault = ResourceVault(provider)
        self.gateway = gateway or ModelGateway(settings, max_calls=max_decisions, redact=self.safe)
        self.design, self.execute, self.diagnosis = DesignAgent(self.gateway), ExecutionAgent(self.gateway), DiagnosisAgent(self.gateway)
        self.max_decisions = max_decisions
        self.playwright = self.browser = self.context = self.page = self.tools = None

    def safe(self, value):
        return json.loads(self.settings.redact(json.dumps(self.vault.redactor.data(value), ensure_ascii=False)))

    def save(self, name, value):
        (self.directory / name).write_text(json.dumps(self.safe(value), ensure_ascii=False, indent=2), encoding='utf-8')

    async def open(self, url):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(self.settings.browsers_path)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel='chromium', headless=self.settings.headless)
        await self.new_scene(url)

    async def new_scene(self, url):
        if self.context is not None:
            await self.context.close()
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.tools = BrowserTools(self.page, url, self.vault, timeout_ms=self.settings.action_timeout_ms)
        origins = self.tools.origins
        async def navigation_guard(route):
            request = route.request
            if request.is_navigation_request() and request.frame == self.page.main_frame:
                try:
                    allowed_url(request.url, url, origins)
                except ToolError:
                    await route.abort()
                    return
            await route.continue_()
        await self.page.route('**/*', navigation_guard)
        await self.page.goto(url, wait_until='domcontentloaded', timeout=self.settings.action_timeout_ms)

    async def close(self):
        try:
            if self.browser is not None:
                await self.browser.close()
        finally:
            if self.playwright is not None:
                await self.playwright.stop()


async def run(settings, *, target_url, goal, run_dir=None, provider=None, gateway=None, max_decisions=40,
              frozen_plan=None, confirmed_answers=None, confirmed_clarifications=None):
    target_url = validate_url(target_url)
    if not goal.strip():
        raise ValueError('A test goal is required')
    directory = Path(run_dir or settings.runtime_path / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-web-' + uuid.uuid4().hex[:8]))
    directory.mkdir(parents=True, exist_ok=False)
    host = Host(settings, directory, provider, gateway, max_decisions=max_decisions)
    state = RunState(goal=goal, target_url=target_url, phase='design', status='running', observations=[], observation={},
        answers={}, clarifications=[],
        plan=None, scene_index=0, checkpoint_index=0, records=[], pending=None, current_action=None,
        failure=None, assessments=[], revisions=[], model_decisions=0, actions=1, questions=0, retries=0, error=None)
    if frozen_plan is not None:
        from ..tools.browser import origin
        plan = Plan.model_validate(frozen_plan)
        for scenario in plan.scenarios:
            allowed_url(scenario.start_url, target_url, {origin(target_url)})
        host.vault.register_plan(plan)
        state.update(plan=plan.model_copy(update={'version': plan.version + 1}).model_dump(),
            answers=dict(confirmed_answers or {}), clarifications=deepcopy(confirmed_clarifications or []),
            phase='select_checkpoint', revisions=[plan.model_dump()])
    pauses = []
    try:
        await host.open(target_url)
        bridge = AsyncNodeBridge()
        graph = build_graph(host, bridge)
        session = AsyncGraphSession(graph, bridge)
        config = {'configurable': {'thread_id': host.vault.run_id}, 'recursion_limit': 512}
        value = state
        # Only native checkpoint pumping is outside the graph; all business loops are graph edges.
        while True:
            async for update in session.astream(value, config):
                for name, changes in update.items():
                    if changes:
                        state.update(changes)
                        host.save('state.json', state)
                        print(f'Agent {name}: {state["phase"]}', flush=True)
            snapshot = await session.aget_state(config)
            state.update(snapshot.values)
            if not snapshot.next:
                break
            pauses.append({'next': list(snapshot.next), 'pending': state['pending'], 'questions': state['decision'].get('questions', [])})
            host.save('pauses.json', pauses)
            value = None
    except Exception as error:
        state.update(status='error', error=host.safe({'message': str(error)})['message'], phase='complete')
    except BaseException:
        state.update(status='cancelled', error='Run interrupted', phase='complete')
        raise
    finally:
        try:
            await host.close()
        finally:
            state['run_dir'] = str(directory)
            host.save('result.json', state)
            host.save('resources.json', host.vault.events)
            host.save('model-calls.json', getattr(host.gateway, 'calls', []))
            host.save('model-inputs.json', getattr(host.gateway, 'inputs', []))
            from ..delivery import write_report
            write_report(directory, host.safe(state))
            host.vault.close()
    return state


async def diagnose_saved(settings, directory, *, gateway=None):
    directory = Path(directory).resolve()
    state = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    evidence = [e for e in state['records'] if e.get('status') in {'failed', 'error', 'blocked'}]
    if not evidence:
        raise ValueError('该运行没有失败证据可供诊断。')
    target = directory / ('diagnosis-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    target.mkdir()
    safe = lambda value: json.loads(settings.redact(json.dumps(value, ensure_ascii=False)))
    transport = gateway or ModelGateway(settings, max_calls=2, redact=safe)
    assessment = await DiagnosisAgent(transport).decide(safe({'goal': state['goal'], 'plan': state.get('plan'),
        'failure': evidence[-1], 'evidence': evidence, 'observation': {}}))
    if not set(assessment.evidence_ids) <= {e['id'] for e in evidence}:
        raise ValueError('Diagnosis cites nonexistent evidence')
    result = {'status': 'completed', 'assessment': assessment.model_dump(), 'directory': str(target), 'original_unchanged': True}
    (target / 'assessment.json').write_text(json.dumps(safe(result), ensure_ascii=False, indent=2), encoding='utf-8')
    return result
