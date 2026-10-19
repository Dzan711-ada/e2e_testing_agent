"""Browser regression for checkpoint planning, frozen restart and runtime account references."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.config import Settings
from e2e_agent.coordinator import execute_suite
from e2e_agent.domain import Suite
from e2e_agent.replay import run_replay
from e2e_agent.resources import ScriptedProvider
from e2e_agent.resume import restart_suite
from e2e_agent.sample_shop import CONTRACT
from e2e_agent.suite_codegen import export_suite
from e2e_agent.suite_demo import shop_suite
from scripts.verify_agents import FixedRoles, INPUTS
from scripts.verify_resources import scan


async def verify(output):
    settings = Settings.load()
    data = shop_suite().model_dump()
    data.update(query=INPUTS["required-clarification"], version=2)
    data["budget"]["max_actions"] = settings.max_actions
    for rule in data["rules"]:
        rule["source"]["reference"] = "contract:shop-v1"
        if rule["id"] == "cart":
            rule["source"] = {"kind": "clarification", "reference": "answer:target_quantity"}
    for scene in data["scenarios"]:
        scene["actions"] = [a for a in scene["actions"] if scene["id"] == "main-chain" and a["checkpoint_id"] == "registration"]
        for action in scene["actions"]:
            if action["kind"] == "fill":
                action["field_role"] = "username" if action["resource_id"] == "username" else "password"
    frozen = Suite.model_validate(data)
    previous = output / "interrupted"
    previous.mkdir()
    (previous / "partial-plan.json").write_text(json.dumps({"suite": data, "complete_script": False}), encoding="utf-8")
    (previous / "agent-result.json").write_text(json.dumps({"status": "cancelled", "agent_protocol_version": 2,
        "answers": {"target_quantity": "1"}, "confirmed_sources": {"contract:shop-v1": {"kind": "contract", "text": CONTRACT},
            "answer:target_quantity": {"kind": "clarification", "text": "1"}}}), encoding="utf-8")
    model = FixedRoles(settings.max_actions)
    result = await restart_suite(settings, previous, run_dir=output / "restarted", transport=model)
    assert result["status"] == "passed" and result["complete_script"] and result["replay"]["status"] == "passed", result.get("error")
    assert not result["browser_state_restored"] and not result["old_resources_restored"]
    assert result["version"] == 2 and result["answers"] == {"target_quantity": "1"}
    assert result["execution"]["source_catalog"]["answer:target_quantity"] == {"kind": "clarification", "text": "1"}
    assert Suite.model_validate(result["suite"]).requirements_fingerprint() == frozen.requirements_fingerprint()
    assert len(model.calls) == 5 and all(c["role"] == "execute" for c in model.calls)
    assert result["execution"]["action_count"] == 15
    assert result["execution"]["scenarios"]["main-chain"]["stats"] == {"login_count": 1, "send_count": 0}

    private = shop_suite().model_dump()
    for ref in private["resources"]:
        ref["acquisition"] = "runtime"
    private["scenarios"][0]["checkpoints"][1]["acceptance"][0].update(operator="visible", expected=True)
    secrets = {"username": "BatchAccountSentinelQ87", "password": "BatchPasswordSentinelQ87!", "wrong_password": "BatchWrongSentinelQ87!"}
    one_time = await execute_suite(Suite.model_validate(private), output / "private-nonreusable", settings.browsers_path,
                                   resource_provider=ScriptedProvider(secrets))
    assert one_time["status"] == "blocked", "Single-use account credentials must not be silently reused"
    assert "already consumed" in one_time["scenarios"]["main-chain"]["checkpoints"][1]["error"]
    for ref in private["resources"]:
        ref["reusable"] = True
    suite = Suite.model_validate(private)
    provider = ScriptedProvider(secrets)
    first = await execute_suite(suite, output / "private-first", settings.browsers_path, resource_provider=provider)
    assert first["status"] == "passed", "Reusable runtime credentials did not complete the original checks"
    export_suite(suite, output / "private-export", settings.browsers_path)
    replay = await run_replay(settings, output / "private-export", resource_provider=ScriptedProvider(secrets))
    assert replay.status == "passed"
    checked = scan(output, [*secrets.values(), settings.deepseek_api_key, settings.qwen_api_key])
    return {"status": "passed", "agent_protocol_version": 2, "real_model": False,
            "cases": ["frozen-partial-restart", "nonreusable-account-blocked", "runtime-account-first-and-replay"],
            "preserved_checks": sum(len(c.acceptance) for s in frozen.scenarios for c in s.checkpoints),
            "checked_artifacts": checked, "plaintext_resource_leaks": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        report = asyncio.run(verify(args.output))
    except Exception as error:
        report = {"status": "failed", "error": Settings.load().redact(f"{type(error).__name__}: {error}")}
    (args.output / "batch-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
