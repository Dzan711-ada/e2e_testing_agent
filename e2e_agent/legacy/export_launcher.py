"""Standalone parent-console launcher shipped with exported suites; no model dependency."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from e2e_support.domain import Suite
from e2e_support.resource_bridge import ResourceBroker
from e2e_support.resources import ConsoleProvider, ScriptedProvider

ROOT = Path(__file__).resolve().parent


async def stop(process):
    if process.returncode is not None:
        return
    if os.name == "nt":
        await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        process.kill()
    await process.wait()


async def run(scripted):
    from e2e_support.evidence import archive_replay
    archive_replay(ROOT, standalone=True)
    suite = Suite.model_validate_json((ROOT / "suite.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "suite-manifest.json").read_text(encoding="utf-8"))
    if scripted and suite.fixture != "local_shop":
        raise ValueError("Scripted test delivery is available only for the simulated local shop")
    provider = ScriptedProvider({"phone": os.environ.get("E2E_RESOURCE_PHONE", "15500000001")}, use_test_delivery=True) if scripted else ConsoleProvider(allow_test_delivery=True)
    run_id = uuid.uuid4().hex
    environment = {key: value for key, value in os.environ.items() if not key.endswith(("API_KEY", "API_TOKEN")) and key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS"}}
    environment.update(E2E_REPLAY_RUN_ID=run_id, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONIOENCODING="utf-8")
    xml_path = ROOT / "standalone-junit.xml"
    xml_path.unlink(missing_ok=True)
    async with ResourceBroker(provider, run_id, suite.resources, max_wait=manifest.get("resource_wait_timeout", 120)) as broker:
        environment.update(broker.environment)
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "pytest", str(ROOT / "test_generated.py"), "-q",
                    "-p", "no:cacheprovider", f"--junitxml={xml_path}", cwd=ROOT, env=environment, stdin=asyncio.subprocess.DEVNULL)
        previous, active = time.monotonic(), 0.0
        wait = asyncio.create_task(process.wait())
        try:
            while not wait.done():
                await asyncio.wait({wait}, timeout=0.05)
                current = time.monotonic()
                if not broker.waiting:
                    active += current - previous
                previous = current
                if active > 120:
                    raise TimeoutError("Active replay execution exceeded 120 seconds")
            exit_code = wait.result()
        finally:
            await stop(process)
            await wait
            (ROOT / "standalone-resource-events.json").write_text(json.dumps(broker.redactor.data(broker.events), indent=2), encoding="utf-8")
    if exit_code != 0 or not xml_path.exists():
        return exit_code or 1
    cases = ET.parse(xml_path).getroot().findall(".//testcase")
    names = [case.get("name") for case in cases]
    expected = manifest["tests"]
    if not expected or len(names) != len(expected) or set(names) != set(expected):
        return 1
    for case in cases:
        props = {p.get("name"): p.get("value") for p in case.findall("./properties/property")}
        if props.get("scenario_id") != expected[case.get("name")] or props.get("e2e_run_id") != run_id:
            return 1
        if props.get("execution_status") != "passed" or any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripted-fixture", action="store_true", help="Use fictional resources and the local fake-SMS test channel")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.scripted_fixture)))
