"""Observed candidates and a deterministic, bounded Playwright action compiler."""
import re
import time
from urllib.parse import urljoin, urlsplit

from playwright.async_api import expect

from .domain import Action
from .resources import ResourceExpired, ResourceUnavailable


class ActionError(ValueError):
    pass


def locator_expression(target):
    """Only these locator forms can be emitted; selectors and Python are never accepted."""
    if not isinstance(target, str) or ":" not in target:
        raise ActionError("An observed locator reference is required")
    kind, value = target.split(":", 1)
    if not value:
        raise ActionError("Empty locator reference")
    if kind == "testid":
        return f"page.get_by_test_id({value!r})"
    if kind == "label":
        return f"page.get_by_label({value!r}, exact=True)"
    if kind == "text":
        return f"page.get_by_text({value!r}, exact=True)"
    if kind in {"button", "link"}:
        # observe() owns these references by textContent.trim(), which can
        # differ from the accessible name when CSS adds a decorative icon.
        # Keep the observed role and exact DOM text; never use substring/nth.
        pattern = r"^\s*" + re.escape(value) + r"\s*$"
        return f"page.get_by_role({kind!r}).filter(has_text=re.compile({pattern!r}))"
    raise ActionError("Unsupported locator type")


def compile_action(action: Action, url):
    """Return ordinary Playwright Python generated solely from the validated action schema."""
    if action.kind in {"fill", "select"} and action.value is not None and (action.field_role is not None or any(word in (action.target or "").lower() for word in ("password", "phone", "code", "username"))):
        raise ActionError("Sensitive business inputs must use resource references")
    if action.kind == "navigate":
        return f"await page.goto({url!r}, wait_until='domcontentloaded')"
    if action.kind == "observe":
        return "observation = await observe(page)"
    if action.kind == "request_resource":
        return f"await request_resource({action.resource_id!r})"
    locator = locator_expression(action.target)
    if action.kind == "wait":
        return f"await {locator}.wait_for(state='visible')\nawait unique({locator})"
    if action.kind == "check":
        return "await check_conditions()"
    value = f"resource({action.resource_id!r})" if action.resource_id else repr(action.value)
    operations = {"fill": f".fill({value})", "click": ".click()", "select": f".select_option({value})",
                  "wait": ".wait_for(state='visible')"}
    if action.kind not in operations:
        raise ActionError("Unsupported action")
    guard = f"await guard_target({locator})\n" if action.kind == "click" else ""
    dispatch = "await mark_dispatch()\n" if action.kind in {"click", "fill", "select"} else ""
    return f"await unique({locator})\n" + guard + dispatch + f"await {locator}{operations[action.kind]}"


async def observe(page):
    """No input values or HTML/network payloads are collected."""
    elements = await page.locator('[data-testid],input,button,select,a,[role]').evaluate_all('''elements => elements.map(e => ({
        testid:e.getAttribute('data-testid'), label:e.labels && e.labels.length ? e.labels[0].textContent.trim() : null,
        text:['BUTTON','A','ARTICLE','H1','H2','H3'].includes(e.tagName) ? e.textContent.trim() : null,
        tag:e.tagName.toLowerCase(), type:e.getAttribute('type') || '', role:e.getAttribute('role') || '',
        form_testid:e.form ? e.form.getAttribute('data-testid') : null
    }))''')
    candidates = {}
    for item in elements:
        refs = []
        if item["testid"]:
            refs.append("testid:" + item["testid"])
        if item["label"]:
            refs.append("label:" + item["label"])
        if item["text"] and item["tag"] in {"button", "a"}:
            refs.append(("button:" if item["tag"] == "button" else "link:") + item["text"])
        for ref in refs:
            candidates.setdefault(ref, []).append(item)
    return {"url": page.url, "candidates": candidates}


class ActionExecutor:
    def __init__(self, page, start_url, resources, budget, *, clock=None, scene_id=None, fixture_store=None,
                 url_policy=None):
        self.page, self.start_url, self.resources, self.budget = page, start_url, resources, budget
        self.started, self.count = time.monotonic(), 0
        self.events = []
        self.clock, self.scene_id, self.fixture_store = clock, scene_id, fixture_store
        self._pending_resource = None
        self.url_policy = url_policy

    async def resource_metadata(self, identity, *, include_delivery=False):
        if not hasattr(self.resources, "request"):
            raise ResourceUnavailable("Resource provider is unavailable")
        if identity not in self.resources.specs:
            raise ResourceUnavailable("Unknown resource reference")
        metadata = {}
        if self.resources.specs[identity].kind == "sms_code":
            challenge = await self.page.get_by_test_id("challenge").input_value()
            phones = [r.id for r in self.resources.specs.values() if r.kind == "phone"]
            if not challenge or len(phones) != 1:
                raise ResourceUnavailable("A current SMS challenge and phone reference are required")
            metadata.update(challenge_id=challenge, phone_ref=phones[0])
            if self.fixture_store:
                if include_delivery:
                    metadata["delivery_hint"] = self.fixture_store.test_code(challenge)
                with self.fixture_store.connection() as db:
                    row = db.execute("SELECT expires FROM challenges WHERE id=?", (challenge,)).fetchone()
                if row is None:
                    raise ResourceUnavailable("Current browser challenge is unavailable")
                metadata["expires_at"] = row[0]
        return metadata

    async def request_resource(self, identity):
        metadata = await self.resource_metadata(identity, include_delivery=True)
        pending = self._pending_resource
        if pending is not None:
            expected = (self.resources.run_id, self.scene_id, self._active_action.id, identity)
            actual = tuple(pending.get(key) for key in ("run_id", "scene_id", "step_id", "ref"))
            if actual != expected:
                raise ResourceExpired("Pending resource belongs to another run, scene or action")
            if self.resources.specs[identity].kind == "sms_code":
                if any(pending.get(key) != metadata.get(key) for key in ("challenge_id", "phone_ref", "expires_at")):
                    raise ResourceExpired("Pending resource does not match the original browser challenge")
                if pending.get("expires_at") is not None and time.time() >= pending["expires_at"]:
                    raise ResourceExpired("Challenge expired before resource input")
        await self.resources.request(identity, self.scene_id, self._active_action.id, self._active_action.intent, **metadata)

    def safe_url(self, value):
        url = urljoin(self.start_url.rstrip('/') + '/', str(value).lstrip('/')) if not str(value).startswith(('http:', 'https:')) else str(value)
        allowed, actual = urlsplit(self.start_url), urlsplit(url)
        if actual.scheme not in {"http", "https"} or (actual.scheme, actual.netloc) != (allowed.scheme, allowed.netloc):
            raise ActionError("Navigation outside the allowed origin")
        prefix = allowed.path.rstrip('/')
        if prefix and actual.path != prefix and not actual.path.startswith(prefix + '/'):
            raise ActionError("Navigation outside the allowed path")
        if actual.username or actual.password or any(part in {"..", "."} for part in actual.path.split('/')):
            raise ActionError("Invalid navigation URL")
        if self.url_policy is not None:
            self.url_policy(url)
        return url

    async def unique(self, locator):
        count = await locator.count()
        if count != 1:
            raise ActionError(f"Expected exactly one observed target; found {count}")

    async def guard_target(self, locator):
        destination = await locator.evaluate("e => e.getAttribute('href') || (e.form ? e.form.action : null)")
        if destination:
            self.safe_url(destination)

    def resource(self, identity):
        if identity not in self.resources:
            raise ActionError(f"Resource unavailable: {identity}")
        return self.resources[identity]

    async def execute(self, action: Action, check_conditions, *, pending_resource=None):
        event = {"action_id": action.id, "checkpoint_id": action.checkpoint_id, "kind": action.kind,
                 "target": action.target, "resource_id": action.resource_id, "source": None, "status": "running",
                 "side_effect_attempted": False}
        self.events.append(event)
        self._pending_resource = pending_resource
        try:
            return await self._execute(action, check_conditions, event)
        except Exception as error:
            event.update(status=getattr(error, "status", "error"), url=self.page.url, error=f"{type(error).__name__}: {error}")
            raise
        finally:
            self._pending_resource = None

    async def _execute(self, action, check_conditions, event):
        elapsed = self.clock.active_seconds if self.clock else time.monotonic() - self.started
        if self.count >= self.budget.max_actions or elapsed >= self.budget.max_seconds:
            raise ActionError("Execution budget exhausted")
        self.count += 1
        self._active_action = action
        if hasattr(self.resources, "current_challenge") and await self.page.get_by_test_id("challenge").count() == 1:
            self.resources.current_challenge = await self.page.get_by_test_id("challenge").input_value()
        snapshot = await observe(self.page)
        if action.kind not in {"navigate", "observe", "request_resource", "wait"}:
            if action.target not in snapshot["candidates"]:
                raise ActionError("Target is absent from the current observation")
        url = self.safe_url(action.value) if action.kind == "navigate" else self.page.url
        source = compile_action(action, url)
        event["source"] = source
        # The compiler emits only owned templates, never model-provided Python.
        import ast
        code = compile(source, "<structured-action>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        namespace = {"page": self.page, "re": re, "resource": self.resource, "unique": self.unique,
                     "guard_target": self.guard_target, "observe": observe, "check_conditions": check_conditions,
                     "request_resource": self.request_resource, "mark_dispatch": self.mark_dispatch}
        try:
            await eval(code, namespace)
            if action.kind == "fill" and action.resource_id and hasattr(self.resources, "fill_proof"):
                locator = eval(locator_expression(action.target), {"page": self.page, "re": re})
                event["resource_binding"] = self.resources.fill_proof(action.resource_id, await locator.input_value())
            self.safe_url(self.page.url)
            event.update(status="passed", url=self.page.url)
        except Exception as error:
            event.update(status=getattr(error, "status", "error"), url=self.page.url, error=f"{type(error).__name__}: {error}")
            raise
        return event

    async def mark_dispatch(self):
        # Mark before invoking Playwright. A later timeout cannot prove the
        # browser/server did not commit the side effect, so retries must stop.
        self.events[-1]["side_effect_attempted"] = True

    async def check(self, condition):
        if condition.operator == "url":
            await expect(self.page).to_have_url(self.safe_url(condition.expected), timeout=1500)
            return self.page.url
        snapshot = await observe(self.page)
        locator = eval(locator_expression(condition.target), {"page": self.page, "re": re})
        if condition.operator == "count":
            await expect(locator).to_have_count(int(condition.expected), timeout=1500)
            return await locator.count()
        if condition.operator == "hidden":
            await expect(locator).to_be_hidden(timeout=1500)
            return "hidden"
        if condition.target not in snapshot["candidates"]:
            raise AssertionError("Required acceptance target is absent: " + condition.target)
        await self.unique(locator)
        if condition.operator == "visible":
            await expect(locator).to_be_visible(timeout=1500)
            return "visible"
        await expect(locator).to_have_text(str(condition.expected), timeout=1500)
        return await locator.text_content()
