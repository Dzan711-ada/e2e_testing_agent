"""Stage-4 automated browser/IPC/privacy acceptance; actual console input is separate."""
import argparse
import asyncio
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import quote, quote_plus

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.browser import execution_environment
from e2e_agent.config import Settings
from e2e_agent.coordinator import execute_suite
from e2e_agent.domain import Suite
from e2e_agent.replay import run_replay
from e2e_agent.resources import ResourceCancelled, ScriptedProvider
from e2e_agent.resume import restart_suite
from e2e_agent.sample_shop import shop_server
from e2e_agent.sms_demo import sms_suite
from e2e_agent.suite_codegen import export_suite
from e2e_agent.suite_demo import shop_suite
from scripts.verify_workflow_compat import descendants, process_tree

PHONE = "15500001111"  # Fictional sentinel; never serialized into generated artifacts.


class CapturingProvider(ScriptedProvider):
    def __init__(self, secrets, values=None, **kwargs):
        super().__init__({"phone": PHONE, **(values or {})}, **kwargs)
        self.secrets = secrets

    async def acquire(self, request):
        if request.delivery_hint:
            self.secrets.append(request.delivery_hint)
        value = await super().acquire(request)
        self.secrets.append(value)
        return value


def chain(result):
    return result["scenarios"]["sms-chain"]


def assert_sms(result):
    main = chain(result)
    assert result["status"] == "passed" and main["complete"]
    assert main["stats"] == {"login_count": 1, "send_count": 1}
    assert main["contexts_created"] == 1
    assert {c["context_id"] for c in main["checkpoints"]} == {main["context_id"]}
    assert sum(e["action_id"] == "send-code" for e in main["events"]) == 1
    assert sum(e["action_id"] == "verify-code" for e in main["events"]) == 1


def scan(directory, values):
    checked = 0
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix.lower() == ".png":
            continue
        if path.suffix.lower() == ".html":
            from e2e_agent.suite_reporting import is_generated_report
            assert is_generated_report(path), "Sensitive raw evidence found: " + str(path)
        else:
            assert path.suffix.lower() not in {".zip", ".har"}, "Sensitive raw evidence found: " + str(path)
        content = path.read_bytes()
        for value in set(values):
            if value:
                for variant in {value, quote(value, safe=""), quote_plus(value)}:
                    assert variant.encode() not in content, "Resource sentinel leaked in " + str(path)
        checked += 1
    return checked


async def verify(output, *, long_wait):
    settings, suite = Settings.load(), sms_suite()
    secrets = [PHONE, settings.deepseek_api_key, settings.qwen_api_key]
    report = {"stage": 4, "actual_console_input": False, "model_calls": 0, "scenarios": []}
    with shop_server() as fixture:
        provider = CapturingProvider(secrets, use_test_delivery=True,
            delay=lambda r: settings.worker_timeout + 1 if long_wait and r.kind == "phone" else (.2 if r.kind == "sms_code" else 0))
        normal = await execute_suite(suite, output / "first", settings.browsers_path, fixture=fixture, resource_provider=provider)
        assert_sms(normal)
        if long_wait:
            assert normal["timing"]["human_wait_seconds"] > settings.worker_timeout
        report["scenarios"].append({"id": "sms-first", "expected": "passed", "actual": normal["status"], "timing": normal["timing"], "worker_timeout": settings.worker_timeout})
        again = await execute_suite(suite, output / "second", settings.browsers_path, fixture=fixture,
                                    resource_provider=CapturingProvider(secrets, use_test_delivery=True))
        assert_sms(again)
        assert chain(normal)["namespace"] != chain(again)["namespace"]
        report["scenarios"].append({"id": "sms-fresh-round", "expected": "passed", "actual": again["status"]})

        variants = [("missing-phone", ScriptedProvider({}), "blocked"),
                    ("cancelled-phone", ScriptedProvider({"phone": ResourceCancelled("Cancelled")}), "cancelled"),
                    ("missing-code", CapturingProvider(secrets), "blocked"),
                    ("wrong-code", CapturingProvider(secrets, {"sms_code": lambda r: "314159" if r.delivery_hint != "314159" else "271828"}), "failed")]
        for identity, source, expected in variants:
            result = await execute_suite(suite, output / identity, settings.browsers_path, fixture=fixture, resource_provider=source, resource_wait_timeout=2)
            assert result["status"] == expected, result
            main = chain(result)
            assert main["stats"]["login_count"] == 0 and main["stats"]["send_count"] <= 1
            assert all(c["status"] == "blocked" for c in main["checkpoints"] if c["id"] in {"cart", "refresh"})
            report["scenarios"].append({"id": identity, "expected": expected, "actual": result["status"]})

        async def expire(scene_id, checkpoint, action, observation, **kwargs):
            if action.id == "request-code":
                with fixture[1].connection() as db:
                    db.execute("UPDATE challenges SET expires=? WHERE used=0", (time.time() - 1,))
            return action

        expired = await execute_suite(suite, output / "expired-code", settings.browsers_path, fixture=fixture,
                                      resource_provider=CapturingProvider(secrets, use_test_delivery=True), strategy=expire)
        assert expired["status"] == "blocked" and chain(expired)["stats"]["send_count"] == 1
        assert not any(e["action_id"] == "fill-code" for e in chain(expired)["events"])
        report["scenarios"].append({"id": "expired-code", "expected": "blocked", "actual": expired["status"]})

        interrupted = output / "interrupted"
        interrupted.mkdir()
        (interrupted / "plan.json").write_text(suite.model_dump_json(indent=2), encoding="utf-8")
        pending = asyncio.create_task(execute_suite(suite, interrupted / "generation", settings.browsers_path, fixture=fixture,
            resource_provider=CapturingProvider(secrets, use_test_delivery=True, delay=lambda r: 30 if r.kind == "sms_code" else 0)))
        deadline, old_request = time.monotonic() + 20, None
        while time.monotonic() < deadline:
            path = interrupted / "generation/resource-events.json"
            try:
                events = json.loads(path.read_text(encoding="utf-8"))
                old_request = next((e for e in events if e["kind"] == "sms_code" and e["status"] == "waiting_user"), None)
            except (OSError, ValueError):
                pass
            if old_request:
                break
            if pending.done():
                await pending
                raise AssertionError("Pause was not reached")
            await asyncio.sleep(.05)
        assert old_request, "No observable resource pause"
        owned = descendants(await asyncio.to_thread(process_tree), os.getpid()) - {os.getpid()}
        assert len(owned) > 1, "Browser process inspection must not be empty"
        pending.cancel()
        try:
            await pending
            raise AssertionError("Cancellation was swallowed")
        except asyncio.CancelledError:
            pass
        assert not (owned & set(await asyncio.to_thread(process_tree))), "Owned browser process leaked"
        restarted = await restart_suite(settings, interrupted, run_dir=output / "restarted", resource_provider=CapturingProvider(secrets, use_test_delivery=True))
        assert restarted["status"] == "passed" and not restarted["browser_state_restored"] and not restarted["old_resources_restored"]
        new_requests = json.loads((output / "restarted/generation/resource-events.json").read_text(encoding="utf-8"))
        new_code_request = next(e for e in new_requests if e["kind"] == "sms_code")
        assert new_code_request["run_id"] != old_request["run_id"] and new_code_request["challenge_id"] != old_request["challenge_id"]
        assert_sms(restarted["execution"])
        report["scenarios"].append({"id": "cancel-and-safe-restart", "expected": "passed", "actual": restarted["status"], "old_owned_processes_remaining": 0})

    exported = output / "exported"
    export_suite(suite, exported, settings.browsers_path, resource_wait_timeout=20)
    replay_source = CapturingProvider(secrets, use_test_delivery=True, delay=lambda r: 11 if r.kind == "phone" else .2)
    replay = await run_replay(replace(settings, replay_timeout=10), exported, resource_provider=replay_source)
    assert replay.status == "passed" and replay.tests == 1, replay
    assert_sms(json.loads((exported / "replay-suite/suite-result.json").read_text(encoding="utf-8")))
    timing = json.loads((exported / "replay-timing.json").read_text(encoding="utf-8"))
    assert timing["human_wait_seconds"] > timing["replay_timeout"]
    report["scenarios"].append({"id": "ipc-replay", "expected": "passed", "actual": replay.status, "timing": timing})
    blocked_export = output / "blocked-export"
    export_suite(suite, blocked_export, settings.browsers_path, resource_wait_timeout=2)
    blocked = await run_replay(settings, blocked_export, resource_provider=ScriptedProvider({}))
    assert blocked.status == "blocked" and blocked.blocked == 1
    report["scenarios"].append({"id": "replay-without-resources", "expected": "blocked", "actual": blocked.status})

    private = shop_suite().model_dump()
    private["scenarios"][0]["checkpoints"][1]["acceptance"][0].update(operator="visible", expected=True)
    for ref in private["resources"]:
        if ref["id"] in {"username", "password"}:
            ref["reusable"] = True
    private_suite = Suite.model_validate(private)
    username, password = "SentinelAccount_8de50", "Sentinel-P@ss-3f67!"
    secrets += [username, password]
    private_export = output / "private-export"
    export_suite(private_suite, private_export, settings.browsers_path, external_resources=("username", "password"))
    result = await run_replay(settings, private_export, resource_provider=ScriptedProvider({"username": username, "password": password}))
    assert result.status == "passed" and result.tests == 3, result
    report["scenarios"].append({"id": "private-account-reference-replay", "expected": "passed", "actual": result.status})
    standalone_export = output / "standalone-export"
    export_suite(suite, standalone_export, settings.browsers_path)
    standalone = await asyncio.create_subprocess_exec(sys.executable, str(standalone_export / "run_with_resources.py"), "--scripted-fixture",
        cwd=standalone_export, env=execution_environment(settings), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    raw, _ = await asyncio.wait_for(standalone.communicate(), 60)
    (output / "standalone.log").write_bytes(raw)
    assert standalone.returncode == 0, "Standalone resource launcher failed"
    report["scenarios"].append({"id": "standalone-export", "expected": "passed", "actual": "passed"})
    report["privacy"] = {"scanned_text_artifacts": scan(output, secrets), "plaintext_resource_leaks": 0, "raw_dom_trace_files": 0}
    report["status"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--quick", action="store_true", help="Diagnostic iteration only; omits the default-worker-timeout long wait")
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        report = asyncio.run(verify(args.output, long_wait=not args.quick))
        report["long_wait_verified"] = not args.quick
    except Exception as error:
        report = {"stage": 4, "status": "failed", "error": Settings.load().redact(f"{type(error).__name__}: {error}")}
    (args.output / "resource-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
