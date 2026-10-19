"""Deterministic replay of observed actions and frozen assertions, without a model."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from playwright.async_api import async_playwright
from ..tools.browser import BrowserTools, find_descriptor
from ..contracts import Action, Plan
from ..resources.interaction import ResourceUnavailable
from ..resources.vault import ResourceVault


class BrokerProvider:
    async def acquire(self, request):
        address, token = os.environ.get('E2E_RESOURCE_BROKER'), os.environ.get('E2E_RESOURCE_TOKEN')
        if not address or not token:
            raise ResourceUnavailable('Use run_with_resources.py to supply replay resources')
        def get():
            body = json.dumps({'resource_id': request.ref, 'scene_id': request.scene_id, 'step_id': request.step_id}).encode()
            call = Request(address + '/acquire', data=body, headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            try:
                with urlopen(call, timeout=request.wait_timeout + 10) as response:
                    result = json.load(response)
                if not isinstance(result.get('value'), str):
                    raise ResourceUnavailable('Resource broker did not return a string')
                return result['value']
            except Exception as error:
                raise ResourceUnavailable('Replay resource channel unavailable') from error
        return await asyncio.to_thread(get)


async def replay_scene(directory, scenario_id):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    plan = Plan.model_validate(manifest['plan'])
    scenario = next(s for s in plan.scenarios if s.id == scenario_id)
    recipe = next(s for s in manifest['scenarios'] if s['id'] == scenario_id)
    vault = ResourceVault(BrokerProvider(), wait_timeout=manifest['resource_wait_timeout'])
    vault.register_plan(plan)
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = manifest['browsers_path']
    output = {'scenario_id': scenario_id, 'status': 'running', 'events': [],
              'api_credentials_present': any(key.endswith('_API_KEY') for key in os.environ)}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chromium', headless=manifest['headless'])
            try:
                context = await browser.new_context()
                page = await context.new_page()
                tools = BrowserTools(page, recipe['url'], vault, timeout_ms=manifest['action_timeout_ms'])
                async def guard(route):
                    if route.request.is_navigation_request() and route.request.frame == page.main_frame:
                        from ..tools.browser import allowed_url
                        try:
                            allowed_url(route.request.url, recipe['url'], tools.origins)
                        except Exception:
                            await route.abort()
                            return
                    await route.continue_()
                await page.route('**/*', guard)
                await page.goto(recipe['url'], wait_until='domcontentloaded', timeout=manifest['action_timeout_ms'])
                checks = {(c.id, a.id): a for c in scenario.checkpoints for a in c.assertions}
                for step in recipe['steps']:
                    if step['kind'] == 'resource':
                        await vault.acquire(vault.specs[step['spec']['id']], scenario_id, step['checkpoint_id'])
                        output['events'].append({'kind': 'resource', 'resource_id': step['spec']['id'], 'status': 'provided'})
                        continue
                    observation = await tools.observe()
                    saved_locator = step.get('locator')
                    ref = (('text:' + saved_locator['text_query']) if saved_locator and 'text_query' in saved_locator else
                           find_descriptor(observation, saved_locator) if saved_locator else None)
                    if step['kind'] == 'action':
                        action = Action.model_validate(step['action'])
                        if action.target is not None:
                            action = action.model_copy(update={'target': ref})
                        event = await tools.execute(action, scenario_id)
                    else:
                        assertion = checks[(step['checkpoint_id'], step['assertion_id'])]
                        event = await tools.verify(assertion, ref, scenario_id)
                    output['events'].append(event)
                    if event['status'] != 'passed':
                        output['status'] = 'failed' if step['kind'] == 'assertion' else 'error'
                        break
                else:
                    output['status'] = 'passed'
            finally:
                await browser.close()
    except ResourceUnavailable:
        output.update(status='blocked', error='Required replay resource unavailable')
    except Exception as error:
        output.update(status='error', error=vault.redactor.text(type(error).__name__ + ': ' + str(error)))
    finally:
        output = vault.redactor.data(output)
        folder = directory / 'replay-results'
        folder.mkdir(exist_ok=True)
        filename = hashlib.sha256(scenario_id.encode()).hexdigest()[:16] + '.json'
        (folder / filename).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
        vault.close()
    return output
