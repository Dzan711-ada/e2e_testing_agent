"""The complete 16-node business graph and its transition helpers."""
from copy import deepcopy
import uuid
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from ..contracts import Plan, ResourceSpec
from ..resources.interaction import ResourceRequest, ResourceUnavailable
from ..tools.browser import ToolError, allowed_url
from .state import RunState


def current(state):
    plan = Plan.model_validate(state['plan'])
    scenario = plan.scenarios[state['scene_index']]
    return plan, scenario, scenario.checkpoints[state['checkpoint_index']]


def record(state, kind, **value):
    return [*state['records'], {'id': f'ev-{len(state["records"]) + 1}', 'kind': kind, **value}]


def limit(state, host):
    if state['model_decisions'] >= host.max_decisions or state['actions'] > host.settings.max_actions or host.vault.clock.active_seconds > 180:
        return {'phase': 'finish', 'status': 'blocked', 'error': 'Execution or decision budget exhausted'}
    return None


def known_urls(state, tools):
    urls = {state['target_url']}
    for observation in state['observations']:
        urls.add(observation['url'])
        for element in observation['elements']:
            if element.get('href'):
                try:
                    urls.add(allowed_url(element['href'], observation['url'], tools.origins))
                except ToolError:
                    pass
    return urls


def validate_plan(plan, state, tools):
    sources = {'user', *['answer:' + key for key in state['answers']], *['page:' + o['url'] for o in state['observations']]}
    if any(rule.source not in sources for rule in plan.requirements):
        raise ValueError('Plan cites an unknown source')
    urls = known_urls(state, tools)
    for scenario in plan.scenarios:
        allowed_url(scenario.start_url, state['target_url'], tools.origins)
        if scenario.start_url not in urls:
            raise ValueError('Scenario starts at an unobserved or invented URL')
    old = state.get('plan')
    if old:
        previous = Plan.model_validate(old)
        new_rules = {r.id: r for r in plan.requirements}
        if any(new_rules.get(r.id) != r for r in previous.requirements):
            raise ValueError('Revision cannot remove or rewrite confirmed requirements')
        new_assertions = {(s.id, c.id, a.id): a for s in plan.scenarios for c in s.checkpoints for a in c.assertions}
        if any(new_assertions.get((s.id, c.id, a.id)) != a for s in previous.scenarios for c in s.checkpoints for a in c.assertions):
            raise ValueError('Revision cannot remove or weaken confirmed assertions')


def build_graph(host, bridge):
    async def observe_page(state):
        observation = await host.tools.observe()
        return {'observation': observation, 'observations': [*state['observations'], observation]}

    async def design(state):
        exceeded = limit(state, host)
        if exceeded:
            return exceeded
        payload = {key: state.get(key) for key in ('goal', 'target_url', 'observations', 'answers', 'clarifications', 'plan', 'failure')}
        payload['budget'] = {'remaining_actions': host.settings.max_actions - state['actions']}
        decision = await host.design.decide(host.safe(payload))
        update = {'decision': decision.model_dump(), 'model_decisions': state['model_decisions'] + 1}
        if decision.status == 'inspect':
            if decision.inspect_url not in known_urls(state, host.tools):
                raise ValueError('Design inspection must use an observed link or URL')
            update['phase'] = 'inspect_page'
        elif decision.status == 'clarify':
            if state['questions'] >= 4 or any(q.id in state['answers'] for q in decision.questions):
                return {**update, 'phase': 'finish', 'status': 'blocked', 'error': 'Repeated question or clarification budget exhausted'}
            update['phase'] = 'clarify'
        elif decision.status == 'ready':
            validate_plan(decision.plan, state, host.tools)
            version = (state['plan']['version'] + 1) if state.get('plan') else 1
            plan = decision.plan.model_copy(update={'version': version})
            host.vault.register_plan(plan)
            update.update(plan=plan.model_dump(), phase='select_checkpoint', scene_index=0, checkpoint_index=0)
        else:
            update.update(phase='finish', status='blocked', error=decision.reason)
        return update

    async def inspect_page(state):
        from ..contracts import Action
        if state['actions'] >= host.settings.max_actions:
            return {'phase': 'finish', 'status': 'blocked', 'error': 'Action budget exhausted'}
        event = await host.tools.execute(Action(kind='navigate', intent=state['decision']['reason'], value=state['decision']['inspect_url']), 'design')
        if event['status'] != 'passed':
            return {'phase': 'finish', 'status': 'blocked', 'error': 'Could not inspect the requested page'}
        return {'phase': 'design', 'actions': state['actions'] + 1}

    async def clarify(state):
        answers = dict(state['answers'])
        clarifications = list(state.get('clarifications', []))
        try:
            for question in state['decision']['questions']:
                request = ResourceRequest(uuid.uuid4().hex, question['id'], '业务澄清', question['text'], 'design', question['id'], host.vault.run_id, sensitive=False)
                with host.vault.clock.waiting():
                    import asyncio
                    answer = await asyncio.wait_for(host.vault.provider.acquire(request), timeout=host.vault.wait_timeout)
                if not isinstance(answer, str) or not answer.strip():
                    raise ResourceUnavailable('Required clarification has no answer')
                answers[question['id']] = answer.strip()
                clarifications.append({**question, 'answer': answer.strip()})
        except (ResourceUnavailable, TimeoutError):
            return {'answers': answers, 'clarifications': clarifications, 'phase': 'finish', 'status': 'blocked',
                    'error': 'Required clarification unavailable; no assumptions substituted'}
        return {'answers': answers, 'clarifications': clarifications, 'questions': state['questions'] + 1, 'phase': 'design'}

    async def select_checkpoint(state):
        plan = Plan.model_validate(state['plan'])
        scene_index, checkpoint_index = state['scene_index'], state['checkpoint_index']
        if scene_index >= len(plan.scenarios):
            return {'phase': 'finish', 'status': 'passed'}
        scenario = plan.scenarios[scene_index]
        if checkpoint_index >= len(scenario.checkpoints):
            host.vault.clear_scene(scenario.id)
            return {'scene_index': scene_index + 1, 'checkpoint_index': 0, 'phase': 'select_checkpoint'}
        update = {'phase': 'step', 'failure': None, 'retries': 0}
        if checkpoint_index == 0:
            if state['actions'] >= host.settings.max_actions:
                return {'phase': 'finish', 'status': 'blocked', 'error': 'Action budget exhausted'}
            await host.new_scene(scenario.start_url)
            update.update(actions=state['actions'] + 1,
                records=record(state, 'scenario_start', scenario_id=scenario.id, url=scenario.start_url, version=plan.version))
        return update

    async def plan_step(state):
        exceeded = limit(state, host)
        if exceeded:
            return exceeded
        plan, scenario, checkpoint = current(state)
        specs = list(host.vault.specs.values())
        payload = {'goal': state['goal'], 'scenario': scenario.model_dump(exclude={'checkpoints'}),
            'checkpoint': checkpoint.model_dump(), 'observation': state['observation'], 'known_urls': sorted(known_urls(state, host.tools)),
            'resources': [s.model_dump() for s in specs], 'available': {s.id: host.vault.has(s.id, scenario.id) for s in specs},
            'history': state['records'][next((i for i in range(len(state['records']) - 1, -1, -1) if state['records'][i]['kind'] == 'scenario_start'), 0):][-30:],
            'failure': state.get('failure'), 'remaining_actions': host.settings.max_actions - state['actions']}
        decision = await host.execute.decide(host.safe(payload))
        update = {'decision': decision.model_dump(), 'model_decisions': state['model_decisions'] + 1}
        if decision.status == 'resource':
            host.vault.declare(decision.resource)
            resources = tuple(host.vault.specs.values())
            update.update(plan=plan.model_copy(update={'resources': resources}).model_dump(),
                pending=decision.resource.model_dump(), phase='await_resource', current_action=None)
        elif decision.status == 'action':
            if state['actions'] >= host.settings.max_actions:
                return {**update, 'phase': 'finish', 'status': 'blocked', 'error': 'Action budget exhausted'}
            action = decision.action
            if action.kind == 'navigate' and allowed_url(action.value, host.page.url, host.tools.origins) not in known_urls(state, host.tools):
                raise ValueError('Execution navigation must use an observed URL')
            if action.target:
                host.tools.target(action.target)
            update['current_action'] = action.model_dump()
            if action.resource_id and not host.vault.has(action.resource_id, scenario.id):
                if action.resource_id not in host.vault.specs:
                    raise ValueError('Describe a new resource before using it')
                update.update(pending=host.vault.specs[action.resource_id].model_dump(), phase='await_resource')
            else:
                update['phase'] = 'execute_step'
        elif decision.status == 'verify':
            ids = [b.assertion_id for b in decision.bindings]
            if len(ids) != len(set(ids)) or set(ids) != {a.id for a in checkpoint.assertions}:
                raise ValueError('Verification must bind every original assertion exactly once')
            try:
                bindings = {b.assertion_id: b.target for b in decision.bindings}
                for assertion in checkpoint.assertions:
                    host.tools.validate_binding(assertion, bindings[assertion.id])
            except ToolError as error:
                failure = {'kind': 'decision_validation', 'reason': str(error), 'dispatched': False}
                return {**update, 'phase': 'observe_page', 'failure': failure,
                        'records': record(state, 'decision_rejected', status='error', reason=str(error))}
            update['phase'] = 'verify_checkpoint'
        elif decision.status == 'replan':
            update.update(phase='revise_plan', failure={'reason': decision.reason})
        else:
            update.update(phase='finish', status='blocked', error=decision.reason)
        return update

    async def await_resource(state):
        _, scenario, checkpoint = current(state)
        spec = ResourceSpec.model_validate(state['pending'])
        try:
            await host.vault.acquire(spec, scenario.id, checkpoint.id)
        except ResourceUnavailable as error:
            return {'phase': 'finish', 'status': error.status, 'error': 'Required resource unavailable: ' + str(error)}
        return {'pending': None, 'phase': 'execute_step' if state.get('current_action') else 'step',
                'records': record(state, 'resource', scenario_id=scenario.id, checkpoint_id=checkpoint.id, spec=spec.model_dump())}

    async def execute_step(state):
        from ..contracts import Action
        _, scenario, checkpoint = current(state)
        event = await host.tools.execute(Action.model_validate(state['current_action']), scenario.id)
        records = record(state, 'action', scenario_id=scenario.id, checkpoint_id=checkpoint.id, **event)
        update = {'records': records, 'actions': state['actions'] + 1, 'current_action': None, 'phase': 'step'}
        if event['status'] != 'passed':
            update.update(phase='diagnose', failure=records[-1])
        return update

    async def verify_checkpoint(state):
        _, scenario, checkpoint = current(state)
        bindings = {b['assertion_id']: b['target'] for b in state['decision']['bindings']}
        records = list(state['records'])
        failures = []
        for assertion in checkpoint.assertions:
            event = await host.tools.verify(assertion, bindings[assertion.id], scenario.id)
            entry = {'id': f'ev-{len(records) + 1}', 'kind': 'assertion', 'scenario_id': scenario.id, 'checkpoint_id': checkpoint.id, **event}
            records.append(entry)
            if event['status'] != 'passed':
                failures.append(entry)
        if failures:
            return {'records': records, 'phase': 'diagnose', 'failure': failures[0]}
        return {'records': records, 'checkpoint_index': state['checkpoint_index'] + 1, 'phase': 'select_checkpoint'}

    async def diagnose(state):
        if state['model_decisions'] >= host.max_decisions:
            return {'phase': 'finish', 'status': 'error', 'error': 'Diagnosis model budget exhausted'}
        evidence_ids = {r['id'] for r in state['records']}
        assessment = await host.diagnosis.decide(host.safe({'goal': state['goal'], 'plan': state['plan'],
            'failure': state['failure'], 'observation': await host.tools.observe(), 'evidence': state['records'][-20:]}))
        if not set(assessment.evidence_ids) <= evidence_ids:
            raise ValueError('Diagnosis cites nonexistent evidence')
        retry = (assessment.recommendation == 'retry' and assessment.attribution == 'agent_execution'
            and state['failure']['kind'] == 'action' and state['failure'].get('dispatched') is False and state['retries'] < 2)
        return {'assessments': [*state['assessments'], assessment.model_dump()], 'model_decisions': state['model_decisions'] + 1,
            'retries': state['retries'] + 1, 'phase': 'step' if retry else 'finish',
            'status': 'running' if retry else ('failed' if state['failure']['kind'] == 'assertion' else 'error')}

    async def revise_plan(state):
        return {'phase': 'design', 'revisions': [*state['revisions'], deepcopy(state['plan'])]}

    async def finish(state):
        from ..delivery import recipes
        complete = recipes(state) is not None
        return {'generation_status': state['status'], 'complete_script': complete,
            'repaired': state['status'] == 'passed' and any(r['kind'] == 'action' and r.get('status') == 'error' for r in state['records']),
            'phase': 'export_script' if complete else 'report'}

    async def export_script(state):
        from ..delivery import export
        directory = export(host.safe(state), host.directory / 'export', host.settings)
        return {'export_dir': str(directory), 'phase': 'replay_script'}

    async def replay_script(state):
        from ..delivery.launcher import launch
        result = await launch(state['export_dir'], host.vault.provider, timeout=host.settings.replay_timeout)
        first, second = state['generation_status'], result['status']
        inconsistent = first in {'passed', 'failed'} and second in {'passed', 'failed'} and first != second
        status = 'error' if inconsistent else second
        entry = record(state, 'replay', status=second, counts=result['counts'])
        return {'records': entry, 'replay_result': result, 'inconsistent': inconsistent, 'status': status,
            'phase': 'diagnose_replay' if second in {'failed', 'error'} and first == 'passed' else 'report'}

    async def diagnose_replay(state):
        if state['model_decisions'] >= host.max_decisions:
            return {'phase': 'report'}
        failure = state['records'][-1]
        assessment = await host.diagnosis.decide(host.safe({'goal': state['goal'], 'plan': state['plan'],
            'failure': failure, 'evidence': [failure], 'observation': {}}))
        if not set(assessment.evidence_ids) <= {failure['id']}:
            raise ValueError('Replay diagnosis cites nonexistent evidence')
        return {'assessments': [*state['assessments'], assessment.model_dump()],
                'model_decisions': state['model_decisions'] + 1, 'phase': 'report'}

    async def report(state):
        from ..delivery import write_report
        write_report(host.directory, host.safe(state))
        return {'phase': 'complete'}

    graph = StateGraph(RunState)
    nodes = {'observe_page': observe_page, 'design': design, 'inspect_page': inspect_page, 'clarify': clarify,
        'select_checkpoint': select_checkpoint, 'plan_step': plan_step, 'await_resource': await_resource,
        'execute_step': execute_step, 'verify_checkpoint': verify_checkpoint, 'diagnose': diagnose,
        'revise_plan': revise_plan, 'finish': finish, 'export_script': export_script,
        'replay_script': replay_script, 'diagnose_replay': diagnose_replay, 'report': report}
    for name, function in nodes.items():
        graph.add_node(name, bridge.node(function))
    def route(name, chooser, *paths):
        graph.add_conditional_edges(name, chooser, {path: path for path in paths})
    graph.add_edge(START, 'observe_page')
    route('observe_page', lambda s: 'design' if s['phase'] == 'design' else 'select_checkpoint' if s['phase'] == 'select_checkpoint' else 'plan_step', 'design', 'select_checkpoint', 'plan_step')
    route('design', lambda s: s['phase'], 'inspect_page', 'clarify', 'select_checkpoint', 'finish')
    route('plan_step', lambda s: s['phase'], 'await_resource', 'execute_step', 'verify_checkpoint', 'revise_plan', 'finish', 'observe_page')
    route('inspect_page', lambda s: 'observe_page' if s['phase'] == 'design' else s['phase'], 'observe_page', 'finish')
    route('clarify', lambda s: s['phase'], 'design', 'finish')
    route('select_checkpoint', lambda s: 'observe_page' if s['phase'] == 'step' else s['phase'], 'observe_page', 'select_checkpoint', 'finish')
    route('await_resource', lambda s: 'observe_page' if s['phase'] == 'step' else s['phase'], 'observe_page', 'execute_step', 'finish')
    route('execute_step', lambda s: 'observe_page' if s['phase'] == 'step' else s['phase'], 'observe_page', 'diagnose')
    route('verify_checkpoint', lambda s: s['phase'], 'select_checkpoint', 'diagnose')
    route('diagnose', lambda s: 'observe_page' if s['phase'] == 'step' else s['phase'], 'observe_page', 'finish')
    graph.add_edge('revise_plan', 'design')
    route('finish', lambda s: s['phase'], 'export_script', 'report')
    graph.add_edge('export_script', 'replay_script')
    route('replay_script', lambda s: s['phase'], 'diagnose_replay', 'report')
    graph.add_edge('diagnose_replay', 'report')
    graph.add_edge('report', END)
    return graph.compile(checkpointer=MemorySaver(), interrupt_before=['clarify', 'await_resource'])
