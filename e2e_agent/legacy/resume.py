"""Logical recovery restarts a complete scene; browser handles and OTPs are never restored."""
from datetime import datetime
import json
from pathlib import Path
import uuid

from .coordinator import execute_suite
from .domain import Suite
from .resources import ConsoleProvider
from .suite_codegen import export_suite
from .site_profiles import profile_for_suite


def read_saved_plan(directory):
    directory = Path(directory)
    # New graph checkpoints contain display projections. Only a canonical
    # plan with its frozen fingerprint can supply executable requirements.
    for name in ("plan.json", "partial-plan.json"):
        path = directory / name
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("artifact_kind") == "canonical_plan":
                suite = Suite.model_validate(data["suite"])
                if data.get("requirements_fingerprint") != suite.requirements_fingerprint():
                    raise ValueError("Saved canonical plan changed protected requirements")
                return suite
    for name in ("suite.json", "plan.json", "partial-plan.json", "agent-state.json"):
        path = directory / name
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("checkpoint_suite") == "display_only" or data.get("graph_protocol_version", 0) >= 4:
                raise ValueError("A display checkpoint cannot restore executable requirements; a canonical plan is required")
            return Suite.model_validate(data.get("suite", data))
    raise ValueError("No saved versioned plan is available for a safe restart")


async def restart_suite(settings, previous, *, run_dir=None, resource_provider=None, transport=None):
    previous = Path(previous).resolve()
    suite = read_saved_plan(previous)
    directory = (run_dir or settings.runtime_path / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-restart-" + uuid.uuid4().hex[:8])).resolve()
    metadata = {}
    for name in ("agent-result.json", "agent-state.json", "partial-plan.json"):
        path = previous / name
        if path.is_file():
            metadata = json.loads(path.read_text(encoding="utf-8"))
            break
    canonical = previous / "partial-plan.json"
    if canonical.is_file():
        saved = json.loads(canonical.read_text(encoding="utf-8"))
        if saved.get("artifact_kind") == "canonical_plan":
            # Keep the previous result status, but restore the confirmed
            # answers/sources from the canonical artifact, never its display.
            metadata.update({key: saved[key] for key in ("agent_protocol_version", "answers", "confirmed_sources") if key in saved})
    missing = [(s.id, cp.id) for s in suite.scenarios for cp in s.checkpoints
               if not any(a.checkpoint_id == cp.id for a in s.actions)]
    if missing:
        if metadata.get("agent_protocol_version") != 2:
            raise ValueError("Incomplete legacy plan cannot be exported as a complete runnable suite")
        from .agent_workflow import run_agent_suite
        result = await run_agent_suite(settings, query=suite.query, run_dir=directory,
            frozen_suite=suite, confirmed_answers=metadata.get("answers"), confirmed_sources=metadata.get("confirmed_sources"),
            resource_provider=resource_provider, transport=transport,
            site_profile=profile_for_suite(suite) or "local_shop")
        result.update(mode="safe_restart", previous_run=str(previous), browser_state_restored=False,
                      old_resources_restored=False, missing_batches_at_restart=missing,
                      stability="unverified" if metadata.get("status") is None else
                          ("consistent" if metadata["status"] == result["status"] else "inconsistent"))
        (directory / "restart-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "plan.json").write_text(suite.model_dump_json(indent=2), encoding="utf-8")
    result = {"mode": "safe_restart", "previous_run": str(previous), "status": "running", "complete_script": False,
              "browser_state_restored": False, "old_resources_restored": False}
    provider = resource_provider
    if provider is None and profile_for_suite(suite) == "practice-login":
        from .practice_resources import public_demo_resources
        from .resources import ScriptedProvider
        provider = ScriptedProvider(public_demo_resources("practice-login"))
    provider = provider or ConsoleProvider(allow_test_delivery=True)
    try:
        execution = await execute_suite(suite, directory / "generation", settings.browsers_path, resource_provider=provider,
                                        headless=settings.headless, public_fictional=suite.fixture == "local_shop")
        result.update(status=execution["status"], execution=execution)
        if execution["status"] == "passed":
            export_suite(suite, directory, settings.browsers_path, headless=settings.headless, public_fictional=suite.fixture == "local_shop")
            result["complete_script"] = True
        result["stability"] = "unverified"
        for name in ("agent-result.json", "restart-result.json", "generation/partial-suite.json"):
            if (previous / name).exists():
                old = json.loads((previous / name).read_text(encoding="utf-8"))
                result["stability"] = "consistent" if old["status"] == result["status"] else "inconsistent"
                break
    except Exception as error:
        result.update(status="error", error=settings.redact(f"{type(error).__name__}: {error}"))
    except BaseException:
        result["status"] = "cancelled"
        raise
    finally:
        if resource_provider is None:
            provider.closed = True
        (directory / "restart-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["run_dir"] = str(directory)
    return result
