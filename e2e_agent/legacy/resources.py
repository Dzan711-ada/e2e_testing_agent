"""Volatile resources, single-owner console input and explicit human-wait accounting."""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import getpass
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Protocol
from urllib.parse import quote, quote_plus
import uuid


class ResourceUnavailable(RuntimeError):
    status = "blocked"


class ResourceCancelled(ResourceUnavailable):
    status = "cancelled"


class ResourceExpired(ResourceUnavailable):
    pass


class ActiveClock:
    def __init__(self):
        self.started, self.wait_seconds, self.wait_started = time.monotonic(), 0.0, None

    @property
    def active_seconds(self):
        current = self.wait_started if self.wait_started is not None else time.monotonic()
        return current - self.started - self.wait_seconds

    @contextmanager
    def waiting(self):
        if self.wait_started is not None:
            raise RuntimeError("Only one resource owner can wait at a time")
        self.wait_started = time.monotonic()
        try:
            yield
        finally:
            self.wait_seconds += time.monotonic() - self.wait_started
            self.wait_started = None


@dataclass(frozen=True)
class ResourceRequest:
    id: str
    ref: str
    kind: str
    purpose: str
    scene_id: str
    step_id: str
    run_id: str
    sensitive: bool = True
    reusable: bool = False
    wait_timeout: float = 120
    challenge_id: str | None = None
    phone_ref: str | None = None
    expires_at: float | None = None
    public_fictional: bool = False
    delivery_hint: str | None = field(default=None, repr=False)

    def audit(self):
        return {key: getattr(self, key) for key in (
            "id", "ref", "kind", "purpose", "scene_id", "step_id", "run_id", "sensitive", "reusable", "wait_timeout",
            "challenge_id", "phone_ref", "expires_at", "public_fictional")}


class ResourceProvider(Protocol):
    async def acquire(self, request: ResourceRequest) -> str: ...


class ScriptedProvider:
    def __init__(self, values=None, *, delay=0, use_test_delivery=False):
        self.values, self.delay, self.use_test_delivery = dict(values or {}), delay, use_test_delivery
        self.requests = []

    async def acquire(self, request):
        self.requests.append(request.audit())
        delay = self.delay(request) if callable(self.delay) else self.delay
        if delay:
            await asyncio.sleep(delay)
        if request.ref in self.values:
            value = self.values[request.ref]
            if isinstance(value, list):
                if not value:
                    raise ResourceUnavailable("Scripted resources exhausted")
                value = value.pop(0)
            if callable(value):
                value = value(request)
            if isinstance(value, Exception):
                raise value
            return str(value)
        if request.kind == "sms_code" and self.use_test_delivery and request.delivery_hint:
            return request.delivery_hint
        raise ResourceUnavailable("No resource supplied for reference " + request.ref)


class ConsoleProvider:
    """A timed-out daemon reader is never followed by a second competing stdin reader."""
    def __init__(self, *, allow_test_delivery=False):
        self.allow_test_delivery, self.closed = allow_test_delivery, False
        self.lock = asyncio.Lock()
        self.requests = []

    async def acquire(self, request):
        async with self.lock:
            event = {**request.audit(), "status": "waiting_user"}
            self.requests.append(event)
            available = sys.stdin is not None and not sys.stdin.closed and (sys.stdin.isatty() or os.environ.get("PYCHARM_HOSTED") == "1")
            if self.closed or not available:
                event["status"] = "blocked"
                raise ResourceUnavailable("No interactive console input channel")
            if request.sensitive and not request.public_fictional and not sys.stdin.isatty():
                event["status"] = "blocked"
                raise ResourceUnavailable("Private input requires a console with hidden input support")
            if request.delivery_hint and self.allow_test_delivery and request.public_fictional:
                print("本地模拟短信测试通道：" + request.delivery_hint, flush=True)
            print(f"需要 {request.kind}，用途：{request.purpose}。直接回车发送；输入 :cancel 取消。", flush=True)
            replies = queue.Queue(maxsize=1)

            def read():
                try:
                    reader = getpass.getpass if request.sensitive and not request.public_fictional else input
                    replies.put((True, reader("资源输入：")))
                except BaseException as error:
                    replies.put((False, error))

            thread = threading.Thread(target=read, name="single-console-resource", daemon=True)
            thread.start()
            deadline = time.monotonic() + request.wait_timeout
            try:
                while time.monotonic() < deadline:
                    try:
                        success, value = replies.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.05)
                        continue
                    if not success:
                        self.closed = True
                        event["status"] = "blocked"
                        raise ResourceUnavailable("Console EOF or input channel closed")
                    if value.strip() == ":cancel":
                        self.closed = True
                        event["status"] = "cancelled"
                        raise ResourceCancelled("User cancelled resource input")
                    event["status"] = "provided"
                    return value.strip()
                self.closed = True
                event["status"] = "blocked"
                raise ResourceUnavailable("Human resource wait deadline exceeded")
            except asyncio.CancelledError:
                self.closed = True
                event["status"] = "cancelled"
                raise


class Redactor:
    def __init__(self):
        self._values = set()
        self._variants = {}
        self._replacements = ()

    def register(self, value):
        if value:
            value = str(value)
            if value in self._values:
                return
            self._values.add(value)
            self._variants[value] = frozenset({value, quote(value, safe=""), quote_plus(value), repr(value)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value, ensure_ascii=True)[1:-1]})
            # Encoding is invariant for a registered value. Keep this private
            # volatile cache off the high-volume per-string redaction path.
            self._replacements = tuple(variant
                for secret in sorted(self._values, key=lambda item: (-len(item), item))
                for variant in sorted(self._variants[secret], key=lambda item: (-len(item), item)))

    def text(self, value):
        for variant in self._replacements:
            value = value.replace(variant, "[RESOURCE]")
        return value

    def data(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(key) if isinstance(key, str) else key: self.data(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.data(item) for item in value]
        return value

    def masks(self, page):
        return [page.locator("input, textarea, [data-testid='identity']"),
                *[page.get_by_text(value, exact=False) for value in self._values]]

    def clear(self):
        self._values.clear()
        self._variants.clear()
        self._replacements = ()


class ResourceManager:
    def __init__(self, specs, run_id, provider, clock, audit_dir: Path, *, wait_timeout=120, public_fictional=False):
        self.specs = {s.id: s for s in specs}
        self.run_id, self.provider, self.clock = run_id, provider, clock
        self.wait_timeout, self.public_fictional = wait_timeout, public_fictional
        self.current_challenge = None
        self.redactor, self._cache, self.events = Redactor(), {}, []
        self.audit_path = audit_dir / "resource-events.json"

    def __contains__(self, identity):
        return identity in self.specs

    def __getitem__(self, identity):
        return self.resolve(identity)

    def bind_static(self, values):
        for identity, value in values.items():
            if identity in self.specs:
                self.redactor.register(value)
                if self.specs[identity].kind == "sms_code":
                    continue  # An OTP is never preloaded across execution/challenge boundaries.
                self._cache[identity] = {"value": str(value), "run_id": self.run_id, "reusable": True,
                                         "consumed": False, "challenge_id": None, "expires_at": None, "binding_id": uuid.uuid4().hex}

    def fill_proof(self, identity, actual):
        item = self._cache.get(identity)
        if item is None or item["run_id"] != self.run_id or actual != item["value"]:
            raise ResourceUnavailable("Filled input does not match the current resource reference")
        return {"ref": identity, "binding_id": item["binding_id"], "run_id": self.run_id, "input_matches": True}

    def save(self):
        # Audit fields are owned public protocol IDs/enums. A resource value
        # spelled "password" must not rename its own resource reference.
        events = [{key: self.redactor.data(value) if key in {"purpose", "error"} else value
                   for key, value in event.items()} for event in self.events]
        self.audit_path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    def resolve(self, identity, *, consume=True, challenge_id=None):
        item = self._cache.get(identity)
        if item is None or item["run_id"] != self.run_id:
            raise ResourceUnavailable("Resource requires a request: " + identity)
        if self.specs[identity].kind == "sms_code" and item["challenge_id"] != self.current_challenge:
            raise ResourceExpired("Resource is not bound to the current browser challenge")
        if challenge_id is not None and item["challenge_id"] != challenge_id:
            raise ResourceExpired("Resource belongs to another challenge")
        if item["expires_at"] is not None and time.time() >= item["expires_at"]:
            raise ResourceExpired("Resource has expired")
        if item["consumed"] and not item["reusable"]:
            raise ResourceExpired("One-time resource was already consumed")
        if consume:
            item["consumed"] = True
        return item["value"]

    async def request(self, identity, scene_id, step_id, purpose, *, challenge_id=None, phone_ref=None, expires_at=None, delivery_hint=None):
        if identity not in self.specs:
            raise ResourceUnavailable("Unknown resource reference")
        spec = self.specs[identity]
        if spec.kind == "sms_code" and (not challenge_id or not phone_ref):
            raise ResourceUnavailable("SMS resource requires a current challenge and phone reference")
        if spec.kind == "sms_code":
            self.current_challenge = challenge_id
        if identity in self._cache and self._cache[identity]["reusable"]:
            return self.resolve(identity, consume=False, challenge_id=challenge_id)
        if identity in self._cache and self._cache[identity]["challenge_id"] == challenge_id:
            raise ResourceExpired("A one-time resource cannot be requested again for the same challenge")
        request = ResourceRequest(uuid.uuid4().hex, identity, spec.kind, purpose, scene_id, step_id, self.run_id,
                                  True, spec.reusable, self.wait_timeout, challenge_id, phone_ref, expires_at,
                                  self.public_fictional, delivery_hint)
        event = {**request.audit(), "status": "waiting_user"}
        self.events.append(event)
        self.save()
        try:
            if expires_at is not None and time.time() >= expires_at:
                raise ResourceExpired("Challenge expired before resource input")
            effective_wait = min(request.wait_timeout, expires_at - time.time()) if expires_at is not None else request.wait_timeout
            with self.clock.waiting():
                try:
                    value = await asyncio.wait_for(self.provider.acquire(request), effective_wait)
                except asyncio.TimeoutError as error:
                    if expires_at is not None and time.time() >= expires_at:
                        raise ResourceExpired("Challenge expired while waiting for the resource") from error
                    raise ResourceUnavailable("Human resource wait deadline exceeded") from error
            self.redactor.register(value)
            patterns = {"phone": r"1\d{10}", "sms_code": r"\d{6}", "account": r".{1,128}", "password": r".{1,512}"}
            if not re.fullmatch(patterns[spec.kind], value):
                raise ResourceUnavailable("Resource format is invalid")
            if expires_at is not None and time.time() >= expires_at:
                raise ResourceExpired("Challenge expired while waiting for the resource")
            self._cache[identity] = {"value": value, "run_id": self.run_id, "reusable": spec.reusable, "consumed": False,
                                     "challenge_id": challenge_id, "expires_at": expires_at, "binding_id": uuid.uuid4().hex}
            event["status"] = "provided"
            return value
        except ResourceUnavailable as error:
            event.update(status=error.status, error=self.redactor.text(str(error)))
            raise
        except asyncio.CancelledError:
            event.update(status="cancelled", error="Run interrupted while awaiting a resource")
            raise
        finally:
            self.save()

    def close(self):
        self._cache.clear()
        self.redactor.clear()
