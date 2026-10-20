import asyncio
from contextlib import redirect_stdout
import io
import json
from unittest.mock import AsyncMock, patch

from e2e_agent.config import Settings
from e2e_agent.core.agents import DesignDecision
from e2e_agent.core.resources.interaction import ResourceUnavailable
from e2e_agent.core.workflow import run
from resource_provider import ScriptedProvider
from test_universal_graph import Roles
from test_universal_tools import pages


def test_full_question_answer_survives_exploration_report_and_restart(tmp_path):
    class ExploreAfterReply(Roles):
        def __init__(self):
            super().__init__('clarify')
            self.contexts = []

        async def call(self, role, schema, prompt, payload):
            if role == 'design' and payload['answers']:
                self.contexts.append(payload['clarifications'])
                if len(self.contexts) == 1:
                    return DesignDecision(status='inspect', reason='复查已有页面', inspect_url=payload['target_url'])
            return await super().call(role, schema, prompt, payload)

    expected = [{'id': 'shipping_expectation', 'text': '订单应该处于什么状态？',
                 'reason': '需要验收依据', 'answer': '已发货'}]

    async def execute(base):
        gateway = ExploreAfterReply()
        result = await run(Settings(), target_url=base + '/orders', goal='查询订单', run_dir=tmp_path / 'first',
                           gateway=gateway, provider=ScriptedProvider({'shipping_expectation': '已发货', 'purchase_order': 'ORD-FICTIONAL'}))
        assert result['status'] == 'passed', result.get('error')
        assert gateway.contexts == [expected, expected]
        assert result['clarifications'] == expected
        saved = json.loads((tmp_path / 'first/result.json').read_text(encoding='utf-8'))
        assert saved['clarifications'] == expected
        assert '订单应该处于什么状态？' in (tmp_path / 'first/report.md').read_text(encoding='utf-8')
        restarted = await run(Settings(), target_url=base + '/orders', goal='查询订单', run_dir=tmp_path / 'restart',
                              frozen_plan=result['plan'], confirmed_answers=result['answers'],
                              confirmed_clarifications=result['clarifications'], gateway=Roles(),
                              provider=ScriptedProvider({'purchase_order': 'ORD-FICTIONAL-NEW'}))
        assert restarted['status'] == 'passed'
        assert restarted['clarifications'] == expected
        assert restarted['answers'] == {'shipping_expectation': '已发货'}

    with pages() as base:
        asyncio.run(execute(base))


def test_completed_replies_are_retained_when_later_reply_is_unavailable(tmp_path):
    class TwoQuestions(Roles):
        async def call(self, role, schema, prompt, payload):
            assert role == 'design'
            return DesignDecision(status='clarify', reason='需要两个独立规则', questions=[
                {'id': 'q1', 'text': '范围是什么？', 'reason': '确定范围'},
                {'id': 'q2', 'text': '预期是什么？', 'reason': '确定结果'}])

    async def execute(base):
        result = await run(Settings(), target_url=base + '/orders', goal='测试订单', run_dir=tmp_path / 'blocked',
                           gateway=TwoQuestions(), provider=ScriptedProvider({'q1': '只查我的订单', 'q2': ResourceUnavailable('no reply')}))
        assert result['status'] == 'blocked'
        assert result['answers'] == {'q1': '只查我的订单'}
        assert result['clarifications'] == [{'id': 'q1', 'text': '范围是什么？', 'reason': '确定范围', 'answer': '只查我的订单'}]
        assert not result['records']

    with pages() as base:
        asyncio.run(execute(base))


def test_cli_resume_passes_original_question_context(tmp_path):
    from e2e_agent.cli import main
    answers = {'q1': '无'}
    clarifications = [{'id': 'q1', 'text': '是否包含另一个业务分支？', 'reason': '确认范围', 'answer': '无'}]
    (tmp_path / 'result.json').write_text(json.dumps({'goal': '用户目标', 'target_url': 'https://example.test/',
        'plan': {'version': 1}, 'answers': answers, 'clarifications': clarifications}), encoding='utf-8')
    with patch('e2e_agent.cli.Settings.load', return_value=Settings()), \
         patch('e2e_agent.core.workflow.run', new_callable=AsyncMock,
               return_value={'status': 'passed', 'run_dir': str(tmp_path / 'new')}) as runner, redirect_stdout(io.StringIO()):
        assert main(['resume', str(tmp_path)]) == 0
    assert runner.call_args.kwargs['confirmed_answers'] == answers
    assert runner.call_args.kwargs['confirmed_clarifications'] == clarifications
