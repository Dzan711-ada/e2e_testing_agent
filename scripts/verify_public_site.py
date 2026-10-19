"""G7 bounded practice adapter: real local protocol and separate public evidence.

Local mirror routing is an explicit test fixture. It never establishes public
site compatibility. The public branch has no route or conftest interception.
"""
import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from urllib.parse import quote, quote_plus, urljoin, urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask, redirect, request, session
from playwright.async_api import Browser, TimeoutError as BrowserTimeout, async_playwright
from werkzeug.serving import make_server

from e2e_agent import agent_workflow
from e2e_agent.agent_workflow import ScriptedAnswers, run_agent_suite
from e2e_agent.agents import RoleModel
from e2e_agent.config import Settings
from e2e_agent.graph_checkpoint import AsyncGraphSession
from e2e_agent.practice_resources import public_demo_resources
from e2e_agent.resources import ScriptedProvider
from e2e_agent.site_profiles import (PRACTICE_PROFILE, PRACTICE_START_URL,
    get_site_profile, profile_contract)
from scripts.verify_stage import configuration_fingerprints, sources

INPUT_VERSION = 1
QUERY = "验证公开练习站正确凭据登录成功，并检查错误密码被拒绝"
CASES = {"valid-login": "login", "invalid-password": "rejection"}
NETWORK_CODE = re.compile(r"net::ERR_(?:NAME_NOT_RESOLVED|NAME_RESOLUTION_FAILED|CONNECTION_[A-Z_]+|"
    r"CERT_[A-Z_]+|SSL_[A-Z_]+|TIMED_OUT|INTERNET_DISCONNECTED|ADDRESS_UNREACHABLE|"
    r"PROXY_CONNECTION_FAILED|TUNNEL_CONNECTION_FAILED)\b")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def reference(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def variants(value):
    return {value, quote(value, safe=""), quote_plus(value), repr(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value, ensure_ascii=True)[1:-1]}


def assert_private_free(blob, private):
    content = blob if isinstance(blob, bytes) else str(blob).encode("utf-8")
    for value in private:
        if value and any(part.encode("utf-8") in content for part in variants(value)):
            raise AssertionError("Resource or API value reached a prohibited evidence boundary")


def scan_files(directory, private):
    from e2e_agent.suite_reporting import is_generated_report
    checked = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix.lower() == ".png":
            continue
        assert path.suffix.lower() not in {".har", ".zip"}, "Raw browser evidence is prohibited"
        if path.suffix.lower() == ".html":
            assert is_generated_report(path), "Only the exact owned generated report HTML is allowed"
        assert_private_free(path.read_bytes(), private)
        checked[path.relative_to(directory).as_posix()] = reference(path)
    assert not (directory / "e2e_support/practice_resources.py").exists(), "A resource provider entered the export"
    return {"status": "passed", "files_scanned": len(checked), "manifest": checked,
        "resource_values_in_model_plan_support": 0, "screenshots": "Known resource/input masks; no OCR claim"}


def scan_saver(saver, private):
    blocks = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            blocks.append(bytes(value))
        elif isinstance(value, str):
            blocks.append(value.encode("utf-8"))
        elif value is not None and not isinstance(value, (int, float, bool)):
            raise AssertionError("A live object reached native checkpoint storage")
    visit(saver.storage)
    visit(saver.writes)
    digest = hashlib.sha256()
    for block in blocks:
        assert_private_free(block, private)
        digest.update(len(block).to_bytes(8, "big"))
        digest.update(block)
    return {"serialized_blocks_scanned": len(blocks), "storage_and_writes_sha256": digest.hexdigest(),
        "resource_value_leaks": 0}


class AuditedRoleModel(RoleModel):
    """Actual provider transport with a read-only input-boundary assertion."""
    def __init__(self, settings, private, **kwargs):
        super().__init__(settings, **kwargs)
        self.private, self.payload_checks = private, []

    def safe_payload(self, payload):
        safe = super().safe_payload(payload)
        assert_private_free(json.dumps(safe, ensure_ascii=False), self.private)
        return safe

    async def call(self, role, schema, system, payload):
        encoded = json.dumps(self.safe_payload(payload), ensure_ascii=False)
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        assert_private_free(system + schema_text + encoded, self.private)
        self.payload_checks.append({"role": role,
            "system_schema_payload_sha256": hashlib.sha256((system + schema_text + encoded).encode()).hexdigest(),
            "resource_value_leaks": 0})
        return await super().call(role, schema, system, payload)


class FixedPracticeRoles:
    """A deterministic role fixture, never reported as a real model call."""
    def __init__(self):
        self.calls, self.limit = [], 20

    async def call(self, role, schema, system, payload):
        self.calls.append({"role": role, "agent_id": {"design": "design", "execute": "execution"}.get(role),
            "status": "passed", "real": False, "input_scope": "fixed practice contract test fixture"})
        if role == "design":
            contract = profile_contract(PRACTICE_PROFILE)
            suite = {"schema_version": 2, "id": "practice-adapter", "fixture": None, "query": payload["query"],
                "budget": {"max_actions": payload["max_actions"], "max_model_calls": payload["model_call_limit"]},
                "resources": contract["resources"],
                "rules": [{"id": identity, "statement": "Check " + identity,
                    "source": {"kind": "contract", "reference": contract["source_id"]}} for identity in contract["cases"]],
                "scenarios": [{"id": identity, "description": "Independent " + identity,
                    "namespace": identity, "start_url": contract["url_root"], "initial_state": "empty",
                    "checkpoints": [{"id": identity, "description": "Check " + identity,
                        "acceptance": [{**condition, "rule_id": identity} for condition in case["acceptance"]]}],
                    "actions": []} for identity, case in contract["cases"].items()]}
            return schema.model_validate({"status": "ready", "suite": suite})
        if role != "execute" or "checkpoint" not in payload:
            raise AssertionError("The local positive protocol unexpectedly required another role")
        identity = payload["checkpoint"]["id"]
        password = "password" if identity == "login" else "wrong_password"
        return schema.model_validate({"actions": [
            {"id": "open", "checkpoint_id": identity, "kind": "navigate", "intent": "Open login", "value": "login"},
            {"id": "account", "checkpoint_id": identity, "kind": "fill", "intent": "Fill account reference",
                "target": "label:Username", "resource_id": "username", "field_role": "username"},
            {"id": "password", "checkpoint_id": identity, "kind": "fill", "intent": "Fill password reference",
                "target": "label:Password", "resource_id": password, "field_role": "password"},
            {"id": "submit", "checkpoint_id": identity, "kind": "click", "intent": "Submit login", "target": "button:Login"}]})


@contextmanager
def protocol_mirror():
    """A real HTTP fixture with form POST, session cookie and redirect semantics."""
    values = public_demo_resources(PRACTICE_PROFILE)
    app = Flask("practice-login-protocol-mirror")
    app.secret_key = secrets.token_hex(32)
    audit = []
    @app.before_request
    def record_request():
        audit.append({"method": request.method, "path": request.path})
    @app.get("/")
    def index():
        return '<a href="/login">Form Authentication</a>'
    @app.get("/login")
    def login():
        return '''<!doctype html><title>Practice protocol fixture</title>
<style>button::before { content: "\\21aa  "; }</style><h2>Login Page</h2>
<form id="login" name="login" action="/authenticate" method="post">
<label for="username">Username</label><input id="username" name="username" type="text">
<label for="password">Password</label><input id="password" name="password" type="password">
<button type="submit"><span>Login</span></button></form>'''
    @app.post("/authenticate")
    def authenticate():
        accepted = request.form.get("username") == values["username"] and request.form.get("password") == values["password"]
        session.clear()
        if accepted:
            session["authenticated"] = True
        audit.append({"method": "AUTH_RESULT", "path": "/authenticate", "accepted": accepted})
        return redirect("/secure" if accepted else "/login")
    @app.get("/secure")
    def secure():
        return '<style>a.logout::before { content: "\\21a9  "; }</style><h2>Secure Area</h2><a class="logout" href="/logout"><span>Logout</span></a>' if session.get("authenticated") else redirect("/login")
    @app.get("/logout")
    def logout():
        session.clear()
        return redirect("/login")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), audit
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive(), "Protocol fixture server did not close"


async def install_mirror_route(context, base, audit):
    profile = get_site_profile(PRACTICE_PROFILE)
    identity = secrets.token_hex(8)
    requests = {}
    def request_identity(request):
        if request not in requests:
            identifier = identity + ":" + str(len(requests) + 1)
            requests[request] = identifier
            parsed = urlsplit(request.url)
            audit.append({"kind": "browser_request", "request_id": identifier,
                "method": request.method, "origin": parsed.scheme + "://" + parsed.netloc,
                "path": parsed.path, "navigation": request.is_navigation_request()})
        return requests[request]
    context.on("request", request_identity)
    async def forward(route):
        identifier = request_identity(route.request)
        parsed = urlsplit(route.request.url)
        if parsed.scheme + "://" + parsed.netloc != profile.origin or parsed.path not in profile.allowed_paths or parsed.query:
            audit.append({"kind": "route_abort", "request_id": identifier,
                "method": route.request.method, "path": parsed.path,
                "status": "aborted", "reason": "outside fixture route scope"})
            await route.abort("blockedbyclient")
            return
        response = await route.fetch(url=base + parsed.path, max_redirects=0)
        headers = dict(response.headers)
        event = {"kind": "mirror_route", "request_id": identifier,
            "method": route.request.method, "path": parsed.path, "status": response.status,
            "session_cookie_header_present": "set-cookie" in headers, "redirect_bridge": False}
        if parsed.path == "/authenticate":
            assert response.status == 302 and "set-cookie" in headers, "Mirror auth lost its HTTP redirect/session-cookie protocol"
        if "location" in headers:
            location = urlsplit(urljoin(base + parsed.path, headers["location"]))
            assert location.scheme + "://" + location.netloc == base and location.path in profile.allowed_paths
            assert response.status == 302 and not location.query and not location.fragment
            destination = profile.origin + location.path
            event.update(redirect_bridge=True, destination_path=location.path, browser_response_status=200)
            audit.append(event)
            # Chromium skips route interception for a server redirect chain.
            # Preserve the real mirror's 302/cookie evidence but bridge it into
            # a NEW navigation so the next document must pass this route again.
            for name in ("location", "content-length", "content-encoding"):
                headers.pop(name, None)
            headers["content-type"] = "text/html; charset=utf-8"
            body = '<!doctype html><title>Local redirect bridge</title><script>location.replace(' + json.dumps(destination) + ')</script>'
            await route.fulfill(status=200, headers=headers, body=body)
            return
        audit.append(event)
        await route.fulfill(response=response, headers=headers)
    await context.route("**/*", forward)


# Self-contained test-only fixture: no original source import and no credentials.
MIRROR_CONFTEST = '''"""Explicit local protocol fixture; this directory is not public compatibility evidence."""
import json
from pathlib import Path
import secrets
from urllib.parse import urljoin, urlsplit
from unittest.mock import patch
import pytest
from playwright.async_api import Browser
ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "local-route-fixture.json").read_text(encoding="utf-8"))
@pytest.fixture(scope="session", autouse=True)
def local_practice_protocol_fixture():
    original = Browser.new_context
    async def new_context(browser, *args, **kwargs):
        kwargs["service_workers"] = "block"
        context = await original(browser, *args, **kwargs)
        identity, requests = secrets.token_hex(8), {}
        def record(event):
            with (ROOT / "local-replay-route-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\\n")
        def request_identity(request):
            if request not in requests:
                identifier = identity + ":" + str(len(requests) + 1)
                requests[request] = identifier
                parsed = urlsplit(request.url)
                record({"kind": "browser_request", "request_id": identifier,
                    "method": request.method, "origin": parsed.scheme + "://" + parsed.netloc,
                    "path": parsed.path, "navigation": request.is_navigation_request()})
            return requests[request]
        context.on("request", request_identity)
        async def forward(route):
            identifier = request_identity(route.request)
            parsed = urlsplit(route.request.url)
            if parsed.scheme + "://" + parsed.netloc != CONFIG["origin"] or parsed.path not in CONFIG["allowed_paths"] or parsed.query:
                record({"kind": "route_abort", "request_id": identifier,
                    "method": route.request.method, "path": parsed.path, "status": "aborted"})
                await route.abort("blockedbyclient")
                return
            response = await route.fetch(url=CONFIG["mirror_base"] + parsed.path, max_redirects=0)
            headers = dict(response.headers)
            event = {"kind": "mirror_route", "request_id": identifier,
                "method": route.request.method, "path": parsed.path, "status": response.status,
                "session_cookie_header_present": "set-cookie" in headers, "redirect_bridge": False}
            if parsed.path == "/authenticate":
                assert response.status == 302 and "set-cookie" in headers
            if "location" in headers:
                destination = urlsplit(urljoin(CONFIG["mirror_base"] + parsed.path, headers["location"]))
                assert destination.scheme + "://" + destination.netloc == CONFIG["mirror_base"]
                assert destination.path in CONFIG["allowed_paths"]
                assert response.status == 302 and not destination.query and not destination.fragment
                event.update(redirect_bridge=True, destination_path=destination.path, browser_response_status=200)
                record(event)
                for name in ("location", "content-length", "content-encoding"):
                    headers.pop(name, None)
                headers["content-type"] = "text/html; charset=utf-8"
                body = '<!doctype html><title>Local redirect bridge</title><script>location.replace(' + json.dumps(CONFIG["origin"] + destination.path) + ')</script>'
                await route.fulfill(status=200, headers=headers, body=body)
                return
            record(event)
            await route.fulfill(response=response, headers=headers)
        await context.route("**/*", forward)
        return context
    with patch.object(Browser, "new_context", new_context):
        yield
'''


@contextmanager
def local_protocol_routing(base, audit):
    original_context, original_replay = Browser.new_context, agent_workflow.run_replay
    async def new_context(browser, *args, **kwargs):
        kwargs["service_workers"] = "block"
        context = await original_context(browser, *args, **kwargs)
        await install_mirror_route(context, base, audit)
        return context
    async def replay_with_explicit_fixture(settings, directory, *, resource_provider=None):
        profile = get_site_profile(PRACTICE_PROFILE)
        assert not (directory / "conftest.py").exists()
        write(directory / "local-route-fixture.json", {"fixture": "local-protocol-mirror",
            "mirror_base": base, "origin": profile.origin, "allowed_paths": sorted(profile.allowed_paths)})
        (directory / "conftest.py").write_text(MIRROR_CONFTEST, encoding="utf-8")
        return await original_replay(settings, directory, resource_provider=resource_provider)
    with patch.object(Browser, "new_context", new_context), patch.object(agent_workflow, "run_replay", replay_with_explicit_fixture):
        yield


@contextmanager
def observe_native_stream(private, evidence):
    original = AsyncGraphSession.astream
    async def monitored(session, *args, **kwargs):
        try:
            async for update in original(session, *args, **kwargs):
                if kwargs.get("subgraphs") and isinstance(update, tuple) and len(update) == 2:
                    namespace, values = update
                    for node, value in values.items():
                        if node == "__interrupt__":
                            continue
                        evidence["updates"].append({"namespace": list(namespace), "node": node,
                            "scene_id": value.get("scene_id") if isinstance(value, dict) else None})
                yield update
        finally:
            evidence["native_saver"] = scan_saver(session.checkpointer, private)
            assert session.active_workers == 0, "Native graph worker survived stream shutdown"
    with patch.object(AsyncGraphSession, "astream", monitored):
        yield


def subsequence(nodes, expected):
    position = iter(nodes)
    return all(any(node == target for node in position) for target in expected)


def actual_events(directory, stem):
    values = read(directory / (stem + ".json"))
    lines = [json.loads(line) for line in (directory / (stem + ".jsonl")).read_text(encoding="utf-8").splitlines() if line]
    assert values and values == lines, "Production event records differ"
    assert [event["graph_event_index"] for event in values] == list(range(1, len(values) + 1))
    return values


def verify_mirror_route_audit(events):
    requests = {event["request_id"] for event in events if event["kind"] == "browser_request"}
    handled = {event["request_id"] for event in events if event["kind"] in {"mirror_route", "route_abort"}}
    assert requests and requests == handled, "A local browser request bypassed the explicit mirror route"
    routes = [event for event in events if event["kind"] == "mirror_route"]
    authenticated = [event for event in routes if event["path"] == "/authenticate" and event["method"] == "POST"]
    assert len(authenticated) == 2 and all(event["status"] == 302 and event["redirect_bridge"]
        and event["session_cookie_header_present"] for event in authenticated)
    assert {event["destination_path"] for event in authenticated} == {"/secure", "/login"}
    secure = [event for event in routes if event["path"] == "/secure"]
    assert len(secure) == 1 and secure[0]["status"] == 200, "Secure document did not come from the authenticated local mirror"
    return {"browser_requests": len(requests), "all_browser_requests_routed_or_aborted": True,
        "authentication_http_302_with_cookie": len(authenticated), "secure_documents_from_mirror": len(secure),
        "transport_limitation": "Test-only 302-to-200 JavaScript bridge starts a new routed browser navigation; this is not public HTTP redirect compatibility evidence"}


def verified_cases(result, directory, native):
    assert result["status"] == "passed" and result["complete_script"]
    assert result["replay"]["status"] == "passed" and result["replay"]["tests"] == 2
    assert not any(result["replay"].get(key, 0) for key in ("skipped", "failures", "errors", "blocked"))
    generation = read(directory / "generation/suite-result.json")
    replay = read(directory / "replay-suite/suite-result.json")
    assert generation["status"] == replay["status"] == "passed" and generation["run_id"] != replay["run_id"]
    assert generation["requirements_fingerprint"] == replay["requirements_fingerprint"]
    assert replay["model_calls"] == 0
    assert native["native_saver"]["serialized_blocks_scanned"] > 0
    parents = actual_events(directory, "parent-events")
    assert subsequence([event["node"] for event in parents], ["observe", "design", "execute", "export", "replay", "analyze"])
    child = actual_events(directory / "generation", "execution-events")
    replay_child = actual_events(directory / "replay-suite", "execution-events")
    plan = read(directory / "plan.json")["suite"]
    contract = profile_contract(PRACTICE_PROFILE)
    assert len(plan["scenarios"]) == 2 and plan["resources"]
    scenes = {scene["checkpoints"][0]["id"]: scene for scene in plan["scenarios"]}
    assert set(scenes) == set(CASES.values())
    contexts = []
    entries = []
    for identity, role in CASES.items():
        scene_id = scenes[role]["id"]
        expected = contract["cases"][role]["acceptance"]
        for record in (generation, replay):
            actual = record["scenarios"][scene_id]
            assert actual["status"] == "passed" and actual["contexts_created"] == 1
            contexts.append(actual["context_id"])
            checkpoints = actual["checkpoints"]
            assert len(checkpoints) == 1 and checkpoints[0]["status"] == "passed" and checkpoints[0]["id"] == role
            evidence = checkpoints[0]["evidence"]
            assert len(evidence) == len(expected) and all(item["status"] == "passed" for item in evidence)
            assert {(item["target"], item["operator"], item["expected"]) for item in evidence} == {
                (item["target"], item["operator"], item["expected"]) for item in expected}
        for events in (child, replay_child):
            nodes = [event["node"] for event in events if event.get("scene_id") == scene_id]
            assert subsequence(nodes, ["plan_actions", "execute_action", "verify_checkpoint", "complete_checkpoint"])
        namespaces = {tuple(event["namespace"]) for event in native["updates"]
            if event.get("scene_id") == scene_id and event["namespace"] and event["node"] == "execute_action"}
        assert namespaces and all(any(part.startswith("execute:") for part in namespace) for namespace in namespaces)
        entries.append({"id": identity, "actual_scenario_id": scene_id, "run_dir": str(directory.resolve()),
            "status": "passed", "native_graph_verified": True, "generation_status": "passed",
            "replay_status": "passed", "assertion_count": len(expected), "checkpoint_namespaces": [list(item) for item in sorted(namespaces)],
            "generation_result": reference(directory / "generation/suite-result.json"),
            "replay_result": reference(directory / "replay-suite/suite-result.json"),
            "junit": reference(directory / "junit.xml"), "agent_result": reference(directory / "agent-result.json")})
    assert len(set(contexts)) == 4, "Cases or independent replay reused a browser context"
    return entries


async def execute_bundle(settings, directory, *, real_model, private):
    model = AuditedRoleModel(settings, private, audit_path=directory / "model-calls.json") if real_model else FixedPracticeRoles()
    provider = ScriptedProvider(public_demo_resources(PRACTICE_PROFILE))
    native = {"updates": []}
    try:
        with observe_native_stream(private, native):
            result = await run_agent_suite(settings, query=QUERY, run_dir=directory, site_profile=PRACTICE_PROFILE,
                transport=model, resource_provider=provider, answers_provider=ScriptedAnswers([]), repair=False)
    finally:
        if directory.exists():
            write(directory / "native-stream-validation.json", native)
            write(directory / "validation-role-calls.json", model.calls)
            if real_model:
                write(directory / "model-input-boundary.json", model.payload_checks)
    return result, model.calls, native


async def public_preflight(settings):
    """Only connection failures before an HTTP document response can block this probe."""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(settings.browsers_path)
    profile = get_site_profile(PRACTICE_PROFILE)
    responded = []
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel="chromium", headless=settings.headless)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            page.on("response", lambda response: responded.append(response.status)
                if response.request.is_navigation_request() else None)
            try:
                response = await page.goto(profile.login_url, wait_until="domcontentloaded", timeout=15000)
            except Exception as error:
                code = NETWORK_CODE.search(str(error))
                if not responded and (code or isinstance(error, BrowserTimeout)):
                    return {"status": "blocked", "reason": code.group(0) if code else "navigation_connection_timeout", "http_response_received": False}
                raise
            assert response is not None and response.status < 400, "The public endpoint returned an HTTP error"
            assert page.url == profile.login_url, "The public endpoint redirected outside the frozen login URL"
            return {"status": "reachable", "url": profile.login_url, "http_status": response.status}
        finally:
            await browser.close()


def failure_cases(directory, status):
    result = read(directory / "generation/suite-result.json") if (directory / "generation/suite-result.json").is_file() else {}
    mapping = {}
    for scene_id, scene in result.get("scenarios", {}).items():
        for checkpoint in scene.get("checkpoints", []):
            mapping[checkpoint["id"]] = scene_id
    return [{"id": identity, "actual_scenario_id": mapping.get(role), "run_dir": str(directory.resolve()),
        "status": status, "native_graph_verified": False,
        "agent_result": reference(directory / "agent-result.json") if (directory / "agent-result.json").is_file() else None}
        for identity, role in CASES.items()]


def network_only_execution_failure(directory):
    """A business/assertion/model failure never becomes public-network blocking."""
    path = directory / "generation/suite-result.json"
    if not path.is_file():
        return False
    record = read(path)
    if record.get("status") == "passed":
        replay = directory / "replay-suite/suite-result.json"
        if not replay.is_file():
            return False
        replay_record = read(replay)
        if (replay_record.get("status") == "passed" or not replay_record.get("run_id")
                or replay_record.get("run_id") == record.get("run_id")
                or replay_record.get("requirements_fingerprint") != record.get("requirements_fingerprint")):
            return False
        record = replay_record
    network = []
    for scene in record.get("scenarios", {}).values():
        if scene.get("status") == "passed":
            continue
        for checkpoint in scene.get("checkpoints", []):
            if any(item.get("status") == "failed" for item in checkpoint.get("evidence", [])):
                return False
            if checkpoint.get("status") in {"error", "failed"}:
                errors = [event.get("error", "") for event in scene.get("events", []) if event.get("status") == "error" or event.get("ok") is False]
                if not errors or not all(NETWORK_CODE.search(error) for error in errors):
                    return False
                failures = [item for item in scene.get("network", []) if item.get("failure") and item.get("resource_type") == "document"]
                if not failures or not all(urlsplit(item.get("url", "")).hostname == "the-internet.herokuapp.com" for item in failures):
                    return False
                network.extend(failures)
    return bool(network)


async def verify(output):
    settings = Settings.load()
    settings.require_model()
    private = [*public_demo_resources(PRACTICE_PROFILE).values(), settings.deepseek_api_key, settings.qwen_api_key]
    report = {"schema_version": 1, "input_version": INPUT_VERSION, "stage": "G7", "status": "failed",
        "site_profile": PRACTICE_PROFILE, "query": QUERY, "contract": profile_contract(PRACTICE_PROFILE),
        "real_model": True, "local_status": "failed", "local_cases": [], "public_attempted": False,
        "public_status": "failed", "public_cases": [], "compatibility_verified": False, "model_calls": 0,
        "model_attempts": [], "local_run_dir": str((output / "local-mirror").resolve()),
        "public_run_dir": str((output / "public-site").resolve()),
        "source_files": sources(), "protected_files": configuration_fingerprints()}
    public_directory, local_directory = output / "public-site", output / "local-mirror"
    try:
        with protocol_mirror() as (base, http_audit):
            route_audit = []
            try:
                with local_protocol_routing(base, route_audit):
                    result, calls, native = await execute_bundle(settings, local_directory, real_model=False, private=private)
            finally:
                if local_directory.exists():
                    write(local_directory / "local-protocol-http-events.json", http_audit)
                    write(local_directory / "local-generation-route-events.json", route_audit)
            report["local_cases"] = verified_cases(result, local_directory, native)
            replay_routes = [json.loads(line) for line in (local_directory / "local-replay-route-events.jsonl").read_text(encoding="utf-8").splitlines() if line]
            assert {item.get("accepted") for item in http_audit if item["method"] == "AUTH_RESULT"} == {True, False}
            assert sum(item["method"] == "AUTH_RESULT" for item in http_audit) == 4, "Mirror did not see generation and independent replay authentication"
            report["local_generation_transport"] = verify_mirror_route_audit(route_audit)
            report["local_replay_transport"] = verify_mirror_route_audit(replay_routes)
            report.update(local_status="passed", local_role_calls=calls,
                local_fixture="Explicit local Flask mirror with audited real HTTP 302/cookies; test-only redirect bridge forces fresh routed browser navigation in generation and independent pytest",
                local_privacy=scan_files(local_directory, private))
        report["public_attempted"] = True
        preflight = await public_preflight(settings)
        report["public_connectivity"] = preflight
        if preflight["status"] == "blocked":
            report.update(status="blocked_public", public_status="blocked",
                public_cases=failure_cases(public_directory, "blocked"))
            return report
        assert not public_directory.exists(), "Public run directory must be fresh"
        assert not (public_directory / "conftest.py").exists()
        result, calls, native = await execute_bundle(settings, public_directory, real_model=True, private=private)
        report.update(model_calls=len(calls), model_attempts=calls,
            model_call_refs=[reference(public_directory / "model-calls.json"), reference(public_directory / "model-input-boundary.json")])
        assert calls and {call["role"] for call in calls} >= {"design", "execute"}
        assert all(call.get("provider") and call.get("real") is not False for call in calls), "Public run did not use actual providers"
        assert not (public_directory / "conftest.py").exists() and not (public_directory / "local-route-fixture.json").exists()
        if result["status"] != "passed":
            blocked = network_only_execution_failure(public_directory)
            report.update(status="blocked_public" if blocked else "failed", public_status="blocked" if blocked else "failed",
                public_cases=failure_cases(public_directory, "blocked" if blocked else "failed"),
                public_privacy=scan_files(public_directory, private))
            return report
        report["public_cases"] = verified_cases(result, public_directory, native)
        report.update(public_status="passed", compatibility_verified=True,
            public_privacy=scan_files(public_directory, private), status="passed")
    except Exception as error:
        detail = settings.redact(type(error).__name__ + ": " + str(error))
        for value in public_demo_resources(PRACTICE_PROFILE).values():
            for spelling in variants(value):
                detail = detail.replace(spelling, "[RESOURCE]")
        report.update(status="failed", error=detail[:2000], compatibility_verified=False)
        if report["local_status"] != "passed":
            report["local_cases"] = failure_cases(local_directory, "failed")
        elif report["public_attempted"]:
            report.update(public_status="failed", public_cases=failure_cases(public_directory, "failed"))
        audit = public_directory / "validation-role-calls.json"
        if audit.is_file():
            report["model_attempts"] = read(audit)
            report["model_calls"] = len(report["model_attempts"])
            report["model_call_refs"] = [reference(audit)]
    finally:
        report["source_files_after"] = sources()
        report["protected_files_after"] = configuration_fingerprints()
        report["source_changes_during_verification"] = sorted(key for key in report["source_files"].keys() | report["source_files_after"].keys()
            if report["source_files"].get(key) != report["source_files_after"].get(key))
        if report["source_changes_during_verification"] or report["protected_files"] != report["protected_files_after"]:
            report.update(status="failed", compatibility_verified=False, integrity_error="Source or configuration changed during public-site validation")
        assert_private_free(json.dumps(report, ensure_ascii=False), private)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    report = asyncio.run(verify(output))
    write(output / "public-site-validation.json", report)
    print(json.dumps({key: report[key] for key in ("status", "local_status", "public_status", "compatibility_verified", "model_calls")}))
    return 0 if report["status"] == "passed" else 2 if report["status"] == "blocked_public" else 1


if __name__ == "__main__":
    raise SystemExit(main())
