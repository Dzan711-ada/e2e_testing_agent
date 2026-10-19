from copy import deepcopy
import unittest

from e2e_agent.domain import Suite, load_record


def valid_suite():
    return {"id": "suite", "query": "Check login and cart", "rules": [
        {"id": "login", "statement": "Correct credentials allow login", "source": {"kind": "contract", "reference": "shop.login.v1"}},
        {"id": "cart", "statement": "Adding one book yields one book", "source": {"kind": "user", "reference": "initial request"}}],
        "scenarios": [{"id": "happy", "description": "Login and cart", "start_url": "http://127.0.0.1/shop/run/login", "namespace": "run",
                       "checkpoints": [{"id": "login", "description": "Logged in", "acceptance": [{"rule_id": "login", "target": "identity", "operator": "visible", "expected": True}]},
                                       {"id": "cart", "description": "Book quantity", "depends_on": ["login"], "acceptance": [{"rule_id": "cart", "target": "quantity", "operator": "equals", "expected": "1"}]}]}]}


class DomainTests(unittest.TestCase):
    def test_complete_sourced_plan_has_stable_fingerprint(self):
        data = valid_suite()
        first = Suite.model_validate(data)
        second = Suite.model_validate_json(first.model_dump_json())
        self.assertEqual(first.requirements_fingerprint(), second.requirements_fingerprint())
        data["scenarios"][0]["checkpoints"][1]["acceptance"][0]["expected"] = "2"
        self.assertNotEqual(first.requirements_fingerprint(), Suite.model_validate(data).requirements_fingerprint())

    def test_missing_acceptance_and_observed_only_source_rejected(self):
        for change in ("no_acceptance", "no_source", "page_is_source", "unknown_rule"):
            data = valid_suite()
            if change == "no_acceptance":
                data["scenarios"][0]["checkpoints"][0]["acceptance"] = []
            elif change == "no_source":
                del data["rules"][0]["source"]
            elif change == "page_is_source":
                data["rules"][0]["source"]["kind"] = "observation"
            else:
                data["scenarios"][0]["checkpoints"][0]["acceptance"][0]["rule_id"] = "invented"
            with self.subTest(change=change), self.assertRaises(ValueError):
                Suite.model_validate(data)

    def test_dangling_and_cyclic_checkpoint_dependencies_rejected(self):
        for dependencies in (["absent"], ["cart"]):
            data = valid_suite()
            data["scenarios"][0]["checkpoints"][0]["depends_on"] = dependencies
            with self.subTest(dependencies=dependencies), self.assertRaises(ValueError):
                Suite.model_validate(data)

    def test_scene_dependency_cycles_and_duplicate_ids_rejected(self):
        for mode in ("dangling", "cycle", "duplicate"):
            data = valid_suite()
            other = deepcopy(data["scenarios"][0])
            other["id"] = "other" if mode != "duplicate" else "happy"
            data["scenarios"].append(other)
            data["scenarios"][0]["depends_on"] = ["absent" if mode == "dangling" else "other"]
            if mode == "cycle":
                other["depends_on"] = ["happy"]
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                Suite.model_validate(data)

    def test_uncovered_required_rule_rejected(self):
        data = valid_suite()
        data["rules"].append({"id": "logout", "statement": "Logout clears session", "source": {"kind": "contract", "reference": "logout"}})
        with self.assertRaises(ValueError):
            Suite.model_validate(data)

    def test_unknown_resource_and_action_budget_rejected(self):
        data = valid_suite()
        action = {"id": "fill", "kind": "fill", "checkpoint_id": "login", "intent": "Enter account", "target": "label:Username", "resource_id": "missing"}
        data["scenarios"][0]["actions"] = [action]
        with self.assertRaises(ValueError):
            Suite.model_validate(data)
        del action["resource_id"]
        action["value"] = "fictional-user"
        data["scenarios"][0]["actions"] += [{**action, "id": "fill2"}]
        data["budget"] = {"max_actions": 1}
        with self.assertRaises(ValueError):
            Suite.model_validate(data)

    def test_legacy_records_retained_without_fabricated_new_schema(self):
        legacy = {"actions": [], "events": [], "status": "failed", "query": "historical"}
        self.assertEqual(load_record(legacy), legacy)
        self.assertNotIn("schema_version", load_record(legacy))
        with self.assertRaises(ValueError):
            load_record({"schema_version": 99})
