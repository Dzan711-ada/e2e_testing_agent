import asyncio
from dataclasses import replace
import json
from pathlib import Path
import pytest
from e2e_agent.config import Settings
from e2e_agent.core.agents import DesignDecision, ExecutionDecision, Assessment
from e2e_agent.core.agents import ModelGateway
from e2e_agent.core.contracts import Plan
from e2e_agent.core.workflow import run
from e2e_agent.core.tools.browser import BrowserTools
from resource_provider import ScriptedProvider
from test_universal_tools import pages, spec


def make_plan(url):
    return Plan(requirements=[{'id': 'delivery', 'statement': '查询的订单应显示已发货', 'source': 'user'}],
        scenarios=[{'id': 'lookup', 'objective': '查询订单', 'start_url': url,
            'checkpoints': [{'id': 'lookup_result', 'objective': '查询订单并检查状态', 'assertions': [
                {'id': 'status', 'requirement_id': 'delivery', 'target': '发货状态', 'operator': 'text', 'expected': '已发货'}]}]}])


class Roles:
    def __init__(self, mode='normal'):
        self.mode, self.calls, self.inputs = mode, [], []
        self.questioned = self.replanned = False
        self.bad_binding = False

    async def call(self, role, schema, prompt, payload):
        self.calls.append({'role': role, 'real': False})
        self.inputs.append({'role': role, 'payload': payload})
        if role == 'design':
            assert payload['observations'] and payload['observations'][0]['elements']
            if self.mode in {'clarify', 'unanswered'} and not payload['answers']:
                return DesignDecision(status='clarify', reason='缺少业务预期', questions=[
                    {'id': 'shipping_expectation', 'text': '订单应该处于什么状态？', 'reason': '需要验收依据'}])
            if self.mode == 'replan' and payload.get('plan') and not payload['answers']:
                return DesignDecision(status='clarify', reason='执行发现业务缺口', questions=[
                    {'id': 'shipping_expectation', 'text': '订单应该处于什么状态？', 'reason': '需要确认'}])
            plan = make_plan(payload['target_url'])
            if self.mode in {'resource-expectation', 'bad-binding'}:
                data = plan.model_dump(mode='json')
                data['resources'] = [spec('purchase_order').model_dump()]
                data['scenarios'][0]['checkpoints'][0]['assertions'].append({'id': 'input-ref', 'requirement_id': 'delivery',
                    'target': '订单输入框', 'operator': 'value', 'expected': {'resource_id': 'purchase_order'}})
                plan = Plan.model_validate(data)
            if self.mode == 'assertion-failure':
                data = plan.model_dump(mode='json')
                data['scenarios'][0]['checkpoints'][0]['assertions'][0]['expected'] = '尚未发货'
                plan = Plan.model_validate(data)
            return DesignDecision(status='ready', reason='范围和预期明确', plan=plan)
        if role == 'diagnose':
            return Assessment(attribution='agent_execution' if self.mode in {'recover', 'committed-error'} else 'unknown', reason='依据实际证据判断操作是否提交',
                              evidence_ids=[payload['failure']['id']], recommendation='retry')
        if self.mode == 'budget':
            return ExecutionDecision(status='resource', reason='测试预算终止', resource=spec('purchase_order'))
        if self.mode == 'replan' and not self.replanned:
            self.replanned = True
            return ExecutionDecision(status='replan', reason='执行中发现业务预期有待确认')
        if not payload['available'].get('purchase_order'):
            return ExecutionDecision(status='resource', reason='查询需要订单编号', resource=spec('purchase_order'))
        actions = [r for r in payload['history'] if r['kind'] == 'action' and r['status'] == 'passed']
        elements = payload['observation']['elements']
        if not any(r['action']['kind'] == 'fill' for r in actions):
            target = next(e['ref'] for e in elements if e['label'] == '订单编号')
            return ExecutionDecision(status='action', reason='填写订单编号', action={
                'kind': 'fill', 'intent': '查询用户提供的订单', 'target': target, 'resource_id': 'purchase_order'})
        if not any(r['action']['kind'] == 'click' for r in actions):
            target = next(e['ref'] for e in elements if e['text'] == '查询订单')
            return ExecutionDecision(status='action', reason='提交查询', action={'kind': 'click', 'intent': '查询', 'target': target})
        target = next(e['ref'] for e in elements if e['tag'] == 'p' and e['text'])
        bindings = [{'assertion_id': 'status', 'target': target}]
        if self.mode in {'resource-expectation', 'bad-binding'}:
            field = next(e['ref'] for e in elements if e['label'] == '订单编号')
            bindings.append({'assertion_id': 'input-ref', 'target': target if self.mode == 'bad-binding' and not self.bad_binding else field})
            self.bad_binding = True
        return ExecutionDecision(status='verify', reason='结果可见，验证冻结预期', bindings=bindings)


@pytest.mark.parametrize('mode,expected', [('normal', 'passed'), ('clarify', 'passed'), ('unanswered', 'blocked'),
    ('replan', 'passed'), ('assertion-failure', 'failed'), ('budget', 'blocked'), ('recover', 'passed'), ('committed-error', 'error'), ('resource-expectation', 'passed'), ('bad-binding', 'passed')])
def test_native_graph_semantic_loops_and_dynamic_resources(tmp_path, monkeypatch, mode, expected):
    if mode in {'recover', 'committed-error'}:
        original = BrowserTools.execute
        injected = False
        async def stale_once(tools, action, scene_id):
            nonlocal injected
            if action.kind == 'fill' and not injected:
                injected = True
                if mode == 'committed-error':
                    event = await original(tools, action, scene_id)
                    return {**event, 'status': 'error', 'error': 'Unknown result after dispatch'}
                await tools.page.locator(f'[data-e2e-agent-ref="{action.target}"]').evaluate("e => e.removeAttribute('data-e2e-agent-ref')")
            return await original(tools, action, scene_id)
        monkeypatch.setattr(BrowserTools, 'execute', stale_once)
    async def execute(base):
        gateway = Roles(mode)
        provider = ScriptedProvider({'purchase_order': 'ORD-REALISTIC-934', 'shipping_expectation': '' if mode == 'unanswered' else '已发货'})
        result = await run(Settings(), target_url=base + '/orders', goal='查询订单并确认已发货',
            run_dir=tmp_path / mode, provider=provider, gateway=gateway, max_decisions=4 if mode == 'budget' else 30)
        assert result['status'] == expected, result.get('error')
        if expected == 'passed':
            assert result['plan']['resources'][0]['id'] == 'purchase_order'
            assert len([r for r in result['records'] if r['kind'] == 'assertion']) == (2 if mode in {'resource-expectation', 'bad-binding'} else 1)
            if mode == 'recover':
                assert any(r.get('status') == 'error' and r.get('dispatched') is False for r in result['records'])
                assert result['assessments']
            else:
                assert all(r['status'] == 'passed' for r in result['records'] if r['kind'] in {'action', 'assertion'})
            pauses = json.loads((tmp_path / mode / 'pauses.json').read_text(encoding='utf-8'))
            assert any('await_resource' in p['next'] for p in pauses)
            assert result['plan']['version'] == (2 if mode == 'replan' else 1)
        if mode in {'clarify', 'unanswered', 'replan'}:
            assert any(r['ref'] == 'shipping_expectation' for r in provider.requests)
        else:
            assert not any(r['ref'] == 'shipping_expectation' for r in provider.requests)
        if mode == 'replan':
            assert result['revisions'] and result['answers']['shipping_expectation'] == '已发货'
        if mode == 'assertion-failure':
            assert gateway.calls[-1]['role'] == 'diagnose'
            assert result['plan']['scenarios'][0]['checkpoints'][0]['assertions'][0]['expected'] == '尚未发货'
            assert any(r.get('status') == 'failed' for r in result['records'])
        if mode == 'unanswered':
            assert not result['records'] and not any(c['role'] == 'execute' for c in gateway.calls)
        if mode == 'committed-error':
            assert len([r for r in result['records'] if r['kind'] == 'action']) == 1
            assert result['assessments'][0]['recommendation'] == 'retry'
            assert not result['complete_script']
        if mode == 'bad-binding':
            assert any(r['kind'] == 'decision_rejected' for r in result['records'])
            assert not result['repaired']
        for path in (tmp_path / mode).glob('*.json'):
            assert 'ORD-REALISTIC-934' not in path.read_text(encoding='utf-8'), path
        assert 'ORD-REALISTIC-934' not in json.dumps(gateway.inputs)
    with pages() as base:
        asyncio.run(execute(base))


def test_model_gateway_corrects_structure_within_physical_budget():
    async def execute():
        class Client:
            def __init__(self):
                self.count = 0
            def with_structured_output(self, schema, **options):
                assert options['method'] == 'function_calling'
                return self
            async def ainvoke(self, messages):
                self.count += 1
                if self.count == 1:
                    try:
                        ExecutionDecision(status='action', reason='rejected', action={'kind': 'navigate', 'intent': 'PrivateRejectedCanary', 'target': 'node-1'})
                    except Exception as error:
                        return {'parsed': None, 'parsing_error': error}
                return {'parsed': ExecutionDecision(status='action', reason='corrected', action={
                    'kind': 'navigate', 'intent': 'follow link', 'value': 'https://example.test/path'}), 'raw': None}
        for cap in (1, 2):
            gateway = ModelGateway(Settings(deepseek_api_key='test-key'), max_calls=cap)
            gateway.providers = [('Fake', Client())]
            if cap == 1:
                with pytest.raises(RuntimeError, match='budget'):
                    await gateway.call('execute', ExecutionDecision, 'Use the schema', {'goal': 'protocol'})
            else:
                result = await gateway.call('execute', ExecutionDecision, 'Use the schema', {'goal': 'protocol'})
                assert result.action.value == 'https://example.test/path'
                assert gateway.calls[1]['correction'] == 1
                assert gateway.inputs[1]['payload']['previous_validation_errors']
            assert len(gateway.calls) == cap
            assert 'PrivateRejectedCanary' not in json.dumps([gateway.calls, gateway.inputs])
    asyncio.run(execute())
