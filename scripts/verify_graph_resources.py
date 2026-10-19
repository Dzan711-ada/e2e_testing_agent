"""R3: native waits on the actual execution graph, real browser and volatile saver."""
import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, quote_plus
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e_agent.config import Settings
from e2e_agent.coordinator import execute_suite
from e2e_agent.domain import Action, Suite
from e2e_agent.execution_graph import build_execution_graph, recursion_limit
from e2e_agent.execution_runtime import RunRuntime
from e2e_agent.graph_checkpoint import AsyncGraphSession, AsyncNodeBridge, MemorySaver
from e2e_agent.graph_state import initial_execution_state
from e2e_agent.resources import Redactor
from e2e_agent.sms_demo import sms_suite
from e2e_agent.suite_codegen import export_suite
from e2e_agent.suite_demo import shop_suite
from scripts.verify_execution_graph import read_actual_graph
from scripts.verify_resources import CapturingProvider, scan
from scripts.verify_workflow_compat import descendants, process_tree

INPUT_VERSION = 1
CASES = (
    "native-phone-pause-resume", "native-otp-pause-resume", "native-cancel-provider-wait",
    "native-checkpoint-run-isolation", "native-expired-challenge-blocked",
    "sensitive-static-action-refused", "sensitive-dynamic-action-refused",
)


def private_scan_saver(saver, secrets):
    """Inspect real serialized storage and writes without saving their raw bytes."""
    blocks = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit(item)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            blocks.append(bytes(value))
        elif isinstance(value, str):
            blocks.append(value.encode("utf-8"))
        elif value is not None and not isinstance(value, (int, float, bool)):
            raise AssertionError("Live object reached serialized saver storage: " + type(value).__name__)
    visit(saver.storage)
    visit(saver.writes)
    for secret in set(secrets):
        if secret:
            for variant in {secret, quote(secret, safe=""), quote_plus(secret)}:
                assert not any(variant.encode("utf-8") in block for block in blocks), "Private sentinel reached native saver bytes"
    digest = hashlib.sha256()
    for block in blocks:
        digest.update(len(block).to_bytes(8, "big"))
        digest.update(block)
    return {"serialized_blocks_scanned": len(blocks), "storage_and_writes_sha256": digest.hexdigest(), "plaintext_resource_leaks": 0}


def initial_report():
    return {"schema_version": 1, "stage": "R3", "input_version": INPUT_VERSION, "status": "running",
            "full_stage_specialty": True, "real_model": False, "model_calls": 0,
            "actual_console_input": False, "native_human_resume_verified": False,
            "separated_repair_roles_verified": False, "scenarios": []}


class NativeRun:
    def __init__(self, settings, output, secrets, *, provider=None, saver=None, suite=None, planner=None):
        phone = "155" + str(int(uuid.uuid4().hex, 16) % 100000000).zfill(8)
        secrets.append(phone)
        self.secrets = secrets
        self.provider = provider or CapturingProvider(secrets, {"phone": phone}, use_test_delivery=True)
        self.suite = suite or sms_suite()
        self.runtime = RunRuntime(self.suite, output, settings.browsers_path, headless=settings.headless,
                                  resource_provider=self.provider, resource_wait_timeout=30, public_fictional=True)
        self.saver, self.planner = (MemorySaver() if saver is None else saver), planner
        self.config = {"configurable": {"thread_id": self.runtime.run_id}, "recursion_limit": recursion_limit(self.suite)}
        self.session = None

    async def __aenter__(self):
        try:
            await self.runtime.open()
            bridge = AsyncNodeBridge()
            graph = build_execution_graph(self.runtime, bridge=bridge, action_planner=self.planner,
                                          checkpointer=self.saver, interrupt_resources=True)
            self.session = AsyncGraphSession(graph, bridge)
        except BaseException:
            await self.runtime.close()
            raise
        return self

    async def __aexit__(self, *args):
        await self.runtime.close()
        assert self.session.active_workers == 0, "Native graph worker leaked"

    async def invoke(self, *, first=False):
        value = initial_execution_state(self.suite, self.runtime.run_id, redact=self.runtime.manager.redactor.data) if first else None
        await self.session.ainvoke(value, self.config)
        snapshot = await self.session.aget_state(self.config)
        json.dumps(snapshot.values, ensure_ascii=False, allow_nan=False)
        return snapshot

    async def pause(self, kind, *, first=False):
        snapshot = await self.invoke(first=first)
        assert snapshot.next == ("await_resource",), "Production graph did not pause before the resource node"
        pending = snapshot.values["pending_resource"]
        assert pending["kind"] == kind and pending["status"] == "waiting_user"
        assert pending["run_id"] == self.runtime.run_id and pending["scene_id"] == "sms-chain"
        assert pending["logical_request_id"] and pending["ref"] in {"phone", "sms_code"}
        assert snapshot.values["execution_status"] == "waiting_user"
        json_path = self.runtime.output_dir / ("native-" + kind + "-pause.json")
        json_path.write_text(json.dumps(snapshot.values, ensure_ascii=False, indent=2), encoding="utf-8")
        if kind == "sms_code":
            assert pending["challenge_id"] and pending["phone_ref"] == "phone" and pending["expires_at"] > time.time()
            self.secrets.append(self.runtime.store.test_code(pending["challenge_id"]))
        private_scan_saver(self.saver, self.secrets)
        return snapshot

    async def complete(self):
        final = await self.invoke()
        assert not final.next
        result = final.values["execution"]
        assert result["requirements_fingerprint"] == self.suite.requirements_fingerprint()
        read_actual_graph(self.runtime.output_dir, result)
        (self.runtime.output_dir / "suite-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


def sms_success(run, result, context):
    assert result["status"] == "passed"
    scene = result["scenarios"]["sms-chain"]
    assert scene["stats"] == {"send_count": 1, "login_count": 1}
    assert scene["contexts_created"] == 1 and scene["context_id"] == context
    assert {checkpoint["context_id"] for checkpoint in scene["checkpoints"]} == {context}
    for identity in ("send-code", "verify-code"):
        assert sum(event["action_id"] == identity for event in scene["events"]) == 1
    requests = run.provider.requests
    assert [request["kind"] for request in requests] == ["phone", "sms_code"]
    assert requests[1]["challenge_id"] and requests[1]["run_id"] == result["run_id"]


def sensitive_suite(secret, *, empty=False):
    data = shop_suite().model_dump()
    scene = data["scenarios"][0]
    scene["checkpoints"] = [scene["checkpoints"][0]]
    scene["actions"] = [] if empty else [{"id": "unsafe", "checkpoint_id": "registration", "kind": "fill",
        "intent": "Fill password", "target": "label:Password", "value": secret, "field_role": "password"}]
    data["scenarios"] = [scene]
    data["rules"] = [data["rules"][0]]
    return Suite.model_validate(data)


async def verify(output, report, secrets):
    settings = Settings.load()
    secrets.extend([settings.deepseek_api_key, settings.qwen_api_key])
    for identity in CASES:
        report["current_case"] = identity
        directory = output / identity
        evidence = {}
        if identity in {"native-phone-pause-resume", "native-otp-pause-resume", "native-expired-challenge-blocked"}:
            data = sms_suite().model_dump()
            if identity == "native-phone-pause-resume":
                data["budget"]["max_seconds"] = 10
            async with NativeRun(settings, directory, secrets, suite=Suite.model_validate(data)) as run:
                phone_pause = await run.pause("phone", first=True)
                context = phone_pause.values["context_id"]
                assert not run.provider.requests, "Provider ran before native resume"
                if identity == "native-phone-pause-resume":
                    active_before = run.runtime.clock.active_seconds
                    await asyncio.sleep(11)
                    assert run.runtime.clock.active_seconds < 10
                    assert run.runtime.clock.active_seconds - active_before < 1, "Native waiting consumed the active execution budget"
                code_pause = await run.pause("sms_code")
                pending = code_pause.values["pending_resource"]
                assert code_pause.values["context_id"] == context
                assert run.runtime.store.stats(run.runtime.session.namespace) == {"send_count": 1, "login_count": 0}
                assert [request["kind"] for request in run.provider.requests] == ["phone"]
                if identity == "native-expired-challenge-blocked":
                    with run.runtime.store.connection() as database:
                        database.execute("UPDATE challenges SET expires=? WHERE id=?", (time.time() - 1, pending["challenge_id"]))
                    result = await run.complete()
                    assert result["status"] == "blocked"
                    assert result["scenarios"]["sms-chain"]["stats"] == {"send_count": 1, "login_count": 0}
                    assert not any(request["kind"] == "sms_code" for request in run.provider.requests)
                    assert not any(event["action_id"] in {"fill-code", "verify-code"} for event in result["scenarios"]["sms-chain"]["events"])
                else:
                    result = await run.complete()
                    sms_success(run, result, context)
                    if identity == "native-phone-pause-resume":
                        assert run.runtime.clock.wait_seconds >= 11 and run.runtime.clock.active_seconds < 10
                        export_suite(run.suite, directory / "exported", settings.browsers_path)
                evidence = {"run_id": run.runtime.run_id, "context_id": context,
                            "challenge_id": pending["challenge_id"], "execution_status": result["status"],
                            "human_wait_seconds": run.runtime.clock.wait_seconds, "active_seconds": run.runtime.clock.active_seconds,
                            "saver_privacy": private_scan_saver(run.saver, secrets)}
        elif identity == "native-checkpoint-run-isolation":
            saver = MemorySaver()
            async with NativeRun(settings, directory / "first", secrets, saver=saver) as first, NativeRun(settings, directory / "second", secrets, saver=saver) as second:
                a = await first.pause("phone", first=True)
                b = await second.pause("phone", first=True)
                assert first.runtime.run_id != second.runtime.run_id
                assert a.values["pending_resource"]["logical_request_id"] != b.values["pending_resource"]["logical_request_id"]
                await second.pause("sms_code")
                preserved = await first.session.aget_state(first.config)
                assert preserved.next == a.next and preserved.values == a.values
                assert not first.provider.requests and [request["kind"] for request in second.provider.requests] == ["phone"]
                assert first.runtime.store.stats(first.runtime.session.namespace) == {"send_count": 0, "login_count": 0}
                assert second.runtime.store.stats(second.runtime.session.namespace) == {"send_count": 1, "login_count": 0}
                evidence = {"run_ids": [first.runtime.run_id, second.runtime.run_id], "saver_privacy": private_scan_saver(saver, secrets)}
        elif identity == "native-cancel-provider-wait":
            class PendingProvider:
                def __init__(self):
                    self.started, self.closed, self.requests = asyncio.Event(), False, []
                async def acquire(self, request):
                    self.requests.append(request.audit())
                    self.started.set()
                    try:
                        await asyncio.Future()
                    finally:
                        self.closed = True
            provider = PendingProvider()
            before = descendants(await asyncio.to_thread(process_tree), os.getpid())
            owned = set()
            async with NativeRun(settings, directory, secrets, provider=provider) as run:
                await run.pause("phone", first=True)
                pending = asyncio.create_task(run.session.ainvoke(None, run.config))
                try:
                    await asyncio.wait_for(provider.started.wait(), 10)
                    owned = descendants(await asyncio.to_thread(process_tree), os.getpid()) - before
                    assert len(owned) > 1, "No real Playwright browser process ownership was observed"
                finally:
                    pending.cancel()
                    try:
                        await pending
                        raise AssertionError("Cancellation was swallowed")
                    except asyncio.CancelledError:
                        pass
                assert run.session.active_workers == 0 and provider.closed
                assert run.runtime.manager.events[-1]["status"] == "cancelled"
                evidence = {"run_id": run.runtime.run_id, "owned_process_count": len(owned), "provider_closed": True,
                            "active_graph_workers": 0, "saver_privacy": private_scan_saver(run.saver, secrets)}
            assert not owned & set(await asyncio.to_thread(process_tree)), "Owned browser process survived cancellation cleanup"
            evidence["remaining_owned_processes"] = []
        elif identity == "sensitive-static-action-refused":
            secret = "StaticPrivate_" + uuid.uuid4().hex
            secrets.append(secret)
            savers = []
            def saver_factory():
                saver = MemorySaver()
                savers.append(saver)
                return saver
            opened = AsyncMock(side_effect=AssertionError("Unsafe initial candidate reached browser startup"))
            with patch.object(RunRuntime, "open", opened), patch("e2e_agent.coordinator.MemorySaver", side_effect=saver_factory):
                try:
                    await execute_suite(sensitive_suite(secret), directory, settings.browsers_path)
                    raise AssertionError("Sensitive static literal was accepted")
                except RuntimeError as error:
                    assert secret not in str(error), "Rejected static candidate was echoed in its exception"
            assert opened.await_count == 0 and not savers, "Static candidate crossed the initial native checkpoint boundary"
            partial = json.loads((directory / "partial-suite.json").read_text(encoding="utf-8"))
            assert partial["status"] == "error"
            evidence = {"browser_open_calls": 0, "saver_instances": 0, "rejected_before_checkpoint": True}
        else:
            secret = "DynamicPrivate_" + uuid.uuid4().hex
            secrets.append(secret)
            async def plan(scene, checkpoint, observation, *, remaining, redact):
                return [Action(id="unsafe-dynamic", checkpoint_id=checkpoint.id, kind="fill", target="label:Password",
                               value=secret, field_role="password", intent="Fill password")]
            async with NativeRun(settings, directory, secrets, suite=sensitive_suite(secret, empty=True), planner=plan) as run:
                final = await run.invoke(first=True)
                assert not final.next and final.values["execution"]["status"] == "error"
                assert final.values["action_count"] == 0 and not final.values["action_queue"]
                assert not final.values["effective_actions"]["main-chain"]
                evidence = {"run_id": run.runtime.run_id, "dispatches": 0, "rejected_before_action_checkpoint": True,
                            "saver_privacy": private_scan_saver(run.saver, secrets)}
        checked = scan(directory, secrets)
        report["scenarios"].append({"id": identity, "expected": "passed", "actual": "passed", "status": "passed",
                                    "native_graph_verified": True, "privacy_verified": True,
                                    "scanned_artifacts": checked, "evidence": evidence})
        (output / "graph-resource-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report.pop("current_case", None)
    report.update(status="passed", native_human_resume_verified=True, plaintext_resource_leaks=0)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    report, secrets = initial_report(), []
    try:
        asyncio.run(verify(args.output, report, secrets))
    except Exception as error:
        redactor = Redactor()
        for value in secrets:
            redactor.register(value)
        report.update(status="failed", error=redactor.text(f"{type(error).__name__}: {error}"))
    path = args.output / "graph-resource-validation.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
