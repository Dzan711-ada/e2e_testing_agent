"""Export a model-free pytest project; each scene keeps its own checkpoint record."""
import json
from pathlib import Path
from importlib.util import find_spec

from .domain import Suite
from .actions import compile_action


def test_id(scenario_id):
    return "test_scenario_" + scenario_id.encode("utf-8").hex()


def export_suite(suite: Suite, directory: Path, browsers_path: Path, *, headless=True, resource_wait_timeout=120, public_fictional=False, external_resources=()):
    for scenario in suite.scenarios:
        if not scenario.actions:
            raise ValueError("Cannot label an unfinished scenario as a complete script")
        for action in scenario.actions:
            compile_action(action, "http://127.0.0.1/validated-template")
    directory.mkdir(parents=True, exist_ok=True)
    package = directory / "e2e_support"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for filename in ("domain.py", "actions.py", "action_policy.py", "coordinator.py", "execution_graph.py", "graph_state.py", "graph_checkpoint.py", "execution_runtime.py", "sample_shop.py", "resources.py", "resource_bridge.py", "evidence.py", "site_profiles.py"):
        source = Path(__file__).with_name(filename)
        if not source.is_file():
            source = Path(find_spec(__package__ + "." + filename[:-3]).origin)
        (package / filename).write_bytes(source.read_bytes())
    (directory / "suite.json").write_text(suite.model_dump_json(indent=2), encoding="utf-8")
    identities = {test_id(s.id): s.id for s in suite.scenarios}
    manifest = {"schema_version": 2, "complete_script": True, "suite_id": suite.id, "tests": identities,
                "requirements_fingerprint": suite.requirements_fingerprint(), "browsers_path": str(browsers_path), "headless": headless,
                "resource_wait_timeout": resource_wait_timeout, "public_fictional": public_fictional,
                "external_resources": list(external_resources)}
    (directory / "suite-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    source = '''# Generated structured suite. Run: python -m pytest test_generated.py -q
import asyncio
import json
import os
from pathlib import Path
from importlib.util import find_spec
import pytest
from e2e_support.domain import Suite
from e2e_support.coordinator import execute_suite

ROOT = Path(__file__).resolve().parent

@pytest.fixture(scope="session")
def suite_result():
    suite = Suite.model_validate_json((ROOT / "suite.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "suite-manifest.json").read_text(encoding="utf-8"))
    assert suite.requirements_fingerprint() == manifest["requirements_fingerprint"]
    resources = {r.id: os.environ["E2E_RESOURCE_" + r.id.upper()] for r in suite.resources if "E2E_RESOURCE_" + r.id.upper() in os.environ}
    return asyncio.run(execute_suite(suite, ROOT / "replay-suite", Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", manifest["browsers_path"])), resources=resources, headless=manifest["headless"], resource_wait_timeout=manifest.get("resource_wait_timeout", 120), public_fictional=manifest.get("public_fictional", False), external_resources=manifest.get("external_resources", [])))

'''
    for name, identity in identities.items():
        source += f'''def {name}(suite_result, record_property):
    record_property("scenario_id", {identity!r})
    record_property("e2e_run_id", os.environ.get("E2E_REPLAY_RUN_ID", "manual"))
    result = suite_result["scenarios"][{identity!r}]
    record_property("execution_status", result["status"])
    assert result["status"] == "passed", json.dumps(result, ensure_ascii=False)

'''
    compile(source, "test_generated.py", "exec")
    (directory / "test_generated.py").write_text(source, encoding="utf-8")
    (directory / "run_with_resources.py").write_bytes(Path(__file__).with_name("export_launcher.py").read_bytes())
    (directory / "requirements.txt").write_text("playwright>=1.50,<2\npydantic>=2,<3\npytest>=8,<10\nFlask>=3.1,<4\nlanggraph==0.2.18\nlanggraph-checkpoint==1.0.9\nlangchain-core==0.3.86\n", encoding="utf-8")
    (directory / "README.md").write_text("# Exported test suite\n\nRun `python -m pytest test_generated.py -q` for tests with available resources, or `python run_with_resources.py` for parent-console resource requests. No model, API key or original project source is required. The fixed-plan LangGraph execution graph and its checkpoint/runtime modules are included with locked compatible framework requirements. Install requirements.txt and Chromium first; PLAYWRIGHT_BROWSERS_PATH can override the saved installation path.\n\nEach pytest case represents one complete scenario; checkpoint results and blocked dependencies are in replay-suite/suite-result.json. A failed scenario remains failed. The local shop creates fresh isolated data each execution. Human waiting has its own deadline and does not consume active execution time. Direct pytest without a resource bridge reports blocked for missing interactive resources.\n\nScreenshots mask known resources and input fields; no raw DOM, authentication state or full trace is exported. Process restart requires a fresh scene and fresh OTP.\n", encoding="utf-8")
    return manifest
