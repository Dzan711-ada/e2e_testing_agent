"""Real-model design evaluation: natural goals, not keyword-triggered questions."""
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from e2e_agent.config import Settings
from e2e_agent.core.workflow import Host, validate_plan
from e2e_agent.core.contracts import Action
from e2e_agent.demo_sites import demo_server


async def verify(directory):
    settings = Settings.load()
    directory.mkdir(parents=True, exist_ok=False)
    report = {'stage': 'U2', 'real_model': True, 'cases': [], 'status': 'running'}
    with demo_server() as base:
        for name, path, goal, expect_question in (
            ('natural-ambiguous', '/identity', '测试登录功能。', True),
            ('explicit-scope', '/identity', '只测试密码登录成功：使用有效凭据登录后，应显示“个人中心”。', False),
            ('resource-is-not-business-question', '/orders', '查询我的订单，确认状态是“已发货”。', False)):
            folder = directory / name
            folder.mkdir()
            host = Host(settings, folder, None, None, max_decisions=8)
            state = {'goal': goal, 'target_url': base + path, 'observations': [], 'answers': {}, 'clarifications': [], 'plan': None}
            entry = {'id': name, 'query': goal, 'expect_clarification': expect_question, 'decisions': [], 'status': 'running'}
            report['cases'].append(entry)
            try:
                await host.open(state['target_url'])
                observed_question = False
                ready = None
                for step in range(6):
                    observation = await host.tools.observe()
                    state['observations'].append(observation)
                    decision = await host.design.decide({**state, 'budget': {'remaining_actions': settings.max_actions}})
                    entry['decisions'].append(decision.model_dump(mode='json'))
                    host.save('decisions.json', entry)
                    if decision.status == 'inspect':
                        from e2e_agent.core.workflow import known_urls
                        if decision.inspect_url not in known_urls(state, host.tools):
                            raise AssertionError('Model invented an inspection URL')
                        event = await host.tools.execute(Action(kind='navigate', intent='查看实际页面', value=decision.inspect_url), 'design')
                        assert event['status'] == 'passed'
                    elif decision.status == 'clarify':
                        assert expect_question, 'Explicit scope/resource-only input caused an unnecessary business question'
                        assert not observed_question, 'Model repeated clarification after a complete answer'
                        observed_question = True
                        for question in decision.questions:
                            state['answers'][question.id] = '两种登录方式都覆盖，各自使用有效凭据登录后应显示个人中心；只测试成功流程。'
                            state['clarifications'].append({**question.model_dump(), 'answer': state['answers'][question.id]})
                    elif decision.status == 'ready':
                        validate_plan(decision.plan, state, host.tools)
                        ready = decision.plan
                        break
                    else:
                        raise AssertionError('Model blocked a supported natural request')
                assert observed_question == expect_question
                assert ready is not None, 'No complete plan within decision budget'
                if expect_question:
                    assert len(ready.scenarios) >= 2, 'Answer requesting both login methods was not represented'
                    assert any(r.source.startswith('answer:') for r in ready.requirements), 'Clarification not cited in the plan'
                if name == 'resource-is-not-business-question':
                    assert ready.resources, 'Order identifier should be a resource reference'
                entry['status'] = 'passed'
            except Exception as error:
                entry.update(status='failed', error=type(error).__name__ + ': ' + str(error))
            finally:
                await host.close()
                host.save('model-calls.json', host.gateway.calls)
                host.save('model-inputs.json', host.gateway.inputs)
                host.save('decisions.json', entry)
                entry['model_calls'] = len(host.gateway.calls)
                host.vault.close()
            print(json.dumps({'case': name, 'status': entry['status'], 'model_calls': entry['model_calls']}, ensure_ascii=False), flush=True)
            (directory / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    report['status'] = 'passed' if all(c['status'] == 'passed' for c in report['cases']) else 'failed'
    (directory / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    result = asyncio.run(verify(Path(sys.argv[1]).resolve()))
    print(json.dumps({'status': result['status'], 'cases': len(result['cases'])}))
    raise SystemExit(0 if result['status'] == 'passed' else 1)
