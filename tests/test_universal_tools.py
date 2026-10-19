"""Cross-business mechanisms, with real Chromium and no language-model substitutes."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import pytest
from flask import Flask
from werkzeug.serving import make_server
from playwright.async_api import async_playwright
from pydantic import ValidationError
from e2e_agent.core.contracts import Action, Assertion, Plan, ResourceSpec
from e2e_agent.core.tools.browser import BrowserTools, ToolError, allowed_url, descriptor, find_descriptor, origin
from e2e_agent.core.resources import ResourceVault
from e2e_agent.resources import ScriptedProvider
from e2e_agent.core.resources.interaction import ResourceUnavailable, ResourceCancelled
from e2e_agent.core.resources.interaction import Redactor


@contextmanager
def pages(flaky=False):
    app = Flask(__name__)
    queries = 0
    @app.get('/newsletter')
    def newsletter():
        return '''<h1>订阅周刊</h1><label>收件邮箱<input id="mail" type="email"></label>
          <button onclick="document.querySelector('output').textContent='订阅成功'">订阅</button><output role="status"></output>'''
    @app.get('/orders')
    def orders():
        nonlocal queries
        queries += 1
        text = '待发货' if flaky and queries <= 2 else '已发货'
        return '''<h1>订单查询</h1><input aria-label="订单编号"><button onclick="document.querySelector('p').textContent='已发货'">查询订单</button><p></p>'''.replace("'已发货'", repr(text))
    @app.get('/preferences')
    def preferences():
        return '''<label>通知<input type="checkbox"></label><label>语言<select><option>中文</option><option>English</option></select></label><a href="/orders">订单入口</a>'''
    server = make_server('127.0.0.1', 0, app)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        worker.join(timeout=5)


def spec(identity, **kwargs):
    return ResourceSpec(id=identity, label=identity, purpose='本次业务输入', **kwargs)


def test_open_resource_names_and_plan_vocabulary():
    resources = tuple(spec(name) for name in ('receipt_email', 'purchase_order', 'invitation_token'))
    plan = Plan(requirements=[{'id': 'shipment', 'statement': '订单应已发货', 'source': 'user'}],
        scenarios=[{'id': 'lookup', 'objective': '查询订单', 'start_url': 'https://example.test/orders',
          'checkpoints': [{'id': 'delivery_result', 'objective': '检查发货结果', 'assertions': [
            {'id': 'shipping', 'requirement_id': 'shipment', 'target': '订单状态', 'operator': 'text', 'expected': '已发货'}]}]}],
        resources=resources)
    assert len(plan.resources) == 3
    malformed = plan.model_dump()
    malformed['scenarios'][0]['checkpoints'][0]['assertions'][0]['requirement_id'] = 'invented'
    with pytest.raises(ValidationError, match='unknown requirement'):
        Plan.model_validate(malformed)
    malformed = plan.model_dump()
    malformed['resources'] += (malformed['resources'][0],)
    with pytest.raises(ValidationError, match='Duplicate'):
        Plan.model_validate(malformed)


def test_resource_lifecycle_and_public_audit():
    async def run():
        provider = ScriptedProvider({'receipt_email': 'fictional@example.test', 'invitation_token': ['A7-B9', 'C1-D2']})
        vault = ResourceVault(provider)
        email, token = spec('receipt_email'), spec('invitation_token', reusable=False)
        await vault.acquire(email, 'scene', 'one')
        await vault.acquire(email, 'scene', 'two')
        assert len(provider.requests) == 1
        assert vault.resolve(email.id, 'scene') == 'fictional@example.test'
        assert not vault.has(email.id, 'other-scene')
        await vault.acquire(token, 'scene', 'three')
        assert vault.resolve(token.id, 'scene') == 'A7-B9'
        assert vault.expected_value(token.id, 'scene') == 'A7-B9'
        with pytest.raises(ResourceUnavailable):
            vault.resolve(token.id, 'scene')
        await vault.acquire(token, 'scene', 'four')
        assert vault.resolve(token.id, 'scene') == 'C1-D2'
        assert 'fictional@example.test' not in json.dumps(vault.events)
        assert 'fictional@example.test' not in vault.redactor.text('fictional@example.test')
        vault.clear_scene('scene')
        assert not vault.has(email.id, 'scene')
        with pytest.raises(ValueError, match='silently change'):
            vault.declare(spec('receipt_email', reusable=False))
        vault.close()
    asyncio.run(run())


@pytest.mark.parametrize('value', ['', ResourceCancelled('cancelled')])
def test_empty_and_cancelled_resources_block(value):
    async def run():
        vault = ResourceVault(ScriptedProvider({'purchase_order': value}))
        with pytest.raises(ResourceUnavailable):
            await vault.acquire(spec('purchase_order'), 'scene', 'step')
        assert not vault.has('purchase_order', 'scene')
        assert vault.events[-1]['status'] in {'blocked', 'cancelled'}
    asyncio.run(run())


def test_timeout_is_bounded_and_accounted_as_human_wait():
    async def run():
        vault = ResourceVault(ScriptedProvider({'opaque_field': 'answer'}, delay=.2), wait_timeout=.02)
        with pytest.raises(ResourceUnavailable, match='deadline'):
            await vault.acquire(spec('opaque_field'), 'scene', 'step')
        assert vault.clock.wait_seconds > 0
        assert vault.events[-1]['status'] == 'blocked'
    asyncio.run(run())


def test_url_scope_and_tool_arguments():
    assert allowed_url('/other', 'https://example.test/deep/path', {'https://example.test'}) == 'https://example.test/other'
    for value in ('javascript:alert(1)', 'https://outside.test/', 'https://user:pass@example.test/'):
        with pytest.raises(ToolError):
            allowed_url(value, 'https://example.test/', {'https://example.test'})
    with pytest.raises(ValidationError):
        Action(kind='fill', intent='输入', target='node-1', value='x', resource_id='y')


def test_resource_strings_cannot_corrupt_owned_protocol_metadata():
    redactor = Redactor()
    for value in ('passed', 'value', 'fill'):
        redactor.register(value)
    result = redactor.data({'status': 'passed', 'kind': 'fill', 'value': 'passed', 'error': 'private passed', 'nested': {'value': 'fill'}})
    assert result['status'] == 'passed' and result['kind'] == 'fill'
    assert result['value'] == '[RESOURCE]' and result['nested']['value'] == '[RESOURCE]'
    assert result['error'] == 'private [RESOURCE]'


def test_real_browser_handles_distinct_business_pages_without_core_changes():
    async def run(base):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(Path(__file__).resolve().parents[1] / '.browsers')
        vault = ResourceVault(ScriptedProvider({'receipt_email': 'fictional@example.test', 'purchase_order': 'ORD-X-2048'}))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chromium', headless=True)
            try:
                page = await browser.new_page()
                tools = BrowserTools(page, base, vault)
                for path, resource, label, button, message in (
                    ('newsletter', 'receipt_email', '收件邮箱', '订阅', '订阅成功'),
                    ('orders', 'purchase_order', '订单编号', '查询订单', '已发货')):
                    event = await tools.execute(Action(kind='navigate', value=base + '/' + path, intent='进入业务页面'), path)
                    assert event['status'] == 'passed'
                    snapshot = await tools.observe()
                    field = next(e for e in snapshot['elements'] if e['label'] == label)
                    saved = descriptor(field)
                    assert find_descriptor(snapshot, saved) == field['ref']
                    await vault.acquire(spec(resource), path, 'input')
                    event = await tools.execute(Action(kind='fill', target=field['ref'], resource_id=resource, intent='填写业务资源'), path)
                    assert event['status'] == 'passed'
                    assert vault.resolve(resource, path) == await page.locator('input').input_value()
                    check_input = Assertion(id='input', requirement_id='goal', target='用户输入', operator='value', expected={'resource_id': resource})
                    private_check = await tools.verify(check_input, field['ref'], path)
                    assert private_check['status'] == 'passed'
                    assert private_check['expected'] == {'resource_id': resource} and private_check['expected_resource_id'] == resource
                    assert vault.resolve(resource, path) not in json.dumps(await tools.observe())
                    control = next(e for e in tools.snapshot['elements'] if e['tag'] == 'button' and e['text'] == button)
                    assert (await tools.execute(Action(kind='click', target=control['ref'], intent='提交'), path))['status'] == 'passed'
                    snapshot = await tools.observe()
                    result = next(e for e in snapshot['elements'] if e['text'] == message)
                    assertion = Assertion(id='result', requirement_id='goal', target='结果提示', operator='text', expected=message)
                    assert (await tools.verify(assertion, result['ref']))['status'] == 'passed'
                    bad = assertion.model_copy(update={'expected': '不正确的业务结果'})
                    assert (await tools.verify(bad, result['ref']))['status'] == 'failed'
                assert (await tools.execute(Action(kind='navigate', value=base + '/preferences', intent='设置'), 'settings'))['status'] == 'passed'
                snapshot = await tools.observe()
                checkbox = next(e for e in snapshot['elements'] if e['type'] == 'checkbox')
                assert (await tools.verify(Assertion(id='initial', requirement_id='goal', target='通知', operator='checked', expected=False), checkbox['ref']))['status'] == 'passed'
                assert (await tools.execute(Action(kind='check', target=checkbox['ref'], intent='启用通知'), 'settings'))['status'] == 'passed'
                assert (await tools.verify(Assertion(id='updated', requirement_id='goal', target='通知', operator='checked', expected=True), checkbox['ref']))['status'] == 'passed'
                selection = next(e for e in snapshot['elements'] if e['tag'] == 'select')
                assert (await tools.execute(Action(kind='select', target=selection['ref'], value='English', intent='选择语言'), 'settings'))['status'] == 'passed'
                with pytest.raises(ToolError):
                    tools.target('node-999999')
                assert (await tools.execute(Action(kind='fill', target=checkbox['ref'], resource_id='missing', intent='缺少资源'), 'settings'))['status'] == 'error'
            finally:
                await browser.close()
                vault.close()
    with pages() as base:
        asyncio.run(run(base))
