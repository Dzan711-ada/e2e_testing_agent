"""LangGraph owns business progress; services perform one bounded operation.

This module has no model/Agent imports. Frozen plans use the same graph as live
planning, so exported replay needs neither model configuration nor API keys.
"""
from copy import deepcopy
import inspect
import hashlib
import json
import shutil
import uuid

from langgraph.graph import StateGraph, START, END

from .domain import Action, RepairPlan, Suite
from .action_policy import safe_validation_message, validate_graph_actions, redact_role_payload
from .graph_state import (ExecutionState, initial_execution_state,
                          redact_execution_record, redact_execution_state)
from .resources import ResourceUnavailable


NODES = ("select_checkpoint", "observe_page", "plan_actions", "execute_action",
         "await_resource", "verify_checkpoint", "diagnose_failure", "plan_repair",
         "validate_repair", "complete_checkpoint")


def materialize_suite(runtime, state):
    """Recover executable semantics from the immutable contract and public actions.

    Masked suite/effective_suite are display views and MUST NOT be parsed as an
    executable/restart plan. Acquired runtime.values never enter this function.
    """
    original = runtime.suite
    if state.get("requirements_fingerprint", original.requirements_fingerprint()) != original.requirements_fingerprint():
        raise ValueError("Execution graph changed protected requirements")
    actions = state.get("effective_actions")
    if actions is None:
        return original
    if set(actions) != {scene.id for scene in original.scenarios}:
        raise ValueError("Execution graph changed scenario identities")
    data = original.model_dump()
    for scene in data["scenarios"]:
        checked = validate_graph_actions(original, actions[scene["id"]],
            redact=runtime.manager.redactor.data, public_locators=state.get("public_locators", ()))
        scene["actions"] = [action.model_dump() for action in checked]
    result = Suite.model_validate(data)
    if result.requirements_fingerprint() != original.requirements_fingerprint():
        raise ValueError("Execution graph changed protected requirements")
    return result


def recursion_limit(suite):
    """Graph transitions have their own cap, independent of dispatch budgets."""
    checkpoints = sum(len(scene.checkpoints) for scene in suite.scenarios)
    repairs = len(suite.scenarios) * suite.budget.max_repairs_per_scenario
    return 32 + 8 * len(suite.scenarios) + 8 * checkpoints + 4 * suite.budget.max_actions + 5 * repairs


def _failure(error):
    message = safe_validation_message(error)
    return {"type": type(error).__name__, "message": message,
            "error": f"{type(error).__name__}: {message}",
            "category": "resource" if isinstance(error, ResourceUnavailable) else
                        "assertion" if isinstance(error, AssertionError) else "error",
            "status": getattr(error, "status", "error")}


def _stop_failure(state, failure):
    outcome = deepcopy(state["checkpoint_outcome"])
    if failure["category"] == "resource":
        outcome.update(status=failure["status"], error=failure["message"])
    elif failure["category"] == "assertion":
        outcome.update(status="failed", error=failure["message"])
    else:
        outcome.update(status="error", error=failure["error"])
    return {"checkpoint_outcome": outcome, "failure": failure,
            "pending_resource": None, "repair_active": False,
            "execution_status": "running", "next_step": "complete_checkpoint"}


def _scene(suite, identity):
    return next(scene for scene in suite.scenarios if scene.id == identity)


def _checkpoint(scene, identity):
    return next(checkpoint for checkpoint in scene.checkpoints if checkpoint.id == identity)


def build_execution_graph(runtime=None, *, runtime_factory=None, bridge,
                          action_planner=None, strategy=None, repair_controller=None,
                          interrupt_resources=True, checkpointer=None, on_plan_ready=None):
    """Return a real compiled graph, suitable as a native parent graph node.

    A supplied runtime or lazy factory owns already-open live services. The
    factory may be async and is called on first entry, after a parent has a
    confirmed Suite. No independent saver is attached unless explicitly given;
    embedding therefore inherits the parent's native checkpoint namespace.
    """
    runtime_ref, events = {"value": runtime}, []
    audit_started = False

    async def live(state):
        if runtime_ref["value"] is None:
            if runtime_factory is None:
                raise ValueError("Execution graph requires runtime services")
            value = runtime_factory(state)
            runtime_ref["value"] = await value if inspect.isawaitable(value) else value
        service = runtime_ref["value"]
        transport = getattr(repair_controller, "transport", None)
        if hasattr(transport, "bind_runtime_redactor"):
            transport.bind_runtime_redactor(service.manager.redactor.data,
                public_ids=state.get("public_ids", ()), public_locators=state.get("public_locators", ()))
        return service

    def repair_history(state, **changes):
        history = deepcopy(state["repair_history"])
        if history:
            history[-1].update(changes)
        return history

    def stop_repair(state, reason, *, history=None):
        return {**_stop_failure(state, state["failure"]), "repair_proposal": None,
            "repair_history": history if history is not None else repair_history(state, decision="stopped", reason=reason)}

    def admitted_locators(state, actions, observation):
        # Only exact current observed references can become public after guard.
        return sorted(set(state.get("public_locators", ())) | {
            action.target for action in actions
            if action.target is not None and action.target in observation.get("candidates", {})})

    def instrument(name, function):
        async def run(state):
            nonlocal audit_started
            service = await live(state)
            if not audit_started:
                directory = service.output_dir
                names = ("execution-state.json", "execution-state-history.jsonl",
                         "execution-events.json", "execution-events.jsonl")
                existing = [directory / filename for filename in names if (directory / filename).is_file()]
                if existing:
                    archive = directory / "execution-history" / uuid.uuid4().hex
                    archive.resolve().relative_to(directory.resolve())
                    archive.mkdir(parents=True, exist_ok=False)
                    for source in existing:
                        shutil.copy2(source, archive / source.name)
                for filename in names:
                    (directory / filename).write_text("[]" if filename == "execution-events.json" else "{}" if filename == "execution-state.json" else "", encoding="utf-8")
                audit_started = True
            try:
                update = await function(state, service)
            except Exception as error:
                # Native checkpoint task errors must never retain a raw
                # validation exception repr containing rejected candidate data.
                safe_error = service.manager.redactor.text(safe_validation_message(error))
                raise RuntimeError(f"{type(error).__name__}: {safe_error}") from None
            view = {**state, **update, "graph_event_index": state.get("graph_event_index", 0) + 1}
            view["budget"] = {**service.suite.budget.model_dump(),
                              "active_seconds": service.clock.active_seconds,
                              "human_wait_seconds": service.clock.wait_seconds,
                              "remaining_actions": service.suite.budget.max_actions - view.get("action_count", 0)}
            safe = redact_execution_state(view, service.manager.redactor.data)
            if repair_controller is not None and "repair_history" in update:
                # The graph owns the sanitized history; publish only mirrors
                # evidence and its return value never controls graph progress.
                repair_controller.publish(safe["repair_history"], suite=service.suite,
                    redact=service.manager.redactor.data, public_locators=safe["public_locators"])
            directory = service.output_dir
            state_file = directory / "execution-state.json"
            encoded = json.dumps(safe, ensure_ascii=False)
            state_file.write_text(encoded, encoding="utf-8")
            with (directory / "execution-state-history.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
            event = {"run_id": service.run_id, "graph_event_index": safe["graph_event_index"], "node": name,
                     "scene_id": safe.get("scene_id"), "checkpoint_id": safe.get("checkpoint_id"),
                     "action_index": safe.get("action_index", 0),
                     "action_id": safe.get("last_action_id"), "action_count": safe.get("action_count", 0),
                     "context_id": safe.get("context_id"), "next_step": safe.get("next_step"),
                     "execution_status": safe.get("execution_status"), "status": safe.get("execution_status"),
                     "state_file": str(state_file)}
            events.append(event)
            with (directory / "execution-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            (directory / "execution-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
            return safe
        run.__name__ = name
        return bridge.node(run)

    async def select_checkpoint(state, service):
        initialized = state.get("execution_protocol_version") == 1
        initial = {} if initialized else initial_execution_state(service.suite, service.run_id)
        state = {**state, **initial}
        original = service.suite
        materialize_suite(service, state)
        results = dict(state["scene_results"])
        scene_id = state.get("scene_id")
        if scene_id is None:
            scene_id = next((identity for identity in state["scene_order"] if identity not in results), None)
            if scene_id is None:
                execution = service.build_record(results=results, action_count=state["action_count"],
                    planned_suite=materialize_suite(service, state),
                    model_used=bool(strategy or action_planner or repair_controller),
                    repaired=state["repaired"], repair_count=sum(state["repair_attempts"].values()),
                    repair_history=state["repair_history"], initial_failures=state["initial_failures"])
                return {**initial, "execution": execution, "execution_status": execution["status"],
                        "pending_resource": None, "next_step": "done"}
            scene = _scene(original, scene_id)
            blocker = next((identity for identity in scene.depends_on if results[identity]["status"] != "passed"), None)
            if blocker:
                results[scene_id] = {"id": scene_id, "status": "blocked", "blocked_by": blocker,
                    "checkpoints": [{"id": checkpoint.id, "status": "blocked", "blocked_by": blocker} for checkpoint in scene.checkpoints],
                    "events": [], "complete": False}
                return {**initial, "scene_results": results, "scene_id": None,
                        "checkpoint_id": None, "context_id": None, "next_step": "select_checkpoint"}
            return {**initial, "scene_id": scene_id, "checkpoint_id": None, "context_id": None,
                    "checkpoint_results": [], "checkpoint_statuses": {}, "checkpoint_outcome": {},
                    "next_step": "select_checkpoint"}
        scene = _scene(original, scene_id)
        if service.session is None:
            missing = next((identity for identity in sorted(service.external) if identity not in service.values), None)
            if missing is not None:
                pending = {"logical_request_id": uuid.uuid4().hex, "ref": missing, "kind": service.specs_by_id[missing].kind,
                           "scene_id": scene_id, "step_id": "prepare-" + missing,
                           "phase": "prepare", "status": "waiting_user", "run_id": service.run_id}
                service.begin_native_pause()
                return {"pending_resource": pending, "execution_status": "waiting_user", "next_step": "await_resource"}
            session = await service.open_scene(scene)
            return {"context_id": session.context_id, "execution_status": "running", "next_step": "select_checkpoint"}
        statuses = dict(state["checkpoint_statuses"])
        identity = next((identity for identity in state["checkpoint_order"][scene_id] if identity not in statuses), None)
        if identity is None:
            record = service.build_scene_record(service.session, state["checkpoint_results"])
            await service.close_scene()
            results[scene_id] = record
            return {"scene_results": results, "scene_id": None, "checkpoint_id": None, "context_id": None,
                    "pending_resource": None, "next_step": "select_checkpoint"}
        checkpoint = _checkpoint(scene, identity)
        blocker = next((dependency for dependency in checkpoint.depends_on if statuses[dependency] != "passed"), None)
        if blocker:
            outcome = {"id": identity, "status": "blocked", "blocked_by": blocker, "evidence": []}
            statuses[identity] = "blocked"
            return {"checkpoint_id": identity, "checkpoint_outcome": outcome,
                    "checkpoint_statuses": statuses, "checkpoint_results": state["checkpoint_results"] + [outcome],
                    "next_step": "select_checkpoint"}
        return {"checkpoint_id": identity, "checkpoint_outcome": {"id": identity, "status": "running", "evidence": []},
                "action_queue": [], "action_index": 0, "current_action": None, "last_action_id": None,
                "failure": None, "failure_execution": None, "repair_queue": [], "repair_index": 0,
                "repair_active": False, "repair_original": None, "pending_resource": None,
                "repair_bundle": None, "repair_assessment": None, "repair_proposal": None,
                "repair_attempt": 0, "scene_repair_attempt": 0,
                "execution_status": "running", "next_step": "observe_page"}

    async def observe_page(state, service):
        try:
            return {"observation": await service.observe(), "next_step": "plan_actions"}
        except Exception as error:
            return _stop_failure(state, _failure(error))

    async def plan_actions(state, service):
        try:
            effective = materialize_suite(service, state)
            scene = _scene(effective, state["scene_id"])
            checkpoint = _checkpoint(scene, state["checkpoint_id"])
            if action_planner is not None and not any(action.checkpoint_id == checkpoint.id for action in scene.actions):
                if service.clock.active_seconds >= service.suite.budget.max_seconds:
                    raise ValueError("Suite execution budget exhausted")
                generated = await action_planner(scene, checkpoint, state["observation"],
                    remaining=service.suite.budget.max_actions - state["action_count"], redact=service.manager.redactor.data)
                generated = validate_graph_actions(service.suite, generated,
                    redact=service.manager.redactor.data,
                    public_locators=state.get("public_locators", ()) + list(state["observation"].get("candidates", {})))
                if not generated or any(action.checkpoint_id != checkpoint.id for action in generated):
                    raise ValueError("Execution Agent returned an empty or wrong-checkpoint action batch")
                locators = admitted_locators(state, generated, state["observation"])
                actions = {identity: list(batch) for identity, batch in state["effective_actions"].items()}
                actions[scene.id] = [action.model_dump() for action in scene.actions if action.checkpoint_id != checkpoint.id] + [action.model_dump() for action in generated]
                effective = materialize_suite(service, {**state, "effective_actions": actions, "public_locators": locators})
            else:
                actions = state["effective_actions"]
                locators = state.get("public_locators", [])
            scene = _scene(effective, state["scene_id"])
            queue = [action.model_dump() for action in scene.actions if action.checkpoint_id == checkpoint.id]
            key = [scene.id, checkpoint.id]
            planned = state["planned_checkpoints"] + ([] if key in state["planned_checkpoints"] else [key])
            update = {"effective_actions": actions, "effective_suite": effective.model_dump(),
                    "public_locators": locators,
                    "action_queue": queue, "action_index": 0, "planned_checkpoints": planned,
                    "next_step": "execute_action" if queue else "verify_checkpoint"}
            if on_plan_ready is not None:
                value = on_plan_ready({**state, **update}, service)
                if inspect.isawaitable(value):
                    await value
            return update
        except Exception as error:
            return _stop_failure(state, _failure(error))

    async def success(state, service, action, outcome):
        try:
            service.save_actions()
            if action.kind == "fill" and action.resource_id:
                await service.screenshot(service.output_dir / f"{state['scene_id']}-{action.id}-masked.png")
            index = state["action_index"] + 1
            return {"checkpoint_outcome": outcome, "action_index": index, "pending_resource": None,
                    "execution_status": "running", "last_action_id": action.id,
                    "next_step": "execute_action" if index < len(state["action_queue"]) else "verify_checkpoint"}
        except Exception as error:
            return _stop_failure({**state, "checkpoint_outcome": outcome}, _failure(error))

    def operation_failure(state, error, *, repairing=False):
        failure = _failure(error)
        if isinstance(error, ResourceUnavailable):
            update = _stop_failure(state, failure)
            if repairing:
                update["repair_history"] = repair_history(state, execution_status="blocked_by_resource")
            return update
        if repair_controller is None:
            return _stop_failure(state, failure)
        if repairing:
            return {"failure": failure, "repair_active": False, "next_step": "diagnose_failure",
                    "repair_history": repair_history(state, execution_status="error")}
        action = state["current_action"]
        original = {"scenario_id": state["scene_id"], "checkpoint_id": state["checkpoint_id"],
                    "action": action, "status": "error", "error": failure["error"]}
        return {"failure": failure, "repair_original": action, "repair_active": False,
                "initial_failures": state["initial_failures"] + [original], "next_step": "diagnose_failure"}

    async def execute_action(state, service):
        repairing = state["repair_active"]
        action_data = state["repair_queue"][state["repair_index"]] if repairing else state["action_queue"][state["action_index"]]
        action = validate_graph_actions(service.suite, [action_data], redact=service.manager.redactor.data,
            public_locators=state.get("public_locators", ()))[0]
        outcome = deepcopy(state["checkpoint_outcome"])
        if not repairing:
            blocker = next((identity for identity in action.preconditions if state["checkpoint_statuses"][identity] != "passed"), None)
            if blocker:
                outcome.update(status="blocked", blocked_by=blocker)
                return {"checkpoint_outcome": outcome, "next_step": "complete_checkpoint"}
        if (not repairing and state["action_count"] >= service.suite.budget.max_actions) or service.clock.active_seconds >= service.suite.budget.max_seconds:
            message = "Repair exceeds the original active execution budget" if repairing else "Suite execution budget exhausted"
            return _stop_failure(state, _failure(ValueError(message)))
        updates = {"action_count": state["action_count"] + 1, "last_action_id": action.id}
        state = {**state, **updates, "checkpoint_outcome": outcome}
        if not repairing and strategy is not None:
            try:
                scene = _scene(service.suite, state["scene_id"])
                checkpoint = _checkpoint(scene, state["checkpoint_id"])
                current_observation = await service.observe()
                action = await strategy(scene.id, checkpoint, action, current_observation, redact=service.manager.redactor.data)
                action = validate_graph_actions(service.suite, [action], redact=service.manager.redactor.data,
                    public_locators=state.get("public_locators", ()) + list(current_observation.get("candidates", {})))[0]
                updates["public_locators"] = admitted_locators(state, [action], current_observation)
            except Exception as error:
                return {**updates, **_stop_failure(state, _failure(error))}
        updates["current_action"] = action.model_dump()
        state = {**state, **updates}
        if action.kind == "request_resource" and not repairing:
            try:
                metadata = await service.resource_request_metadata(action)
            except Exception as error:
                return {**updates, **_stop_failure(state, _failure(error))}
            pending = {"logical_request_id": uuid.uuid4().hex, "ref": action.resource_id,
                       "kind": service.specs_by_id[action.resource_id].kind, "scene_id": state["scene_id"],
                       "step_id": action.id, "phase": "action", "status": "waiting_user", "run_id": service.run_id,
                       "challenge_id": metadata.get("challenge_id"), "phone_ref": metadata.get("phone_ref"),
                       "expires_at": metadata.get("expires_at")}
            service.begin_native_pause()
            return {**updates, "pending_resource": pending, "execution_status": "waiting_user", "next_step": "await_resource"}
        scene = _scene(service.suite, state["scene_id"])
        checkpoint = _checkpoint(scene, state["checkpoint_id"])

        async def check_conditions():
            await service.check_conditions(checkpoint, outcome)

        try:
            await service.execute_one(action, check_conditions)
        except Exception as error:
            return {**updates, "checkpoint_outcome": outcome, **operation_failure({**state, "checkpoint_outcome": outcome}, error, repairing=repairing)}
        if not repairing:
            return {**updates, **await success(state, service, action, outcome)}
        index = state["repair_index"] + 1
        if index < len(state["repair_queue"]):
            return {**updates, "checkpoint_outcome": outcome, "repair_index": index, "next_step": "execute_action"}
        original = Action.model_validate(state["repair_original"])
        actions = {identity: list(batch) for identity, batch in state["effective_actions"].items()}
        actions[state["scene_id"]] = [replacement for saved in actions[state["scene_id"]]
            for replacement in (state["repair_queue"] if saved["id"] == original.id else [saved])]
        try:
            effective = materialize_suite(service, {**state, "effective_actions": actions})
        except Exception as error:
            return {**updates, **_stop_failure({**state, "checkpoint_outcome": outcome}, _failure(error))}
        committed = {**updates, "repair_active": False, "repair_index": index, "repaired": True,
                "effective_actions": actions, "effective_suite": effective.model_dump(),
                **await success(state, service, original, outcome),
                "repair_history": repair_history(state, execution_status="passed")}
        if on_plan_ready is not None and committed["next_step"] != "complete_checkpoint":
            try:
                value = on_plan_ready({**state, **committed}, service)
                if inspect.isawaitable(value):
                    await value
            except Exception as error:
                # Dispatch already completed. Stop safely if evidence storage
                # fails; never retry the committed browser side effect.
                return {**committed, **_stop_failure({**state, **committed}, _failure(error))}
        return committed

    async def await_resource(state, service):
        service.end_native_pause()
        pending = state["pending_resource"]
        if pending["phase"] == "prepare":
            try:
                await service.prepare_resource(pending["ref"], state["scene_id"])
                return {"pending_resource": None, "execution_status": "running", "next_step": "select_checkpoint"}
            except ResourceUnavailable as error:
                scene = _scene(service.suite, state["scene_id"])
                results = dict(state["scene_results"])
                results[scene.id] = {"id": scene.id, "status": error.status, "complete": False,
                    "checkpoints": [{"id": checkpoint.id, "status": "blocked", "blocked_by": "resource", "evidence": []} for checkpoint in scene.checkpoints],
                    "events": [], "error": str(error), "contexts_created": 0}
                return {"scene_results": results, "scene_id": None, "checkpoint_id": None,
                        "pending_resource": None, "execution_status": "running", "next_step": "select_checkpoint"}
        action = validate_graph_actions(service.suite, [state["current_action"]],
            redact=service.manager.redactor.data, public_locators=state.get("public_locators", ()))[0]
        outcome = deepcopy(state["checkpoint_outcome"])
        checkpoint = _checkpoint(_scene(service.suite, state["scene_id"]), state["checkpoint_id"])

        async def check_conditions():
            await service.check_conditions(checkpoint, outcome)

        try:
            await service.execute_one(action, check_conditions, pending_resource=pending)
        except Exception as error:
            return {"checkpoint_outcome": outcome, "pending_resource": None,
                    **operation_failure({**state, "checkpoint_outcome": outcome}, error)}
        return await success(state, service, action, outcome)

    async def verify_checkpoint(state, service):
        outcome = deepcopy(state["checkpoint_outcome"])
        checkpoint = _checkpoint(_scene(service.suite, state["scene_id"]), state["checkpoint_id"])
        try:
            await service.check_conditions(checkpoint, outcome)
            outcome["status"] = "passed"
            return {"checkpoint_outcome": outcome, "next_step": "complete_checkpoint"}
        except Exception as error:
            return _stop_failure({**state, "checkpoint_outcome": outcome}, _failure(error))

    async def diagnose_failure(state, service):
        original = Action.model_validate(state["repair_original"])
        event = redact_execution_record(service.session.executor.events[-1], service.manager.redactor.data,
            public_ids=state["public_ids"], public_locators=state["public_locators"])
        record = {"scenario_id": state["scene_id"], "checkpoint_id": state["checkpoint_id"],
            "action_id": original.id, "original_action": original.model_dump(), "failed_event": event,
            "decision": "stopped"}
        history = state["repair_history"] + [record]
        def reject(reason):
            history[-1]["reason"] = reason
            return stop_repair(state, reason, history=history)
        if event.get("side_effect_attempted") is not False:
            return reject("External side effect is uncertain; observe/investigate instead of repeating")
        if original.kind not in {"click", "fill", "wait"}:
            return reject("No automatic repair for this operation or resource lifecycle")
        key = state["scene_id"] + ":" + original.id
        attempt = state["repair_attempts"].get(key, 0) + 1
        total = state["scene_repair_attempts"].get(state["scene_id"], 0) + 1
        if attempt > service.suite.budget.max_repairs_per_failure or total > service.suite.budget.max_repairs_per_scenario:
            return reject("Repair budget exhausted")
        if service.clock.active_seconds >= service.suite.budget.max_seconds:
            return reject("Repair exceeds the original active execution budget")
        if len(repair_controller.transport.calls) >= service.suite.budget.max_model_calls:
            return reject("Model call budget exhausted")
        snapshot = await service.observe()
        path = service.output_dir / f"{state['scene_id']}-{original.id}-before-repair-{len(history)}.png"
        await service.screenshot(path)
        checkpoint = {"id": state["checkpoint_id"], "status": "error", "error": state["failure"]["message"],
            "evidence": list(state["checkpoint_outcome"]["evidence"]), "observation": snapshot, "screenshot": str(path)}
        effective = materialize_suite(service, state)
        partial = {"status": "error", "run_id": service.run_id,
            "requirements_fingerprint": effective.requirements_fingerprint(),
            "sanitized_plan": effective.model_dump(), "source_catalog": service.sources,
            "scenarios": {**state["scene_results"], state["scene_id"]: {"status": "error",
                "checkpoints": state["checkpoint_results"] + [checkpoint],
                "events": service.session.executor.events, "network": service.session.network}}}
        try:
            bundle, assessment = await repair_controller.assess(suite=effective, execution=partial,
                                                               redact=service.manager.redactor.data)
            assessment = assessment.model_dump()
            record.update(evidence=bundle, assessment=assessment)
            safe = redact_role_payload({"bundle": bundle, "assessment": assessment}, service.manager.redactor.data,
                                       public_ids=state["public_ids"], public_locators=state["public_locators"])
            if assessment["diagnosis"]["attribution"] != "agent_execution":
                return reject("Diagnosis does not support an operation repair")
        except Exception as error:
            return reject(safe_validation_message(error))
        return {"observation": snapshot, "failure_execution": partial, "repair_bundle": safe["bundle"],
            "repair_assessment": safe["assessment"], "repair_proposal": None,
            "repair_attempt": attempt, "scene_repair_attempt": total,
            "repair_history": history, "next_step": "plan_repair"}

    async def plan_repair(state, service):
        if service.clock.active_seconds >= service.suite.budget.max_seconds:
            return stop_repair(state, "Repair exceeds the original active execution budget")
        if len(repair_controller.transport.calls) >= service.suite.budget.max_model_calls:
            return stop_repair(state, "Model call budget exhausted")
        effective = materialize_suite(service, state)
        scene = _scene(effective, state["scene_id"])
        checkpoint = _checkpoint(scene, state["checkpoint_id"])
        original = Action.model_validate(state["repair_original"])
        key = scene.id + ":" + original.id
        updates = {"repair_attempts": {**state["repair_attempts"], key: state["repair_attempt"]},
                   "scene_repair_attempts": {**state["scene_repair_attempts"], scene.id: state["scene_repair_attempt"]}}
        try:
            plan = await repair_controller.plan(suite=effective, scene=scene, checkpoint=checkpoint,
                original=original, observation=state["observation"],
                failed_event=state["repair_history"][-1]["failed_event"], assessment=state["repair_assessment"],
                bundle=state["repair_bundle"], attempt=state["repair_attempt"], redact=service.manager.redactor.data)
            plan = RepairPlan.model_validate(plan.model_dump())
            proposed = validate_graph_actions(service.suite, plan.proposed_actions,
                redact=service.manager.redactor.data,
                public_locators=state["public_locators"] + list(state["observation"].get("candidates", {})))
            trusted = set(state["public_ids"]) | {effective.requirements_fingerprint(),
                hashlib.sha256(effective.model_dump_json().encode()).hexdigest(),
                *[item["id"] for item in state["repair_bundle"]["evidence"]],
                *[f"{item.id}:{check.id}:{index}" for item in effective.scenarios
                  for check in item.checkpoints for index, _ in enumerate(check.acceptance)]}
            metadata = [plan.scenario_id, plan.action_id, plan.restart_checkpoint, plan.requirements_fingerprint,
                        plan.protected_data_fingerprint, *plan.evidence_ids, *plan.validation_conditions]
            if any(service.manager.redactor.text(value) != value and value not in trusted for value in metadata):
                raise ValueError("Repair metadata contains a private resource value")
        except Exception as error:
            return {**updates, **stop_repair(state, safe_validation_message(error))}
        history = repair_history(state, proposed_plan=plan.model_dump(), attempt=state["repair_attempt"])
        return {**updates, "repair_proposal": plan.model_dump(), "repair_history": history,
            "public_locators": admitted_locators(state, proposed, state["observation"]), "next_step": "validate_repair"}

    async def validate_repair(state, service):
        effective = materialize_suite(service, state)
        scene = _scene(effective, state["scene_id"])
        checkpoint = _checkpoint(scene, state["checkpoint_id"])
        try:
            proposed = repair_controller.validate(RepairPlan.model_validate(state["repair_proposal"]),
                suite=effective, scene=scene, checkpoint=checkpoint,
                original=Action.model_validate(state["repair_original"]), observation=state["observation"],
                failed_event=state["repair_history"][-1]["failed_event"], attempt=state["repair_attempt"],
                scene_attempts=state["scene_repair_attempt"],
                evidence_ids=[item["id"] for item in state["repair_bundle"]["evidence"]])
            proposed = validate_graph_actions(service.suite, proposed, redact=service.manager.redactor.data,
                                             public_locators=state["public_locators"])
            if state["action_count"] + len(proposed) > service.suite.budget.max_actions:
                raise ValueError("Repair exceeds the original suite action budget")
        except Exception as error:
            return stop_repair(state, safe_validation_message(error))
        return {"repair_active": True, "repair_index": 0, "repair_queue": [action.model_dump() for action in proposed],
                "repair_history": repair_history(state, decision="approved"), "next_step": "execute_action"}

    async def complete_checkpoint(state, service):
        checkpoint = _checkpoint(_scene(service.suite, state["scene_id"]), state["checkpoint_id"])
        outcome = deepcopy(state["checkpoint_outcome"])
        await service.capture_checkpoint(checkpoint, outcome)
        statuses = {**state["checkpoint_statuses"], checkpoint.id: outcome["status"]}
        return {"checkpoint_outcome": outcome, "checkpoint_statuses": statuses,
                "checkpoint_results": state["checkpoint_results"] + [outcome],
                "pending_resource": None, "repair_active": False, "execution_status": "running", "next_step": "select_checkpoint"}

    graph = StateGraph(ExecutionState)
    functions = (select_checkpoint, observe_page, plan_actions, execute_action, await_resource,
                 verify_checkpoint, diagnose_failure, plan_repair, validate_repair, complete_checkpoint)
    for name, function in zip(NODES, functions):
        graph.add_node(name, instrument(name, function))
    graph.add_edge(START, "select_checkpoint")
    routes = {
        "select_checkpoint": ("select_checkpoint", "observe_page", "await_resource", "done"),
        "observe_page": ("plan_actions", "complete_checkpoint"),
        "plan_actions": ("execute_action", "verify_checkpoint", "complete_checkpoint"),
        "execute_action": ("execute_action", "await_resource", "verify_checkpoint", "diagnose_failure", "complete_checkpoint"),
        "await_resource": ("select_checkpoint", "execute_action", "verify_checkpoint", "diagnose_failure", "complete_checkpoint"),
        "verify_checkpoint": ("complete_checkpoint",),
        "diagnose_failure": ("plan_repair", "complete_checkpoint"),
        "plan_repair": ("validate_repair", "complete_checkpoint"),
        "validate_repair": ("execute_action", "complete_checkpoint"),
        "complete_checkpoint": ("select_checkpoint",),
    }
    for name, destinations in routes.items():
        graph.add_conditional_edges(name, lambda state: state["next_step"],
                                    {destination: END if destination == "done" else destination for destination in destinations})
    return graph.compile(checkpointer=checkpointer,
                         interrupt_before=["await_resource"] if interrupt_resources else None)
