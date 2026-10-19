"""Real-model gate (default) or fixed-role browser regression (--offline)."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.agent_workflow import ScriptedAnswers, run_agent_suite
from e2e_agent.agents import DesignDecision, TargetChoice
from e2e_agent.config import Settings
from e2e_agent.domain import Suite
from e2e_agent.suite_demo import shop_suite

INPUTS = {
    "clear-goal": "测试注册、登录和购物车：注册新账号后登录，添加一本 Agent Handbook，检查数量为 1 且刷新后保留；也验证错误密码被拒绝和未登录不能访问购物车。",
    "required-clarification": "测试注册、登录和购物车：注册新账号后登录，添加一本 Agent Handbook；购物车目标数量由我指定，目前尚未提供，请先询问这个目标数量，再按答案验证并刷新检查；也验证错误密码被拒绝和未登录不能访问购物车。",
}


class FixedRoles:
    def __init__(self, max_actions):
        self.calls, self.limit, self.max_actions = [], 20, max_actions

    async def call(self, role, schema, system, payload):
        if len(self.calls) >= self.limit:
            raise ValueError("Model budget exhausted")
        self.calls.append({"role": role, "real": False, "status": "passed", "input_scope": "fixed test data"})
        if role == "execute":
            if "checkpoint" in payload:
                data = shop_suite().model_dump()
                scene = next(item for item in data["scenarios"] if item["id"] == payload["scenario_id"])
                actions = [a for a in scene["actions"] if a["checkpoint_id"] == payload["checkpoint"]["id"]]
                for action in actions:
                    if action["kind"] == "fill":
                        action["field_role"] = "username" if action["resource_id"] == "username" else "password"
                return schema.model_validate({"actions": actions})
            action = payload["action"]
            return TargetChoice(action_id=action["id"], checkpoint_id=action["checkpoint_id"], target=action["target"], reason="fixed regression")
        if "尚未提供" in payload["query"] and not payload["answers"]:
            return DesignDecision(status="needs_clarification", questions=[{"id": "target_quantity", "text": "购物车目标数量是多少？", "reason": "用户指定目标但尚未给出"}])
        data = shop_suite().model_dump()
        data.update(query=payload["query"])
        data["budget"]["max_actions"] = self.max_actions
        for rule in data["rules"]:
            rule["source"]["reference"] = "contract:shop-v1"
            if rule["id"] == "cart" and payload["answers"]:
                rule["source"] = {"kind": "clarification", "reference": "answer:target_quantity"}
        for scene in data["scenarios"]:
            for action in scene["actions"]:
                if action["kind"] == "fill":
                    action["field_role"] = "username" if action["resource_id"] == "username" else "password"
            scene["actions"] = []
        return DesignDecision(status="ready", suite=Suite.model_validate(data))


async def verify(output, offline, case=None):
    settings = Settings.load()
    if not offline:
        settings.require_model()
    report = {"stage": 3, "real_model": not offline, "input_version": 5, "agent_protocol_version": 2, "full_stage_specialty": case is None, "input_scope": "local controlled shop, explicit contract, confirmed execution defaults and fictional resource references only",
              "inputs": INPUTS, "scenarios": []}
    for identity, query in INPUTS.items():
        if case is not None and identity != case:
            continue
        provider = ScriptedAnswers(["1"] if identity == "required-clarification" else [])
        transport = FixedRoles(settings.max_actions) if offline else None
        result = await run_agent_suite(settings, query=query, run_dir=output / identity, answers_provider=provider, transport=transport)
        entry = {"id": identity, "expected": "passed", "actual": result["status"], "complete_script": result["complete_script"],
                 "questions": provider.requests, "model_calls": result["model_calls"], "result_path": str(output / identity / "agent-result.json")}
        report["scenarios"].append(entry)
        if result["status"] != "passed":
            entry["error"] = result.get("error", "Inspect checkpoint evidence")
            report["status"] = "failed"
            break
        assert result["complete_script"] and result["replay"]["status"] == "passed"
        assert {c["role"] for c in result["model_calls"]} == {"design", "execute"}
        if identity == "required-clarification":
            assert len(provider.requests) == 1 and result["answers"] and result["version"] == 2
        else:
            assert not provider.requests
    else:
        report["status"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", choices=list(INPUTS), help="Diagnostic subset only; cannot satisfy G3")
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        report = asyncio.run(verify(args.output, args.offline, args.case))
    except Exception as error:
        report = {"stage": 3, "real_model": not args.offline, "status": "failed", "error": Settings.load().redact(f"{type(error).__name__}: {error}")}
    (args.output / "agent-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "real_model": not args.offline}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
