"""Fictional resource input for current workflow tests; never part of the runtime."""
import asyncio

from e2e_agent.core.resources.interaction import ResourceUnavailable


class ScriptedProvider:
    def __init__(self, values=None, *, delay=0):
        self.values, self.delay = dict(values or {}), delay
        self.requests = []

    async def acquire(self, request):
        self.requests.append(request.audit())
        delay = self.delay(request) if callable(self.delay) else self.delay
        if delay:
            await asyncio.sleep(delay)
        if request.ref not in self.values:
            raise ResourceUnavailable('No resource supplied for reference ' + request.ref)
        value = self.values[request.ref]
        if isinstance(value, list):
            if not value:
                raise ResourceUnavailable('Scripted resources exhausted')
            value = value.pop(0)
        if callable(value):
            value = value(request)
        if isinstance(value, Exception):
            raise value
        return str(value)
