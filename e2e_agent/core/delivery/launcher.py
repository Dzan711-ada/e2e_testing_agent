"""Parent console broker for an isolated pytest child. No model/config import."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
import xml.etree.ElementTree as ET
from ..contracts import Plan
from ..resources.interaction import ActiveClock, ConsoleProvider, Redactor, ResourceRequest


async def launch(directory, provider=None, *, timeout=120):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    plan = Plan.model_validate(manifest['plan'])
    existing = [directory / name for name in ('junit.xml', 'pytest.log', 'replay.json', 'replay-results') if (directory / name).exists()]
    if existing:
        history = directory / 'replay-history' / secrets.token_hex(8)
        history.mkdir(parents=True)
        for path in existing:
            shutil.move(str(path), str(history / path.name))
    specs = {s.id: s for s in plan.resources}
    scenes = {s.id for s in plan.scenarios}
    provider, clock, redactor = provider or ConsoleProvider(), ActiveClock(), Redactor()
    loop, token, events = asyncio.get_running_loop(), secrets.token_urlsafe(32), []

    async def acquire(payload):
        identity, scene = payload['resource_id'], payload['scene_id']
        if identity not in specs or scene not in scenes:
            raise ValueError('Unknown replay resource or scene')
        spec = specs[identity]
        request = ResourceRequest(secrets.token_hex(12), identity, spec.label, spec.purpose, scene,
            payload['step_id'], 'replay', sensitive=spec.sensitive, reusable=spec.reusable,
            wait_timeout=manifest['resource_wait_timeout'])
        with clock.waiting():
            value = await asyncio.wait_for(provider.acquire(request), timeout=request.wait_timeout)
        if not isinstance(value, str) or not value.strip() or len(value) > spec.max_length:
            raise ValueError('Invalid resource')
        redactor.register(value)
        events.append({'resource_id': identity, 'scene_id': scene, 'status': 'provided'})
        return value

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != '/acquire' or not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(403)
                return
            future = None
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('Invalid request size')
                payload = json.loads(self.rfile.read(length))
                future = asyncio.run_coroutine_threadsafe(acquire(payload), loop)
                value = future.result(timeout=manifest['resource_wait_timeout'] + 5)
                body = json.dumps({'value': value}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                if future is not None:
                    future.cancel()
                self.send_error(409, 'Resource unavailable')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'COMSPEC', 'PATHEXT'}}
    environment.update(PYTHONUTF8='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
        E2E_RESOURCE_BROKER=f'http://127.0.0.1:{server.server_port}', E2E_RESOURCE_TOKEN=token,
        PLAYWRIGHT_BROWSERS_PATH=manifest['browsers_path'])
    process = communication = None
    timed_out = False
    try:
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pytest', 'test_generated.py', '-q',
            '-p', 'no:cacheprovider', '--junitxml=junit.xml', cwd=directory, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        communication = asyncio.create_task(process.communicate())
        while not communication.done():
            await asyncio.wait({communication}, timeout=.1)
            if clock.active_seconds > timeout:
                timed_out = True
                process.kill()
                break
        stdout, _ = await communication
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
    (directory / 'pytest.log').write_text(redactor.text(stdout.decode('utf-8', errors='replace')), encoding='utf-8')
    counts = {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    names = []
    try:
        document = ET.parse(directory / 'junit.xml')
        for suite in document.getroot().iter('testsuite'):
            for key in counts:
                counts[key] += int(suite.get(key, 0))
        names = [c.get('name') for c in document.getroot().iter('testcase')]
    except (OSError, ET.ParseError):
        pass
    expected = [f'test_scenario[case-{i}]' for i in range(len(plan.scenarios))]
    valid_collection = sorted(names) == sorted(expected) and counts['tests'] == len(expected) and counts['skipped'] == 0
    status = ('passed' if process.returncode == 0 and valid_collection and not any(counts[k] for k in ('failures', 'errors'))
              else 'failed' if valid_collection and counts['failures'] else 'error')
    if timed_out:
        status = 'error'
    case_results = []
    for path in (directory / 'replay-results').glob('*.json'):
        case_results.append(json.loads(path.read_text(encoding='utf-8')))
    if valid_collection and any(c['status'] == 'blocked' for c in case_results):
        status = 'blocked'
    elif valid_collection and any(c['status'] == 'error' for c in case_results):
        status = 'error'
    result = {'status': status, 'exit_code': process.returncode, 'counts': counts, 'collected': names,
        'expected': expected, 'timed_out': timed_out, 'active_seconds': clock.active_seconds,
        'human_wait_seconds': clock.wait_seconds, 'resource_events': events,
        'case_statuses': {c['scenario_id']: c['status'] for c in case_results}}
    (directory / 'replay.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
