"""Frozen stage-2 fixture plan; this is an oracle, never a substitute for a model test."""
from .domain import Suite


def shop_suite():
    def rule(identity, statement):
        return {"id": identity, "statement": statement, "source": {"kind": "contract", "reference": "sample_shop.CONTRACT.v1"}}

    def check(identity, target, expected, dependency=(), operator="equals"):
        return {"id": identity, "description": identity, "depends_on": dependency,
                "acceptance": [{"rule_id": identity, "target": target, "operator": operator, "expected": expected}]}

    def action(identity, checkpoint, kind, target=None, value=None, resource=None):
        return {"id": identity, "checkpoint_id": checkpoint, "kind": kind, "target": target, "value": value,
                "resource_id": resource, "intent": identity}

    login = [action("open-login", "login", "navigate", value="login"),
             action("username", "login", "fill", "label:Username", resource="username"),
             action("password", "login", "fill", "label:Password", resource="password"),
             action("submit-login", "login", "click", "testid:login-submit")]
    registration = [action("open-register", "registration", "navigate", value="register"),
                    action("new-username", "registration", "fill", "label:Username", resource="username"),
                    action("new-password", "registration", "fill", "label:Password", resource="password"),
                    action("register", "registration", "click", "testid:register-submit")]
    scenarios = [{"id": "main-chain", "description": "Register, log in and add a book in the same context", "namespace": "main",
                  "start_url": "{base}/shop/{namespace}", "checkpoints": [
                      check("registration", "testid:message", "Registration successful"),
                      check("login", "testid:identity", "Signed in: stage_user", ["registration"]),
                      check("cart", "testid:quantity", "1", ["login"]),
                      check("refresh", "testid:quantity", "1", ["cart"])],
                  "actions": registration + login + [action("add-book", "cart", "click", "button:Add to cart"),
                                                       action("refresh-cart", "refresh", "navigate", value="cart")]},
                 {"id": "wrong-password", "description": "Reject an incorrect password in an isolated context", "namespace": "negative",
                  "initial_state": "registered_account", "start_url": "{base}/shop/{namespace}",
                  "checkpoints": [check("rejection", "testid:message", "Invalid credentials")],
                  "actions": [{**a, "checkpoint_id": "rejection", "resource_id": "wrong_password" if a["id"] == "password" else a["resource_id"]} for a in login]},
                 {"id": "unauthenticated", "description": "An independent context cannot inherit the main login", "namespace": "guest",
                  "start_url": "{base}/shop/{namespace}", "checkpoints": [check("authentication", "testid:message", "Log in to view the cart")],
                  "actions": [action("open-cart", "authentication", "navigate", value="cart")]}]
    return Suite.model_validate({"id": "shop-suite", "query": "Register, test login and cart, wrong password and unauthenticated access",
        "fixture": "local_shop", "rules": [rule("registration", "Registration creates an account without login"),
            rule("login", "Correct password authenticates the user"), rule("cart", "Adding one book yields quantity 1"),
            rule("refresh", "Cart survives refresh"), rule("rejection", "Wrong password is rejected"), rule("authentication", "Anonymous cart access is rejected")],
        "scenarios": scenarios, "resources": [{"id": identity, "kind": "account" if identity == "username" else "password", "run_id": "per-execution"} for identity in ("username", "password", "wrong_password")]})
