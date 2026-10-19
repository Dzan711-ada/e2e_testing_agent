"""Run one implemented stage gate, keeping fresh, code-bound evidence."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.config import Settings

TZ = timezone(timedelta(hours=8))


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configuration_fingerprints(root=ROOT):
    files = [root / ".env"]
    for name in (".idea", ".run"):
        files.extend(p for p in (root / name).rglob("*") if p.is_file())
    return {p.relative_to(root).as_posix(): fingerprint(p) for p in files if p.is_file()}


def assert_configuration_unchanged(before, root=ROOT):
    after = configuration_fingerprints(root)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    if changed:
        raise ValueError("Configuration changed during verification: " + ", ".join(changed))


def sources():
    files = [p for p in ROOT.iterdir() if p.is_file() and p.name != ".env"]
    for name in ("e2e_agent", "tests", "scripts", "docs"):
        files += [p for p in (ROOT / name).rglob("*") if p.is_file() and "__pycache__" not in p.parts
                  and p.name != "DEVELOPMENT_PROGRESS.md"]
    return {p.relative_to(ROOT).as_posix(): fingerprint(p) for p in sorted(files)}


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def smoke_results(data):
    expected = {"fixed_browser_flow": "passed", "deliberate_assertion_failure": "failed"}
    assert set(data) == set(expected), "Incorrect smoke scenario collection"
    results = []
    for identity, status in expected.items():
        item = data[identity]
        result_path = Path(item["run_dir"]) / "result.json"
        actual = json.loads(result_path.read_text(encoding="utf-8"))
        events = actual["events"]
        replay = actual["replay"]
        assert actual["status"] == item["status"] == status
        assert events and actual["index"] == len(actual["actions"])
        assert replay["status"] == status and replay["tests"] == 1
        assert not replay["errors"] and not replay["skipped"]
        if status == "passed":
            assert all(event["ok"] for event in events) and replay["exit_code"] == 0
        else:
            assert all(event["ok"] for event in events[:-1])
            assert not events[-1]["ok"] and events[-1]["error_type"] == "AssertionError"
            assert replay["failures"] == 1 and replay["exit_code"] == 1
        results.append({"id": identity, "expected": status, "actual": status,
                        "generation": "passed" if all(e["ok"] for e in events) else "failed",
                        "replay": replay, "result_path": str(result_path)})
    return results


def verify_manual(path):
    from scripts.manual_sms import hashes
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["stage"] == 4 and data["status"] == "passed" and data["actual_console_input"] is True
    assert data["provider"] == "ConsoleProvider" and data["headless"] is False
    assert data["input_channel"] == "parent_console_stdin" and data["resource_mode"] == "public_fictional_local_sms"
    assert data["source_files"] == hashes(), "Manual evidence is from another resource implementation"
    rounds = data["rounds"]
    assert len(rounds) == 2 and {r["id"] for r in rounds} == {"first", "independent-replay"}
    assert len({r["run_id"] for r in rounds}) == 2 and all(r["status"] == "passed" for r in rounds)
    requests = data["console_requests"]
    assert len(requests) == 4 and all(r["status"] == "provided" for r in requests)
    assert [r["kind"] for r in requests] == ["phone", "sms_code", "phone", "sms_code"]
    assert requests[1]["challenge_id"] != requests[3]["challenge_id"]
    assert data["replay"]["status"] == "passed" and data["replay"]["tests"] == 1
    return {"path": str(path), "sha256": fingerprint(path), "status": "passed", "rounds": rounds,
            "actual_console_input": True, "source_files": data["source_files"]}


def verify(stage, baseline_path, manual_path=None, wait_for_manual=0):
    settings = Settings.load()
    run_id = datetime.now(TZ).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    out = ROOT / "runtime" / "development" / f"stage-{stage}" / run_id
    out.mkdir(parents=True)
    record = {"schema_version": 1, "stage": stage, "run_id": run_id, "started": now(), "status": "running",
              "gate": f"G{stage}", "model_calls": 0, "commands": [], "scenarios": [],
              "baseline": str(baseline_path), "source_files": sources(), "protected_files": configuration_fingerprints()}
    summary = out / "verification.json"
    write_json(summary, record)
    legacy = ROOT / "runtime" / "smoke_validation.json"
    original = legacy.read_bytes() if legacy.exists() else None
    if original is not None:
        (out / "previous-smoke-validation.json").write_bytes(original)

    def run(name, arguments, *, cwd=ROOT, timeout=180):
        command = [sys.executable, *map(str, arguments)]
        print(f"[{now()}] {name}", flush=True)
        started = time.monotonic()
        entry = {"id": name, "command": command, "cwd": str(cwd), "started": now(),
                 "expected_exit_code": 0, "log": str(out / (name + ".log"))}
        record["commands"].append(entry)
        write_json(summary, record)
        try:
            process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                raw, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    process.kill()
                raw, _ = process.communicate()
                entry["timed_out"] = True
            entry["exit_code"] = process.returncode
            output = settings.redact(raw.decode("utf-8", errors="replace"))
            Path(entry["log"]).write_text(output, encoding="utf-8")
        finally:
            entry["duration_seconds"] = round(time.monotonic() - started, 3)
            write_json(summary, record)
        assert not entry.get("timed_out") and entry["exit_code"] == 0, f"{name} failed; see {entry['log']}"
        return output

    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        assert baseline["stage"] == 0 and baseline["kind"] == "fresh_baseline" and baseline["status"] == "passed"
        assert {c["name"] for c in baseline["commands"]} == {"unit", "browser", "smoke"}
        assert all(c["exit_code"] == 0 for c in baseline["commands"])
        assert (baseline_path.parent / "source-baseline.zip").is_file()
        record["baseline_sha256"] = fingerprint(baseline_path)
        if stage > 0:
            previous_runs = sorted((ROOT / "runtime/development" / f"stage-{stage - 1}").glob("*/verification.json"))
            assert previous_runs, "Previous stage has no verification evidence"
            previous_path = previous_runs[-1]
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            assert previous["status"] == "passed" and previous["stage"] == stage - 1, "Previous stage gate is not passed"
            record["previous_gate"] = {"path": str(previous_path), "sha256": fingerprint(previous_path)}
            record["changes_since_previous"] = {
                "added": sorted(set(record["source_files"]) - set(previous["source_files"])),
                "removed": sorted(set(previous["source_files"]) - set(record["source_files"])),
                "modified": sorted(p for p in record["source_files"] if p in previous["source_files"] and record["source_files"][p] != previous["source_files"][p])}
        record["initial_protected_files"] = baseline["protected_files"]
        record["configuration_changes_before_verification"] = sorted(
            p for p in baseline["protected_files"].keys() | record["protected_files"].keys()
            if baseline["protected_files"].get(p) != record["protected_files"].get(p))
        record["dependencies"] = {name: importlib.metadata.version(name) for name in (
            "langgraph", "langgraph-checkpoint", "langchain-core", "langchain-openai", "pydantic", "playwright", "pytest", "Flask")}
        record["lock_sha256"] = fingerprint(ROOT / "uv.lock")
        assert record["lock_sha256"] == baseline["source_files"]["uv.lock"], "Unreviewed dependency change"
        with zipfile.ZipFile(out / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in record["source_files"]:
                archive.write(ROOT / path, path)
        unit = run("offline-regression", ["-m", "unittest", "discover", "-s", "tests", "-v"])
        match = re.search(r"Ran (\d+) tests? in", unit)
        baseline_unit = (baseline_path.parent / "unit.log").read_text(encoding="utf-8")
        initial_count = int(re.search(r"Ran (\d+) tests? in", baseline_unit)[1])
        assert match and int(match[1]) >= initial_count > 0, "Missing regression collection"
        assert re.search(r"^OK\s*$", unit, re.MULTILINE), "Skipped or unsuccessful tests"
        record["offline_tests"] = {"total": int(match[1]), "passed": int(match[1]), "failed": 0, "skipped": 0}
        run("browser-check", ["main.py", "check", "--browser"], timeout=60)
        run("original-smoke", ["scripts/smoke_test.py", "--offline"])
        data = json.loads(legacy.read_text(encoding="utf-8"))
        write_json(out / "smoke-validation.json", data)
        record["scenarios"] = smoke_results(data)
        run("workflow-compatibility", ["scripts/verify_workflow_compat.py", "--output", out / "compatibility"])
        compat = json.loads((out / "compatibility" / "compatibility.json").read_text(encoding="utf-8"))
        assert compat["status"] == "passed" and {c["id"] for c in compat["cases"]} == {"resume", "cancel_while_paused"}
        record["compatibility"] = compat
        foreign = out / "foreign-working-directory"
        foreign.mkdir()
        run("foreign-check", [ROOT / "main.py", "check", "--browser"], cwd=foreign, timeout=60)
        run("foreign-demo", [ROOT / "main.py", "demo"], cwd=foreign)
        if stage >= 1:
            run("controlled-shop", ["scripts/verify_shop.py", "--output", out / "shop"], timeout=180)
            shop = json.loads((out / "shop/shop-validation.json").read_text(encoding="utf-8"))
            expected_ids = {"normal-round-1", "normal-round-2", "wrong-password-rejected", "correct-password-rejected",
                            "wrong-password-accepted", "wrong-cart-quantity", "cart-refresh-loss", "simulated-sms-chain"}
            assert shop["status"] == "passed" and {s["id"] for s in shop["scenarios"]} == expected_ids
            assert len(shop["scenarios"]) == len(expected_ids)
            assert all(s["expected"] == s["actual"] for s in shop["scenarios"])
            record["shop"] = shop
        if stage >= 2:
            run("structured-suites", ["scripts/verify_suites.py", "--output", out / "suites"], timeout=240)
            suites = json.loads((out / "suites/suite-validation.json").read_text(encoding="utf-8"))
            expected_ids = {"normal-generation-1", "normal-generation-2", "login-failure-blocking", "ambiguous-target-refused", "independent-suite-replay"}
            assert suites["status"] == "passed" and {s["id"] for s in suites["scenarios"]} == expected_ids
            assert len(suites["scenarios"]) == len(expected_ids) and all(s["expected"] == s["actual"] for s in suites["scenarios"])
            record["suites"] = suites
        if stage >= 3:
            for mode, flags in (("fixed", ["--offline"]), ("real", [])):
                run(f"agents-{mode}", ["scripts/verify_agents.py", *flags, "--output", out / f"agents-{mode}"], timeout=1200)
                agents = json.loads((out / f"agents-{mode}/agent-validation.json").read_text(encoding="utf-8"))
                assert agents["status"] == "passed" and agents["real_model"] == (mode == "real")
                assert agents.get("full_stage_specialty") is True, "Diagnostic subsets cannot satisfy the stage gate"
                assert {s["id"] for s in agents["scenarios"]} == {"clear-goal", "required-clarification"}
                assert len(agents["scenarios"]) == 2 and all(s["actual"] == "passed" and s["complete_script"] for s in agents["scenarios"])
                record[f"agents_{mode}"] = agents
            record["model_calls"] = sum(len(s["model_calls"]) for s in record["agents_real"]["scenarios"])
        if stage >= 4:
            run("resource-lifecycle", ["scripts/verify_resources.py", "--output", out / "resources"], timeout=600)
            resources = json.loads((out / "resources/resource-validation.json").read_text(encoding="utf-8"))
            expected = {"sms-first", "sms-fresh-round", "missing-phone", "cancelled-phone", "missing-code", "wrong-code",
                        "expired-code", "cancel-and-safe-restart", "ipc-replay", "replay-without-resources",
                        "private-account-reference-replay", "standalone-export"}
            assert resources["status"] == "passed" and resources["long_wait_verified"] is True
            assert len(resources["scenarios"]) == len(expected) and {s["id"] for s in resources["scenarios"]} == expected
            assert all(s["actual"] == s["expected"] for s in resources["scenarios"])
            record["resources"] = resources
            run("sms-real-model", ["scripts/verify_sms_model.py", "--output", out / "sms-model"], timeout=600)
            sms_model = json.loads((out / "sms-model/sms-model-validation.json").read_text(encoding="utf-8"))
            assert sms_model["status"] == "passed" and sms_model["real_model"] is True
            assert sms_model["plaintext_resource_leaks"] == 0 and sms_model["guarded_payloads"] > 0
            record["sms_model"] = sms_model
            record["model_calls"] += len(sms_model["model_calls"])
            if stage == 4:
                assert sources() == record["source_files"], "Source changed during automated verification"
                assert_configuration_unchanged(record["protected_files"])
                record.update(automatic_status="passed", status="waiting_user")
                write_json(summary, record)
                print(f"[{now()}] Automatic checks passed; actual parent-console acceptance is required", flush=True)
                deadline = time.monotonic() + wait_for_manual
                ready = False
                while manual_path:
                    try:
                        ready = json.loads(manual_path.read_text(encoding="utf-8")).get("status") != "running"
                    except (OSError, ValueError):
                        ready = False
                    if ready or time.monotonic() >= deadline:
                        break
                    time.sleep(1)
                if not ready:
                    record.update(status="blocked", unresolved=["Mandatory actual console input acceptance is missing; G4 is not passed"])
                    return record, summary
                record["manual"] = verify_manual(manual_path)
            else:
                record["manual_scope"] = "G4 actual human acceptance remains historical evidence; current resource automation is rerun. G7 requires a new human acceptance."
        if stage >= 5:
            run("agent-batch-recovery", ["scripts/verify_agent_batches.py", "--output", out / "agent-batches"], timeout=240)
            batches = json.loads((out / "agent-batches/batch-validation.json").read_text(encoding="utf-8"))
            assert batches["status"] == "passed" and batches["plaintext_resource_leaks"] == 0
            assert set(batches["cases"]) == {"frozen-partial-restart", "nonreusable-account-blocked", "runtime-account-first-and-replay"}
            record["agent_batches"] = batches
            from scripts.diagnostic_fixtures import EXPECTED, INPUT_VERSION
            for mode, flags in (("fixed", ["--offline"]), ("real", [])):
                run(f"diagnosis-{mode}", ["scripts/verify_diagnostics.py", *flags, "--output", out / f"diagnosis-{mode}"], timeout=600)
                diagnosis = json.loads((out / f"diagnosis-{mode}/diagnostic-validation.json").read_text(encoding="utf-8"))
                assert diagnosis["status"] == "passed" and diagnosis["real_model"] == (mode == "real")
                assert diagnosis["full_stage_specialty"] is True and diagnosis["input_version"] == INPUT_VERSION
                assert diagnosis["browser_evidence_version"] == 5
                assert len(diagnosis["scenarios"]) == len(EXPECTED) and {s["id"] for s in diagnosis["scenarios"]} == set(EXPECTED)
                assert diagnosis["critical_passed"] is True and diagnosis["automatic_repairs"] == 0
                assert all(s["expected"] == EXPECTED[s["id"]] == s["actual"] and s["status"] == "passed" for s in diagnosis["scenarios"])
                assert all(s.get("resource_consistency_proven") is True for s in diagnosis["scenarios"] if s["id"].startswith("browser-"))
                record[f"diagnosis_{mode}"] = diagnosis
            assert record["diagnosis_fixed"]["fixed_input_hashes"] == record["diagnosis_real"]["fixed_input_hashes"]
            record["model_calls"] += sum(len(s["model_calls"]) for s in record["diagnosis_real"]["scenarios"])
        if stage >= 6:
            from scripts.verify_repairs import FIXED_CASES, REAL_CASES, INPUT_VERSION as REPAIR_INPUT_VERSION
            for mode, flags in (("fixed", ["--offline"]), ("real", [])):
                run(f"repairs-{mode}", ["scripts/verify_repairs.py", *flags, "--output", out / f"repairs-{mode}"], timeout=600)
                repairs = json.loads((out / f"repairs-{mode}/repair-validation.json").read_text(encoding="utf-8"))
                assert repairs["status"] == "passed" and repairs["real_model"] == (mode == "real")
                assert repairs["input_version"] == REPAIR_INPUT_VERSION and repairs["full_stage_specialty"] is True
                expected = REAL_CASES if mode == "real" else FIXED_CASES
                assert len(repairs["scenarios"]) == len(expected) and {s["id"] for s in repairs["scenarios"]} == expected
                assert all(s["status"] == "passed" and s["expectations_preserved"] for s in repairs["scenarios"])
                assert repairs["plaintext_resource_leaks"] == 0
                record[f"repairs_{mode}"] = repairs
            assert all(record["repairs_fixed"]["input_hashes"][identity] == digest for identity, digest in record["repairs_real"]["input_hashes"].items())
            record["model_calls"] += sum(len(s["model_calls"]) for s in record["repairs_real"]["scenarios"])
        assert sources() == record["source_files"], "Source changed during verification; rerun required"
        assert_configuration_unchanged(record["protected_files"])
        record["status"] = "passed"
        record["unresolved"] = []
    except Exception as error:
        record["status"] = "failed"
        record["unresolved"] = [settings.redact(f"{type(error).__name__}: {error}")]
    finally:
        if original is not None:
            legacy.write_bytes(original)
        record["finished"] = now()
        write_json(summary, record)
    print(f"G{stage}: {record['status']}; {summary}", flush=True)
    return record, summary


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # G7 delegates to a source-bound delivery gate. Earlier gate semantics and
    # the complete G6 implementation remain unchanged and are reused there.
    delivery_args = None
    if "--stage=7" in argv:
        delivery_args = [value for value in argv if value != "--stage=7"]
    elif "--stage" in argv:
        position = argv.index("--stage")
        if position + 1 < len(argv) and argv[position + 1] == "7":
            delivery_args = argv[:position] + argv[position + 2:]
    if delivery_args is not None:
        from scripts.verify_delivery import main as delivery_main
        return delivery_main(delivery_args)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=int, choices=[0, 1, 2, 3, 4, 5, 6, 7], help="Only implemented gates may be selected; stage 7 has its own delivery options")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--manual-evidence", type=Path, help="Required phase-4 actual parent-console acceptance")
    parser.add_argument("--wait-for-manual", type=int, default=0, help="Wait for a not-yet-created manual evidence file after automated checks")
    args = parser.parse_args(argv)
    baselines = sorted((ROOT / "runtime/development/stage-0").glob("*-baseline-*/baseline.json"))
    if not args.baseline and not baselines:
        parser.error("Capture a fresh source baseline and original tests before changing code")
    if not 0 <= args.wait_for_manual <= 3600:
        parser.error("Manual wait must be between 0 and 3600 seconds")
    record, path = verify(args.stage, args.baseline or baselines[-1], args.manual_evidence, args.wait_for_manual)
    print(json.dumps({"status": record["status"], "unresolved": record["unresolved"]}, ensure_ascii=False))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
