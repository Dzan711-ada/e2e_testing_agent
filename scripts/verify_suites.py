"""Stage-2 browser acceptance: chain state, isolation, blocking and independent suite replay."""
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
from e2e_agent.sample_shop import shop_server
from e2e_agent.suite_codegen import export_suite
from e2e_agent.suite_demo import shop_suite


def assert_normal(record):
    assert record["status"] == "passed", record
    assert set(record["scenarios"]) == {"main-chain", "wrong-password", "unauthenticated"}
    main = record["scenarios"]["main-chain"]
    assert main["stats"]["login_count"] == 1 and main["contexts_created"] == 1
    assert len(main["checkpoints"]) == 4 and all(c["status"] == "passed" for c in main["checkpoints"])
    assert {c["context_id"] for c in main["checkpoints"]} == {main["context_id"]}
    assert len({s["context_id"] for s in record["scenarios"].values()}) == 3
    assert all(record["scenarios"][s]["stats"]["login_count"] == 0 for s in ("wrong-password", "unauthenticated"))
    assert sum(e["action_id"] == "submit-login" for e in main["events"]) == 1


async def verify(output):
    settings, suite = Settings.load(), shop_suite()
    report = {"stage": 2, "scenarios": [], "model_calls": 0}
    with shop_server() as fixture:
        for iteration in (1, 2):
            result = await execute_suite(suite, output / f"generation-{iteration}", settings.browsers_path, fixture=fixture)
            assert_normal(result)
            report["scenarios"].append({"id": f"normal-generation-{iteration}", "expected": "passed", "actual": result["status"],
                                        "result_path": str(output / f"generation-{iteration}/suite-result.json")})
            if iteration == 1:
                first_namespaces = {s["namespace"] for s in result["scenarios"].values()}
            else:
                assert not first_namespaces & {s["namespace"] for s in result["scenarios"].values()}
        failed = await execute_suite(suite, output / "login-failure", settings.browsers_path, fixture=fixture, faults={"main-chain": ["valid_login_denied"]})
        assert failed["status"] == "failed", failed
        checkpoints = {c["id"]: c for c in failed["scenarios"]["main-chain"]["checkpoints"]}
        assert checkpoints["login"]["status"] == "failed"
        assert all(checkpoints[c]["status"] == "blocked" for c in ("cart", "refresh"))
        assert checkpoints["cart"]["blocked_by"] == "login"
        assert not failed["scenarios"]["main-chain"]["complete"]
        assert not any(e["action_id"] == "add-book" for e in failed["scenarios"]["main-chain"]["events"])
        assert all(failed["scenarios"][s]["status"] == "passed" for s in ("wrong-password", "unauthenticated"))
        report["scenarios"].append({"id": "login-failure-blocking", "expected": "failed", "actual": failed["status"], "dependent_status": "blocked"})
        ambiguous_data = suite.model_dump()
        for action in ambiguous_data["scenarios"][0]["actions"]:
            if action["id"] == "submit-login":
                action["target"] = "button:Log in"
        ambiguous = await execute_suite(Suite.model_validate(ambiguous_data), output / "ambiguous", settings.browsers_path, fixture=fixture,
                                        faults={"main-chain": ["ambiguous_buttons"]})
        main = ambiguous["scenarios"]["main-chain"]
        assert main["status"] == "error" and main["stats"]["login_count"] == 0
        assert "found 2" in next(c for c in main["checkpoints"] if c["id"] == "login")["error"]
        report["scenarios"].append({"id": "ambiguous-target-refused", "expected": "error", "actual": main["status"]})
    exported = output / "exported"
    manifest = export_suite(suite, exported, settings.browsers_path)
    replay = await run_replay(settings, exported)
    assert replay.status == "passed" and replay.tests == 3, replay
    rerun = json.loads((exported / "replay-suite/suite-result.json").read_text(encoding="utf-8"))
    assert_normal(rerun)
    assert not first_namespaces & {s["namespace"] for s in rerun["scenarios"].values()}
    report["scenarios"].append({"id": "independent-suite-replay", "expected": "passed", "actual": replay.status,
                                "replay": replay.model_dump(), "expected_tests": manifest["tests"], "result_path": str(exported / "replay-suite/suite-result.json")})
    report["status"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        report = asyncio.run(verify(args.output))
    except Exception as error:
        report = {"stage": 2, "status": "failed", "error": Settings.load().redact(f"{type(error).__name__}: {error}")}
    (args.output / "suite-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
