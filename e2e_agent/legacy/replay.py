"""Replay legacy single tests or match an exported suite's exact scenario identities."""

import asyncio
import json
from pathlib import Path
import sys
import uuid
import time
from contextlib import asynccontextmanager
import xml.etree.ElementTree as ET

from .browser import execution_environment, stop_process
from .config import Settings
from .models import ReplayResult
from .domain import Suite
from .resources import ConsoleProvider
from .resource_bridge import ResourceBroker
from .evidence import archive_replay


def read_result(xml_path: Path, exit_code: int, *, expected_ids: dict[str, str] | None = None,
                expected_run_id: str | None = None) -> ReplayResult:
    if not xml_path.is_file():
        return ReplayResult(status="error", exit_code=exit_code, detail="Pytest 未生成 JUnit 结果，请查看 pytest.log。")
    try:
        cases = ET.parse(xml_path).getroot().findall(".//testcase")
        failures = sum(case.find("failure") is not None for case in cases)
        errors = sum(case.find("error") is not None for case in cases)
        skipped = sum(case.find("skipped") is not None for case in cases)
        properties_by_case = [{p.get("name"): p.get("value") for p in case.findall("./properties/property")} for case in cases]
        blocked = sum(p.get("execution_status") == "blocked" for p in properties_by_case)
        cancelled = sum(p.get("execution_status") == "cancelled" for p in properties_by_case)
        if expected_ids is None:
            identities_valid = len(cases) == 1
        else:
            names = [case.get("name") for case in cases]
            identities_valid = bool(expected_ids) and len(names) == len(expected_ids) and set(names) == set(expected_ids)
            for case in cases:
                properties = {p.get("name"): p.get("value") for p in case.findall("./properties/property")}
                identities_valid &= properties.get("scenario_id") == expected_ids.get(case.get("name"))
                if expected_run_id is not None:
                    identities_valid &= properties.get("e2e_run_id") == expected_run_id
        if not identities_valid:
            return ReplayResult(status="error", exit_code=exit_code, tests=len(cases), failures=failures,
                                errors=errors, skipped=skipped, detail="JUnit 场景身份、集合、数量或本轮标识不匹配。")
        passed = exit_code == 0 and not (failures or errors or skipped or blocked or cancelled)
        status = "passed" if passed else ("failed" if failures and not errors and not skipped and exit_code == 1 else "error")
        if expected_ids is not None and not errors and not skipped and exit_code == 1 and failures == blocked + cancelled:
            status = "cancelled" if cancelled else ("blocked" if blocked else status)
        return ReplayResult(status=status, exit_code=exit_code, tests=len(cases), failures=failures,
                            errors=errors, skipped=skipped, blocked=blocked,
                            detail="" if passed else "测试未通过；未收集到用例或跳过用例均不计为通过。")
    except ET.ParseError:
        return ReplayResult(status="error", exit_code=exit_code, detail="JUnit 文件无效，请查看 pytest.log。")


@asynccontextmanager
async def resource_broker(provider, run_id, suite, max_wait):
    if suite is None:
        yield None
    else:
        async with ResourceBroker(provider, run_id, suite.resources, max_wait=max_wait) as broker:
            yield broker


async def run_replay(settings: Settings, run_dir: Path, *, resource_provider=None) -> ReplayResult:
    run_dir = run_dir.resolve()
    script = run_dir / "test_generated.py"
    if not script.is_file():
        return ReplayResult(status="error", detail="没有完整的 test_generated.py，无法重跑。")
    archive_replay(run_dir)
    xml_path = run_dir / "junit.xml"
    xml_path.unlink(missing_ok=True)  # Never trust a successful result from a prior run.
    manifest_path = run_dir / "suite-manifest.json"
    expected_ids, run_id, suite, max_wait = None, None, None, 120
    environment = execution_environment(settings)
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_ids = manifest["tests"]
            if manifest["schema_version"] != 2 or not manifest["complete_script"] or not expected_ids or not isinstance(expected_ids, dict):
                raise ValueError("Incomplete suite manifest")
            if len(set(expected_ids.values())) != len(expected_ids):
                raise ValueError("Duplicate scenario identity")
            suite = Suite.model_validate_json((run_dir / "suite.json").read_text(encoding="utf-8"))
            max_wait = float(manifest.get("resource_wait_timeout", 120))
            if not 0 < max_wait <= 3600:
                raise ValueError("Invalid resource wait deadline")
        except (ValueError, KeyError, TypeError, OSError):
            return ReplayResult(status="error", detail="套件导出清单无效或不完整。")
        run_id = uuid.uuid4().hex
        environment["E2E_REPLAY_RUN_ID"] = run_id
    provider = resource_provider or ConsoleProvider(allow_test_delivery=True)
    try:
        async with resource_broker(provider, run_id, suite, max_wait) as broker:
            if broker:
                environment.update(broker.environment)
            with (run_dir / "pytest.log").open("w", encoding="utf-8") as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pytest", str(script), "-q", "-p", "no:cacheprovider",
                    f"--junitxml={xml_path}", cwd=run_dir, env=environment,
                    stdout=log, stderr=asyncio.subprocess.STDOUT,
                )
                waiting, active, previous = 0.0, 0.0, time.monotonic()
                wait_task = asyncio.create_task(process.wait())
                try:
                    while not wait_task.done():
                        await asyncio.wait({wait_task}, timeout=0.05)
                        current = time.monotonic()
                        if broker and broker.waiting:
                            waiting += current - previous
                        else:
                            active += current - previous
                        previous = current
                        if active > settings.replay_timeout:
                            raise asyncio.TimeoutError
                    exit_code = wait_task.result()
                except asyncio.TimeoutError:
                    await stop_process(process)
                    await wait_task
                    return ReplayResult(status="error", detail="独立测试主动执行超时，已停止测试进程。")
                except BaseException:
                    await stop_process(process)
                    await wait_task
                    raise
                finally:
                    timing = {"active_seconds": active, "human_wait_seconds": waiting,
                              "worker_timeout": settings.worker_timeout, "replay_timeout": settings.replay_timeout}
                    (run_dir / "replay-timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
                    if broker:
                        (run_dir / "replay-resource-events.json").write_text(json.dumps(broker.redactor.data(broker.events), indent=2), encoding="utf-8")
    finally:
        if resource_provider is None:
            provider.closed = True
    return read_result(xml_path, exit_code, expected_ids=expected_ids, expected_run_id=run_id)
