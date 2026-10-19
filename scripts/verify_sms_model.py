"""True-model SMS integration, with a guard against plaintext resources in role payloads."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.agent_workflow import ScriptedAnswers, run_agent_suite
from e2e_agent.agents import RoleModel
from e2e_agent.config import Settings
from scripts.verify_resources import CapturingProvider, PHONE, scan


class GuardedModel(RoleModel):
    def __init__(self, settings, secrets):
        super().__init__(settings)
        self.secrets, self.guarded_payloads = secrets, 0

    async def call(self, role, schema, system, payload):
        serialized = json.dumps(payload, ensure_ascii=False)
        if any(value and value in serialized for value in self.secrets):
            raise ValueError("A plaintext resource was refused before model transmission")
        self.guarded_payloads += 1
        return await super().call(role, schema, system, payload)


async def verify(output):
    settings = Settings.load()
    settings.require_model()
    secrets = [PHONE, settings.deepseek_api_key, settings.qwen_api_key]
    provider = CapturingProvider(secrets, use_test_delivery=True)
    model = GuardedModel(settings, secrets)
    query = "仅测试模拟短信登录和购物车：运行时向资源提供者请求虚构手机号，发送一次模拟验证码，再请求当次验证码完成登录；同一会话加购一本 Agent Handbook，检查数量为 1，刷新后再次检查数量为 1。范围仅限这条短信业务链。"
    result = await run_agent_suite(settings, query=query, run_dir=output / "run", answers_provider=ScriptedAnswers([]), transport=model, resource_provider=provider)
    assert result["status"] == "passed" and result["complete_script"], result.get("error")
    assert result["replay"]["status"] == "passed"
    for scene in result["execution"]["scenarios"].values():
        assert scene["stats"] == {"login_count": 1, "send_count": 1}
        assert scene["contexts_created"] == 1
    rerun = json.loads((output / "run/replay-suite/suite-result.json").read_text(encoding="utf-8"))
    assert all(s["stats"] == {"login_count": 1, "send_count": 1} for s in rerun["scenarios"].values())
    assert [r["kind"] for r in provider.requests].count("sms_code") == 2
    checked = scan(output, secrets)
    return {"stage": 4, "status": "passed", "real_model": True, "input_version": 1, "query": query,
            "model_calls": model.calls, "guarded_payloads": model.guarded_payloads, "plaintext_resource_leaks": 0,
            "scanned_artifacts": checked, "actual_console_input": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        report = asyncio.run(verify(args.output))
    except Exception as error:
        report = {"stage": 4, "status": "failed", "real_model": True, "error": Settings.load().redact(f"{type(error).__name__}: {error}")}
    (args.output / "sms-model-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "real_model": True}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
