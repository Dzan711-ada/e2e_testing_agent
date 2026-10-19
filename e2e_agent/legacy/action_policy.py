"""Keep private action arguments outside native graph checkpoints."""
from copy import deepcopy
import re

from pydantic import ValidationError

from .actions import ActionError, compile_action
from .domain import Action, Diagnosis, RepairPlan, Suite


def public_action_ids(suite):
    """Identifiers are public protocol data, even when a secret has that spelling."""
    return frozenset({suite.id, *[resource.id for resource in suite.resources],
        *[rule.id for rule in suite.rules], *[rule.source.reference for rule in suite.rules],
        *[scene.id for scene in suite.scenarios],
        *[checkpoint.id for scene in suite.scenarios for checkpoint in scene.checkpoints],
        *[action.id for scene in suite.scenarios for action in scene.actions]})


def public_action_locators(suite):
    """Known contract locators, excluding free page-text references."""
    return frozenset({target for target in (
        *[action.target for scene in suite.scenarios for action in scene.actions],
        *[condition.target for scene in suite.scenarios for checkpoint in scene.checkpoints
          for condition in checkpoint.acceptance]) if target and not target.startswith("text:")})


_OWNED_BUDGET_MESSAGES = frozenset({
    "Plan exceeds action budget", "No action budget remains for a required checkpoint",
    "Design exceeded the supplied scope or budget", "Model call budget exhausted",
    "Execution budget exhausted", "Suite execution budget exhausted", "Repair budget exhausted",
    "Repair attempt budget exceeded", "Repair exceeds the original suite action budget",
    "Repair exceeds the original active execution budget", "Protected inputs, scope, actions or budgets changed",
})


def safe_validation_message(error):
    """Summarize Pydantic errors without input, repr, context or validator prose.

    A custom validator's message may itself quote the rejected value. Error
    codes and the presence of a budget field retain useful program-owned
    constraint information without copying that message. Other exceptions
    still require the caller's runtime redactor.
    """
    if not isinstance(error, ValidationError):
        return str(error)
    items = error.errors(include_input=False, include_url=False, include_context=False)
    owned = [message for message in sorted(_OWNED_BUDGET_MESSAGES)
             if any(item.get("msg") in {message, "Value error, " + message} for item in items)]
    if owned:
        return "; ".join(owned)
    codes = sorted({item["type"] for item in items
                    if re.fullmatch(r"[a-z_]{1,80}", item.get("type", ""))})
    budget = any(part in {"budget", "max_actions", "max_model_calls", "max_seconds",
                         "max_repairs_per_failure", "max_repairs_per_scenario"}
                 for item in items for part in item.get("loc", ()))
    return "Structured value validation failed" + (" (budget constraint)" if budget else "") + (
        ": " + ", ".join(codes[:6]) if codes else "")


def _private(value, redact):
    return isinstance(value, str) and redact(value) != value


def _public_spelling(value, public_ids, redact):
    """Only already-public collisions may explain a redacted substring.

    Do not delete ID fragments before redaction: a private value may itself
    contain a public word such as ``login``. Compare the complete redaction to
    the redaction explained by public tokens instead, so a longer secret
    cannot be concealed by removing one of its substrings.
    """
    masked = redact(value)
    if masked == value:
        return True
    aliases = sorted((identity for identity in public_ids if identity and _private(identity, redact)),
                     key=len, reverse=True)
    if not aliases:
        return False
    pattern = re.compile("|".join(re.escape(identity) for identity in aliases))
    explained = pattern.sub(lambda match: redact(match.group(0)), value)
    return explained == masked


def validate_graph_actions(suite, actions, *, redact=lambda value: value, public_locators=()):
    """Validate action carriers before any graph input/update is checkpointed.

    This gate is deliberately narrower than site/repair policy. It uses the
    existing compiler's sensitive-input rule and prevents already acquired
    resource values from becoming public action text. No input value is echoed
    when a candidate is refused.
    """
    ids = public_action_ids(suite)
    original_ids = {action.id for scene in suite.scenarios for action in scene.actions}
    checkpoints = {checkpoint.id for scene in suite.scenarios for checkpoint in scene.checkpoints}
    resources = {resource.id: resource for resource in suite.resources}
    locators = set(public_locators) | set(public_action_locators(suite))
    result = []
    try:
        for candidate in actions:
            action = Action.model_validate(candidate.model_dump() if isinstance(candidate, Action) else candidate)
            if action.checkpoint_id not in checkpoints or set(action.preconditions) - checkpoints:
                raise ActionError("Action has an undeclared checkpoint reference")
            if action.resource_id is not None and action.resource_id not in resources:
                raise ActionError("Action has an undeclared resource reference")
            if action.kind in {"fill", "select"} and action.resource_id and action.field_role:
                aliases = {"account": "username", "password": "password", "phone": "phone", "sms_code": "sms_code"}
                if action.field_role != aliases[resources[action.resource_id].kind]:
                    raise ActionError("Action field role does not match its resource")
            if _private(action.id, redact) and not _public_spelling(action.id, ids, redact):
                raise ActionError("Action identity contains a private resource value")
            if _private(action.intent, redact) and not (action.intent == action.id and action.id in original_ids):
                raise ActionError("Action intent contains a private resource value")
            if _private(action.postcondition, redact):
                raise ActionError("Action description contains a private resource value")
            if _private(action.target, redact):
                if action.target not in locators or not _public_spelling(action.target, ids, redact):
                    raise ActionError("Action target contains a private resource value")
            if _private(action.value, redact):
                if action.kind != "navigate" or not _public_spelling(action.value, ids, redact):
                    raise ActionError("Literal action input contains a private resource value")
            # The returned source is discarded; it never enters graph state.
            compile_action(action, "http://127.0.0.1/owned-action-check")
            result.append(action)
    except ValidationError as error:
        raise ActionError(safe_validation_message(error)) from None
    return result


_ACTION_ENUMS = {
    "kind": frozenset({"observe", "navigate", "fill", "click", "select", "check", "wait", "request_resource"}),
    "field_role": frozenset({"username", "password", "email", "phone", "sms_code", None}),
}
_ACTION_IDS = frozenset({"id", "checkpoint_id", "resource_id"})


def redact_action_record(value, redact, *, public_locators=(), public_ids=()):
    """A display-only view of rejected/proposed actions; never executable state.

    Preserve confirmed public identities where supplied. Unknown candidate
    identities and free text receive the same redaction as other evidence.
    Sensitive literal fills are hidden even before their value is registered.
    """
    if isinstance(value, (list, tuple)):
        return [redact_action_record(item, redact, public_locators=public_locators, public_ids=public_ids)
                for item in value]
    if not isinstance(value, dict):
        return redact(value) if isinstance(value, str) else deepcopy(value)
    ids, locators = set(public_ids), set(public_locators)
    sensitive_fill = value.get("kind") in {"fill", "select"} and value.get("value") is not None and (
        value.get("field_role") is not None or
        any(word in str(value.get("target") or "").lower() for word in ("password", "phone", "code", "username")))
    result = {}
    for key, item in value.items():
        if key in _ACTION_ENUMS and isinstance(item, (str, type(None))) and item in _ACTION_ENUMS[key]:
            result[key] = deepcopy(item)
        elif key in _ACTION_IDS and isinstance(item, str) and _public_spelling(item, ids, redact):
            result[key] = deepcopy(item)
        elif key == "target" and isinstance(item, str) and item in locators and _public_spelling(item, ids, redact):
            result[key] = deepcopy(item)
        elif key == "intent" and item == value.get("id") and item in ids:
            result[key] = deepcopy(item)
        elif key == "value" and value.get("kind") == "navigate" and isinstance(item, str) and _public_spelling(item, ids, redact):
            result[key] = item
        elif key == "value" and sensitive_fill:
            result[key] = "[RESOURCE]"
        elif key == "preconditions" and isinstance(item, (list, tuple)):
            result[key] = [entry if entry in ids else redact(entry) for entry in item]
        else:
            result[key] = redact(item)
    return result


_ROLE_PUBLIC_FIELDS = frozenset({
    "id", "ref", "kind", "status", "operator", "category", "type", "attribution",
    "run_id", "suite_id", "scene_id", "scenario_id", "checkpoint_id", "action_id", "rule_id",
    "resource_id", "field_role", "reference", "namespace", "context_id", "binding_id", "challenge_id",
    "acquisition", "fixture", "initial_state", "resource_type",
    "phone_ref", "account_ref", "password_ref", "blocked_by", "restart_checkpoint", "method",
    "requirements_fingerprint", "protected_data_fingerprint", "condition_fingerprint", "agent_id",
    "evidence_ids", "counter_evidence_ids", "unverified_rule_ids", "validation_conditions",
    "required_validation_conditions", "allowed_rule_ids", "source_ids", "depends_on", "preconditions",
    "allowed_equivalent_targets", "planned_scenarios",
    "decision", "phase", "source_status", "initial_status", "execution_status", "stability",
})
_FIXED_PUBLIC = frozenset({
    "account", "password", "phone", "sms_code", "username", "email", "fixture", "runtime",
    "user", "clarification", "contract", "local_shop", "empty", "registered_account",
    "pending", "running", "waiting_user", "passed", "failed", "error", "blocked", "cancelled",
    "visible", "hidden", "equals", "count", "url", "suspected_product", "agent_execution",
    "environment", "requirements", "unknown", "design", "execution", "diagnosis", "repair",
    "observe", "navigate", "fill", "click", "select", "check", "wait", "request_resource",
    "text", "tel", "number", "submit", "button", "input", "div", "form", "a", "article",
    "Username", "Password", "Phone", "Code", "Log in", "Register account", "Add to cart",
    "Send code", "Verify code", "GET", "POST", "document", "xhr", "fetch",
    "requirement", "action", "assertion", "observation", "checkpoint", "setup", "screenshot", "network",
    "confirmed", "missing", "approved", "stopped", "consistent", "inconsistent", "unverified",
    "ready", "needs_clarification", "conflict", "clarify", "execute", "export", "replay", "analyze", "complete",
})


def _domain_keys():
    keys = set()
    for model in (Suite, RepairPlan, Diagnosis):
        schema = model.model_json_schema()
        for item in (schema, *schema.get("$defs", {}).values()):
            keys.update(item.get("properties", {}))
    return keys


# These keys are owned by our typed domain and role/graph envelopes. They do
# not acquire secrecy merely because a resource has the same spelling.
_ROLE_OWNED_KEYS = frozenset(_domain_keys()) | _ROLE_PUBLIC_FIELDS | frozenset({
    "answers", "observations", "observation", "current_observation", "candidates", "label", "tag", "role",
    "text", "form_testid", "source_catalog", "sanitized_plan", "confirmed_sources", "execution", "replay",
    "replay_result", "previous_replay_run_id", "replay_detail", "replay_artifacts",
    "decision", "phase", "rounds", "error", "complete_script", "diagnostics", "diagnostic_execution",
    "diagnostic_origin", "diagnostic_run_id", "parent_run_id", "agent_protocol_version", "graph_protocol_version",
    "resume_frozen", "repair_enabled", "repair_count", "stability", "plan_file", "checkpoint_suite",
    "scenarios", "checkpoints", "events", "network", "evidence", "coverage", "diagnosis", "assessment",
    "clarification_questions", "investigations", "repair_recommendations", "coverage_findings", "uncertainties",
    "explanation", "bundle", "attempt", "decision", "reason", "original_action", "failed_event", "proposed_plan",
    "source_status", "missing_source_refs", "planned_actions", "planned_checks", "goal", "contract",
    "max_actions", "model_call_limit", "execution_action_costs", "confirmed_execution_policy", "rule_scope_policy",
    "sources", "checkpoint", "scenario", "checkpoint_role", "account_resources_ready", "observed_fixture_protocol",
    "immutable_policy", "action_field_examples", "example_policy", "allowed_equivalent_targets", "side_effect_attempted",
    "provider", "model", "input_scope", "structured_method", "usage", "finish_reason", "reported_model",
    "system_fingerprint", "duration_seconds", "rejected_constraints", "correction_feedback", "previous_validator_feedback",
    "path", "message", "status", "timing", "active_seconds", "human_wait_seconds", "fixture_state", "stats",
    "source", "operation", "condition_fingerprint", "input_matches", "resource_binding", "contexts_created", "complete",
    "all_required_verified", "reported_pass_without_required_evidence", "planned", "observed", "verified", "unverified_checks",
    "login_success_ui", "password_rejection", "anonymous_cart", "registration", "login", "cart", "sms",
    "execution_protocol_version", "effective_suite", "effective_actions", "public_ids", "public_locators",
    "scene_order", "checkpoint_order", "scene_results", "checkpoint_results", "checkpoint_statuses", "checkpoint_outcome",
    "action_queue", "action_index", "current_action", "last_action_id", "planned_checkpoints", "action_count",
    "pending_resource", "failure", "failure_execution", "repair_queue", "repair_index", "repair_active", "repair_original",
    "repair_history", "repair_attempts", "scene_repair_attempts", "repair_bundle", "repair_assessment", "repair_proposal",
    "repair_attempt", "scene_repair_attempt", "repaired", "initial_failures", "next_step", "execution_status", "graph_event_index",
})


def redact_role_payload(value, redact, *, public_ids=(), public_locators=()):
    """Sanitize this application's typed role payloads without renaming protocol.

    This is a display/model boundary, never an executable-plan constructor.
    Public provenance comes from the frozen contract and observed nonprivate
    locator metadata, not from a rejected model candidate.
    """
    ids, locators = set(public_ids), set(public_locators)

    def public(item):
        if isinstance(item, str):
            return item if item in _FIXED_PUBLIC or _public_spelling(item, ids, redact) else redact(item)
        if isinstance(item, (list, tuple)):
            return [public(entry) for entry in item]
        return walk(item)

    def walk(item):
        if isinstance(item, (list, tuple)):
            return [walk(entry) for entry in item]
        if not isinstance(item, dict):
            return redact(item) if isinstance(item, str) else item
        if item.get("kind") in _ACTION_ENUMS["kind"] and "checkpoint_id" in item and "intent" in item and "id" in item:
            return redact_action_record(item, redact, public_ids=ids, public_locators=locators)
        output = {}
        for key, entry in item.items():
            safe_key = key if not isinstance(key, str) or key in _ROLE_OWNED_KEYS or _public_spelling(key, ids, redact) else redact(key)
            if key == "candidates" and isinstance(entry, dict):
                output[safe_key] = {public(ref): [walk(field) for field in fields] for ref, fields in entry.items()}
            elif key in _ROLE_PUBLIC_FIELDS:
                output[safe_key] = public(entry)
            elif key == "target" and entry in locators:
                output[safe_key] = public(entry)
            elif key in {"tag", "role", "label", "form_testid"}:
                output[safe_key] = public(entry)
            elif key == "text" and isinstance(entry, str) and entry in _FIXED_PUBLIC:
                output[safe_key] = entry
            elif key in {"original_action", "action", "proposed_actions"}:
                output[safe_key] = redact_action_record(entry, redact, public_ids=ids, public_locators=locators)
            else:
                output[safe_key] = walk(entry)
        return output

    return walk(value)
