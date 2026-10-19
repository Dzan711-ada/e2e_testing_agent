"""Authenticated loopback resource proxy; only the parent console owns stdin."""
import asyncio
import json
import os
import secrets

from .resources import Redactor, ResourceCancelled, ResourceRequest, ResourceUnavailable


class ResourceBroker:
    def __init__(self, provider, launch_id, specs, *, max_wait=120):
        self.provider, self.launch_id, self.max_wait = provider, launch_id, max_wait
        self.specs = {s.id: s.kind for s in specs}
        self.token, self.pending, self.events, self.tasks = secrets.token_hex(32), 0, [], set()
        self.redactor = Redactor()

    @property
    def waiting(self):
        return self.pending > 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=32768)
        port = self.server.sockets[0].getsockname()[1]
        self.environment = {"E2E_RESOURCE_BRIDGE_ADDRESS": f"127.0.0.1:{port}", "E2E_RESOURCE_BRIDGE_TOKEN": self.token,
                            "E2E_RESOURCE_BRIDGE_RUN_ID": self.launch_id}
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.redactor.clear()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        event = None
        try:
            payload = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if not secrets.compare_digest(str(payload.get("token", "")), self.token) or payload.get("launch_id") != self.launch_id:
                raise ResourceUnavailable("Resource bridge authentication failed")
            request = ResourceRequest(**payload["request"])
            if self.specs.get(request.ref) != request.kind or not 0 < request.wait_timeout <= self.max_wait or len(self.events) >= 64:
                raise ResourceUnavailable("Resource request is outside the exported contract")
            event = {**request.audit(), "status": "waiting_user"}
            self.events.append(event)
            self.pending += 1
            try:
                value = await asyncio.wait_for(self.provider.acquire(request), request.wait_timeout)
                self.redactor.register(value)
                event["status"] = "provided"
                response = {"status": "provided", "value": value}
            finally:
                self.pending -= 1
        except ResourceCancelled:
            response = {"status": "cancelled", "error": "User cancelled the resource request"}
            if event:
                event["status"] = "cancelled"
        except asyncio.CancelledError:
            if event:
                event["status"] = "cancelled"
            raise
        except Exception:
            response = {"status": "blocked", "error": "Resource unavailable or wait deadline exceeded"}
            if event:
                event["status"] = "blocked"
        else:
            response = response
        finally:
            if not writer.is_closing():
                try:
                    if "response" in locals():
                        writer.write((json.dumps(response) + "\n").encode())
                        await writer.drain()
                except (ConnectionError, BrokenPipeError):
                    pass
                writer.close()
                await writer.wait_closed()
            self.tasks.discard(task)


class ReplayProvider:
    async def acquire(self, request):
        address = os.environ.get("E2E_RESOURCE_BRIDGE_ADDRESS")
        token = os.environ.get("E2E_RESOURCE_BRIDGE_TOKEN")
        launch_id = os.environ.get("E2E_RESOURCE_BRIDGE_RUN_ID")
        if not address or not token or not launch_id:
            raise ResourceUnavailable("No parent resource bridge; rerun through main.py replay")
        host, port = address.rsplit(":", 1)
        if host != "127.0.0.1":
            raise ResourceUnavailable("Resource bridge must be loopback-only")
        reader, writer = await asyncio.open_connection(host, int(port), limit=32768)
        try:
            data = {**request.audit(), "delivery_hint": request.delivery_hint}
            writer.write((json.dumps({"token": token, "launch_id": launch_id, "request": data}) + "\n").encode())
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), request.wait_timeout + 5))
            if response.get("status") == "cancelled":
                raise ResourceCancelled("User cancelled replay resource input")
            if response.get("status") != "provided":
                raise ResourceUnavailable("Parent resource provider did not supply the resource")
            return str(response["value"])
        finally:
            writer.close()
            await writer.wait_closed()
