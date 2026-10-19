"""Generic DOM observations and bounded Playwright tools; no business adapters."""
from urllib.parse import urljoin, urlsplit
from playwright.async_api import expect
from ..contracts import Action, Assertion


class ToolError(ValueError):
    pass


def allowed_url(value, base, origins):
    url = urljoin(base, value)
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ToolError('A plain HTTP(S) URL is required')
    if f'{parsed.scheme}://{parsed.netloc}' not in origins:
        raise ToolError('Navigation outside the allowed origins')
    return url


def origin(url):
    parsed = urlsplit(url)
    return f'{parsed.scheme}://{parsed.netloc}'


_OBSERVE = '''elements => elements.filter(e => e.getClientRects().length).map(e => {
    const ref = e.getAttribute('data-e2e-agent-ref') || 'node-' + (++window.__e2eAgentSequence);
    e.setAttribute('data-e2e-agent-ref', ref);
    const labels = e.labels ? Array.from(e.labels).map(x => x.textContent.trim()).join(' ') : '';
    const form = e.form;
    return {ref, testid:e.getAttribute('data-testid') || '', tag:e.tagName.toLowerCase(), type:e.getAttribute('type') || '',
      role:e.getAttribute('role') || '', id:e.id || '', name:e.getAttribute('name') || '',
      label:labels || e.getAttribute('aria-label') || e.getAttribute('placeholder') || '',
      text:!['INPUT','TEXTAREA','SELECT'].includes(e.tagName) ? e.textContent.trim().slice(0,350) : '',
      href:e.tagName === 'A' ? e.getAttribute('href') : null,
      disabled:!!e.disabled, required:!!e.required,
      form:form ? (form.id || form.getAttribute('aria-label') || form.getAttribute('name') || '') : '',
      options:e.tagName === 'SELECT' ? Array.from(e.options).map(o => o.textContent.trim()).slice(0,40) : []};
})'''


async def observe(page, redact=lambda value: value):
    await page.evaluate('window.__e2eAgentSequence = window.__e2eAgentSequence || 0')
    items = await page.locator('input,textarea,select,button,a,[role],h1,h2,h3,p,output,span:not(:has(*)),div:not(:has(*)),[data-testid]').evaluate_all(_OBSERVE)
    # Body text supplies business context but is untrusted page data. Input values are never read here.
    body = await page.locator('body').inner_text(timeout=5000)
    return redact({'url': page.url, 'title': await page.title(), 'text': body[:12000],
                   'elements': items[:180], 'truncated': len(items) > 180 or len(body) > 12000})


def descriptor(element):
    return {key: element[key] for key in ('tag', 'type', 'role', 'id', 'name', 'label', 'text', 'form', 'testid')}


def find_descriptor(observation, target):
    matches = [item for item in observation['elements'] if all(descriptor(item).get(key) == value for key, value in target.items())]
    if len(matches) != 1:
        raise ToolError(f'Replay target must be unique; found {len(matches)}')
    return matches[0]['ref']


class BrowserTools:
    def __init__(self, page, start_url, vault, *, timeout_ms=10000):
        self.page, self.start_url, self.vault = page, start_url, vault
        self.origins = {origin(start_url)}
        self.timeout_ms = timeout_ms
        self.snapshot = None

    async def observe(self):
        self.snapshot = await observe(self.page, self.vault.redactor.data)
        return self.snapshot

    def target(self, ref):
        candidates = [item for item in (self.snapshot or {}).get('elements', []) if item['ref'] == ref]
        if len(candidates) != 1:
            raise ToolError('Target must be present and unique in the current observation')
        return candidates[0]

    def validate_binding(self, assertion, ref):
        if assertion.operator == 'url' or ref and ref.startswith('text:') and assertion.operator in {'visible', 'hidden', 'count', 'text', 'contains'}:
            return
        item = self.target(ref)
        if assertion.operator == 'value' and item['tag'] not in {'input', 'textarea', 'select'}:
            raise ToolError('A value assertion must target a form control, not result text')
        if assertion.operator == 'checked' and item['type'] not in {'checkbox', 'radio'}:
            raise ToolError('A checked assertion needs a checkbox or radio')

    def locator(self, ref):
        self.target(ref)
        # Only tool-generated references reach this selector, never arbitrary model CSS or code.
        if not ref.startswith('node-') or not ref[5:].isdigit():
            raise ToolError('Invalid observed reference')
        return self.page.locator(f'[data-e2e-agent-ref="{ref}"]')

    async def execute(self, action: Action, scene_id):
        record = {'action': action.model_dump(), 'status': 'running', 'dispatched': False, 'locator': None}
        try:
            if action.kind == 'navigate':
                url = allowed_url(action.value, self.page.url if self.page.url != 'about:blank' else self.start_url, self.origins)
                record['dispatched'] = True
                await self.page.goto(url, wait_until='domcontentloaded', timeout=self.timeout_ms)
            else:
                item = self.target(action.target)
                record['locator'] = descriptor(item)
                locator = self.locator(action.target)
                if await locator.count() != 1:
                    raise ToolError('Observed target is no longer unique')
                if action.kind == 'click':
                    destination = await locator.evaluate("e => e.getAttribute('href') || (e.form ? e.form.action : null)")
                    if destination:
                        allowed_url(destination, self.page.url, self.origins)
                if action.kind in {'fill', 'select'}:
                    if item['type'] == 'password' and action.resource_id is None:
                        raise ToolError('Password inputs require a resource reference')
                    value = self.vault.resolve(action.resource_id, scene_id) if action.resource_id else action.value
                if action.kind == 'wait':
                    await locator.wait_for(state='visible', timeout=self.timeout_ms)
                else:
                    record['dispatched'] = True
                    if action.kind == 'fill':
                        await locator.fill(value, timeout=self.timeout_ms)
                    elif action.kind == 'select':
                        await locator.select_option(label=value, timeout=self.timeout_ms)
                    elif action.kind == 'press':
                        await locator.press(action.value, timeout=self.timeout_ms)
                    else:
                        await getattr(locator, action.kind)(timeout=self.timeout_ms)
            allowed_url(self.page.url, self.start_url, self.origins)
            record['status'] = 'passed'
        except Exception as error:
            record.update(status='error', error=self.vault.redactor.text(f'{type(error).__name__}: {error}'))
        return self.vault.redactor.data(record)

    async def verify(self, assertion: Assertion, ref=None, scene_id=None):
        result = {'assertion_id': assertion.id, 'requirement_id': assertion.requirement_id,
                  'operator': assertion.operator, 'expected': assertion.expected.model_dump() if assertion.expected_resource_id else assertion.expected,
                  'expected_resource_id': assertion.expected_resource_id, 'status': 'running', 'locator': None}
        try:
            self.validate_binding(assertion, ref)
            expected = self.vault.expected_value(assertion.expected_resource_id, scene_id) if assertion.expected_resource_id else assertion.expected
            if assertion.operator == 'url':
                wanted = allowed_url(str(assertion.expected), self.start_url, self.origins)
                await expect(self.page).to_have_url(wanted, timeout=2000)
            else:
                if ref and ref.startswith('text:') and assertion.operator in {'visible', 'hidden', 'count', 'text', 'contains'}:
                    result['locator'] = {'text_query': ref[5:]}
                    locator = self.page.get_by_text(ref[5:], exact=True)
                else:
                    result['locator'] = descriptor(self.target(ref))
                    if assertion.operator in {'text', 'contains'}:
                        # Result text is the assertion's subject, not a stable locating attribute.
                        result['locator'].pop('text')
                    locator = self.locator(ref)
                checks = {'visible': 'to_be_visible', 'hidden': 'to_be_hidden', 'checked': 'to_be_checked', 'enabled': 'to_be_enabled'}
                if assertion.operator in checks:
                    method = checks[assertion.operator]
                    if not assertion.expected:
                        method = method.replace('to_', 'not_to_', 1)
                    await getattr(expect(locator), method)(timeout=2000)
                elif assertion.operator == 'count':
                    await expect(locator).to_have_count(assertion.expected, timeout=2000)
                elif assertion.operator == 'value':
                    await expect(locator).to_have_value(str(expected), timeout=2000)
                elif assertion.operator == 'contains':
                    await expect(locator).to_contain_text(str(expected), timeout=2000)
                else:
                    await expect(locator).to_have_text(str(expected), timeout=2000)
            result['status'] = 'passed'
        except Exception as error:
            result.update(status='failed', error=self.vault.redactor.text(f'{type(error).__name__}: {error}'))
        return self.vault.redactor.data(result)
