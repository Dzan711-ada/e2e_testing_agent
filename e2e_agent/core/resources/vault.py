"""String resources with an open identity, a volatile value, and a public audit."""
import asyncio
import uuid
from ..contracts import ResourceSpec
from .interaction import ActiveClock, ConsoleProvider, Redactor, ResourceRequest, ResourceUnavailable


class ResourceVault:
    def __init__(self, provider=None, *, wait_timeout=120):
        self.provider = provider or ConsoleProvider()
        self.wait_timeout = wait_timeout
        self.run_id = uuid.uuid4().hex
        self.redactor, self.clock = Redactor(), ActiveClock()
        self.specs, self._values, self._consumed, self.events = {}, {}, set(), []

    def declare(self, spec: ResourceSpec):
        if spec.id in self.specs and self.specs[spec.id] != spec:
            raise ValueError('A resource declaration cannot silently change')
        self.specs[spec.id] = spec
        self.redactor.public_ids.add(spec.id)

    def register_plan(self, plan):
        self.redactor.public_ids.update([r.id for r in plan.requirements] + [r.source for r in plan.requirements])
        for scenario in plan.scenarios:
            self.redactor.public_ids.add(scenario.id)
            for checkpoint in scenario.checkpoints:
                self.redactor.public_ids.add(checkpoint.id)
                self.redactor.public_ids.update(a.id for a in checkpoint.assertions)
        for spec in plan.resources:
            self.declare(spec)

    async def acquire(self, spec: ResourceSpec, scene_id, step_id):
        self.declare(spec)
        key = (scene_id, spec.id)
        if spec.reusable and key in self._values:
            return
        request = ResourceRequest(uuid.uuid4().hex, spec.id, spec.label, spec.purpose,
            scene_id, step_id, self.run_id, sensitive=spec.sensitive,
            reusable=spec.reusable, wait_timeout=self.wait_timeout)
        event = {'resource_id': spec.id, 'scene_id': scene_id, 'step_id': step_id, 'status': 'waiting_user'}
        self.events.append(event)
        try:
            with self.clock.waiting():
                value = await asyncio.wait_for(self.provider.acquire(request), timeout=self.wait_timeout)
            if not isinstance(value, str):
                raise ResourceUnavailable('A string resource is required')
            self.redactor.register(value)
            if not value.strip() or len(value) > spec.max_length:
                raise ResourceUnavailable('Resource is empty or exceeds its declared length')
            self._values[key] = value
            self._consumed.discard(key)
            event['status'] = 'provided'
        except asyncio.TimeoutError as error:
            event['status'] = 'blocked'
            raise ResourceUnavailable('Human resource wait deadline exceeded') from error
        except asyncio.CancelledError:
            event['status'] = 'cancelled'
            raise
        except ResourceUnavailable as error:
            event['status'] = error.status
            raise

    def has(self, identity, scene_id):
        return (scene_id, identity) in self._values and (scene_id, identity) not in self._consumed

    def resolve(self, identity, scene_id):
        key = (scene_id, identity)
        if not self.has(identity, scene_id):
            raise ResourceUnavailable('Resource has not been supplied: ' + identity)
        value = self._values[key]
        if not self.specs[identity].reusable:
            self._consumed.add(key)
        return value

    def expected_value(self, identity, scene_id):
        if (scene_id, identity) not in self._values:
            raise ResourceUnavailable('Expected resource has not been supplied')
        return self._values[(scene_id, identity)]

    def clear_scene(self, scene_id):
        self._values = {key: value for key, value in self._values.items() if key[0] != scene_id}
        self._consumed = {key for key in self._consumed if key[0] != scene_id}

    def close(self):
        self._values.clear()
        self._consumed.clear()
        self.redactor.clear()
