"""Bounded, model-free observations and independent requirement coverage review."""
import json
import hashlib
import re
from datetime import datetime
from pathlib import Path
import shutil
import uuid
from urllib.parse import urlsplit, urlunsplit


def public_url(value):
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    netloc = hostname + (":" + str(parsed.port) if parsed.port is not None else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def archive_replay(directory, *, standalone=False):
    directory = Path(directory).resolve()
    names = (["standalone-junit.xml"] if standalone else ["pytest.log", "junit.xml", "replay-timing.json", "replay-resource-events.json"])
    names.append("replay-suite")
    existing = [directory / name for name in names if (directory / name).exists()]
    if not existing:
        return None
    target = directory / "replay-history" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    target.relative_to(directory)
    target.mkdir(parents=True)
    for source in existing:
        if source.is_dir():
            shutil.copytree(source, target / source.name)
        else:
            shutil.copy2(source, target / source.name)
    return target


async def actual_condition(page, condition):
    from .actions import locator_expression
    if condition.operator == "url":
        return public_url(page.url)
    locator = eval(locator_expression(condition.target), {"page": page, "re": re})
    count = await locator.count()
    if condition.operator == "count":
        return count
    if condition.operator in {"visible", "hidden"}:
        visible = count == 1 and await locator.is_visible()
        return visible if condition.operator == "visible" else not visible
    texts = await locator.all_text_contents()
    return texts[0][:1000] if count == 1 else {"count": count, "texts": [s[:1000] for s in texts[:8]]}


def condition_fingerprint(condition):
    return hashlib.sha256(json.dumps(condition.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def review_coverage(suite, execution):
    """Check recorded assertions against the frozen plan, never an Agent's prose."""
    review = []
    scenes = execution.get("scenarios", {})
    for rule in suite.rules:
        planned, observed, missing = [], [], []
        for scene in suite.scenarios:
            checkpoints = {c["id"]: c for c in scenes.get(scene.id, {}).get("checkpoints", [])}
            for checkpoint in scene.checkpoints:
                conditions = [c for c in checkpoint.acceptance if c.rule_id == rule.id]
                for condition in conditions:
                    identity = f"{scene.id}:{checkpoint.id}:{condition.target}:{condition.operator}"
                    planned.append(identity)
                    actual = checkpoints.get(checkpoint.id, {})
                    matches = [e for e in actual.get("evidence", []) if e.get("rule_id") == rule.id
                               and e.get("target") == condition.target and e.get("operator") == condition.operator
                               and (e.get("condition_fingerprint") == condition_fingerprint(condition) if e.get("condition_fingerprint")
                                    else e.get("expected") == condition.expected)]
                    status = matches[0].get("status") if len(matches) == 1 else "missing"
                    observed.append({"checkpoint": f"{scene.id}:{checkpoint.id}", "status": status})
                    if status != "passed":
                        missing.append(identity)
        review.append({"rule_id": rule.id, "required": rule.required, "planned": planned, "observed": observed,
                       "verified": bool(planned) and not missing, "unverified_checks": missing})
    return {"rules": review, "all_required_verified": all(r["verified"] for r in review if r["required"]),
            "reported_pass_without_required_evidence": execution.get("status") == "passed" and any(
                not r["verified"] for r in review if r["required"])}


def build_bundle(suite, execution):
    from .domain import Suite
    if execution.get("requirements_fingerprint") not in (None, suite.requirements_fingerprint()):
        raise ValueError("Execution and requirement versions differ; old failures cannot be reinterpreted under modified expectations")
    public_plan = Suite.model_validate(execution.get("sanitized_plan", suite.model_dump()))
    catalog = execution.get("source_catalog", {})
    missing_sources = sorted({r.source.reference for r in public_plan.rules
                              if catalog.get(r.source.reference, {}).get("kind") != r.source.kind})
    evidence = []
    def add(kind, **values):
        item = {"id": f"ev-{len(evidence) + 1:04d}", "kind": kind, **values}
        evidence.append(item)
        return item["id"]
    for rule in public_plan.rules:
        add("requirement", rule_id=rule.id, statement=rule.statement, source=rule.source.model_dump(), required=rule.required)
    if suite.fixture == "local_shop":
        contract = next((entry["text"] for entry in catalog.values() if entry.get("kind") == "contract"
                         and isinstance(entry.get("text"), dict) and "login_success_ui" in entry["text"]), None)
        if contract:
            add("requirement", rule_id="shop-ui-contract", statement=contract["login_success_ui"],
                source={"kind": "contract", "reference": "recorded-contract.login_success_ui"}, required=False)
    for scene in suite.scenarios:
        result = execution.get("scenarios", {}).get(scene.id, {})
        for action in result.get("events", []):
            add("action", scenario_id=scene.id, operation=action.get("kind"), **{k: v for k, v in action.items() if k != "kind"})
        for checkpoint in result.get("checkpoints", []):
            if checkpoint.get("fixture_state"):
                add("setup", scenario_id=scene.id, checkpoint_id=checkpoint["id"], **checkpoint["fixture_state"])
            add("checkpoint", scenario_id=scene.id, checkpoint_id=checkpoint["id"], status=checkpoint["status"],
                error=checkpoint.get("error"), blocked_by=checkpoint.get("blocked_by"))
            for assertion in checkpoint.get("evidence", []):
                assertion = {k: v for k, v in assertion.items() if k != "id"}
                add("assertion", scenario_id=scene.id, checkpoint_id=checkpoint["id"], **assertion)
            if checkpoint.get("observation"):
                add("observation", scenario_id=scene.id, checkpoint_id=checkpoint["id"], **checkpoint["observation"])
            if checkpoint.get("screenshot"):
                add("screenshot", scenario_id=scene.id, checkpoint_id=checkpoint["id"], path=checkpoint["screenshot"],
                    scope="masked artifact exists; pixels have not been supplied to the model")
        for event in result.get("network", []):
            add("network", scenario_id=scene.id, **event)
    return {"schema_version": 1, "query": public_plan.query, "requirements_fingerprint": suite.requirements_fingerprint(),
            "execution_status": execution.get("status", "error"), "source_status": "missing" if missing_sources else "confirmed",
            "missing_source_refs": missing_sources,
            "confirmed_sources": catalog,
            "coverage": review_coverage(suite, execution), "evidence": evidence,
            "planned_actions": [{"scenario_id": s.id, **a.model_dump()} for s in public_plan.scenarios for a in s.actions],
            "planned_checks": [{"scenario_id": s.id, "checkpoint_id": c.id, **a.model_dump()}
                               for s in public_plan.scenarios for c in s.checkpoints for a in c.acceptance]}


def model_bundle(bundle):
    # File paths may reveal evaluator labels; masked image pixels are not supplied.
    data = json.loads(json.dumps(bundle))
    for item in data["evidence"]:
        item.pop("path", None)
    return data
