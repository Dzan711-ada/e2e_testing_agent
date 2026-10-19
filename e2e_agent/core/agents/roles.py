"""The three Agent entry points; each uses its own prompt and decision schema."""
from .decisions import Assessment, DesignDecision, ExecutionDecision
from .prompts import DESIGN, DIAGNOSE, EXECUTE


class DesignAgent:
    def __init__(self, gateway):
        self.gateway = gateway

    async def decide(self, payload):
        return await self.gateway.call('design', DesignDecision, DESIGN, payload)


class ExecutionAgent:
    def __init__(self, gateway):
        self.gateway = gateway

    async def decide(self, payload):
        return await self.gateway.call('execute', ExecutionDecision, EXECUTE, payload)


class DiagnosisAgent:
    def __init__(self, gateway):
        self.gateway = gateway

    async def decide(self, payload):
        return await self.gateway.call('diagnose', Assessment, DIAGNOSE, payload)
