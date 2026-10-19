"""Predeclared cross-business evaluation with real models and independent replay."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import secrets
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from e2e_agent.config import Settings
from e2e_agent.core.workflow import run
from e2e_agent.core.resources.interaction import ResourceUnavailable
from e2e_agent.demo_sites import demo_server
from e2e_agent.sample_shop import shop_server


class DemoHuman:
    """Test-only human substitute. This vocabulary is not part of the engine."""
    def __init__(self, clarification=False, fresh_account=False):
        self.clarification, self.requests, self.values = clarification, [], []
        self.fresh_account, self.accounts = fresh_account, {}

    async def acquire(self, request):
        self.requests.append(request.audit())
        if request.kind == '业务澄清':
            if not self.clarification:
                raise ResourceUnavailable('Unexpected business clarification for an explicit goal')
            return '只覆盖密码登录的成功流程；使用有效账号和密码登录后应显示个人中心。'
        description = request.kind.lower()
        if not any(word in description for word in ('password', '密码', 'order', '订单', 'email', '邮箱', 'account', 'username', '账号', '账户', '用户名')):
            description = request.ref.lower()
        if any(word in description for word in ('password', '密码')):
            value = 'demo-password'
        elif any(word in description for word in ('order', '订单')):
            value = 'ORDER-FICTIONAL-' + str(len(self.values) + 100)
        elif any(word in description for word in ('email', '邮箱')):
            value = 'fictional-' + str(len(self.values)) + '@example.test'
        elif any(word in description for word in ('account', 'username', '账号', '账户', '用户名')):
            value = self.accounts.setdefault(request.run_id, 'account-' + secrets.token_hex(4)) if self.fresh_account else 'demo-user'
        else:
            raise ResourceUnavailable('No fictional value configured for this test resource')
        self.values.append(value)
        return value


async def verify(directory):
    settings = Settings.load()
    directory.mkdir(parents=True, exist_ok=False)
    report = {'stage': 'U4', 'real_model': True, 'status': 'running', 'cases': []}
    with ExitStack() as stack:
        base = stack.enter_context(demo_server())
        shop_base, store = stack.enter_context(shop_server())
        store.prepare('genericproof')
        cases = [
            ('natural-login', base + '/identity', '测试登录功能。', True, 'passed'),
            ('password-login', base + '/identity/password', '使用有效账号和密码登录，确认登录后显示“个人中心”。', False, 'passed'),
            ('order-lookup', base + '/orders', '查询我的订单，确认状态是“已发货”。', False, 'passed'),
            ('newsletter', base + '/newsletter', '订阅周刊，提交后应显示“订阅成功”。', False, 'passed'),
            ('public-number-input', 'https://the-internet.herokuapp.com/inputs', '在数字输入框中填写7，并确认输入框的值为7。', False, 'passed'),
            ('wrong-business-expectation', base + '/orders', '查询我的订单，确认状态是“待发货”。', False, 'failed'),
            ('register-login-cart', shop_base + '/shop/genericproof/login', '注册新账号后应显示“Registration successful”；使用同一账户密码登录后，页面应包含“Signed in:”身份提示；添加一本 Agent Handbook，购物车数量应为1，刷新后仍为1。', False, 'passed'),
        ]
        (directory / 'specification.json').write_text(json.dumps([
            {'id': name, 'url': url, 'goal': goal, 'clarification': clarify, 'expected': 'first-and-replay-' + expected}
            for name, url, goal, clarify, expected in cases], ensure_ascii=False, indent=2), encoding='utf-8')
        for name, url, goal, clarify, expected in cases:
            provider = DemoHuman(clarify, fresh_account=name == 'register-login-cart')
            result = await run(settings, target_url=url, goal=goal, run_dir=directory / name,
                provider=provider, max_decisions=30 if name == 'register-login-cart' else 20)
            calls = json.loads((directory / name / 'model-calls.json').read_text(encoding='utf-8'))
            leaks = []
            for path in (directory / name).rglob('*'):
                if path.is_file() and path.suffix.lower() in {'.json', '.py', '.html', '.md', '.log', '.xml'}:
                    text = path.read_text(encoding='utf-8', errors='replace')
                    if any(value and value in text for value in [*provider.values, settings.deepseek_api_key, settings.qwen_api_key]):
                        leaks.append(str(path.relative_to(directory / name)))
            observed_question = any(r['kind'] == '业务澄清' for r in provider.requests)
            passed = result['status'] == expected and result.get('complete_script') and result.get('replay_result', {}).get('status') == expected
            passed = passed and observed_question == clarify and not leaks
            if expected == 'failed':
                passed = passed and bool(result['assessments']) and any(c['role'] == 'diagnose' for c in calls)
            entry = {'id': name, 'status': 'passed' if passed else 'failed', 'expected': expected, 'actual': result['status'],
                'generation': result.get('generation_status'), 'replay': result.get('replay_result', {}).get('status'),
                'observed_clarification': observed_question, 'model_calls': len(calls), 'privacy_leaks': leaks,
                'error': result.get('error'), 'result': str(directory / name / 'result.json')}
            report['cases'].append(entry)
            (directory / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    report['status'] = 'passed' if all(c['status'] == 'passed' for c in report['cases']) else 'failed'
    report['total_model_calls'] = sum(c['model_calls'] for c in report['cases'])
    (directory / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    report = asyncio.run(verify(Path(sys.argv[1]).resolve()))
    print(json.dumps({'status': report['status'], 'total_model_calls': report['total_model_calls']}))
    raise SystemExit(0 if report['status'] == 'passed' else 1)
