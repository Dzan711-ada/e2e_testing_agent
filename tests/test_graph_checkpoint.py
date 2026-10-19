import asyncio
import json
from typing import TypedDict
import unittest

from langgraph.graph import StateGraph, START, END
from e2e_agent.graph_checkpoint import AsyncGraphSession, AsyncNodeBridge, MemorySaver


class ProbeState(TypedDict, total=False):
    run_id: str
    sent: int
    pending_resource: dict
    completed: bool


def probe(events):
    bridge = AsyncNodeBridge()
    owner = asyncio.get_running_loop()

    async def send(state):
        if asyncio.get_running_loop() is not owner:
            raise AssertionError("Async node left its owner loop")
        events.append((state["run_id"], "send"))
        return {"sent": state.get("sent", 0) + 1,
                "pending_resource": {"ref": "otp", "status": "requested"}}

    async def wait(state):
        events.append((state["run_id"], "wait"))
        return {"pending_resource": {}, "completed": True}

    child = StateGraph(ProbeState)
    child.add_node("send", bridge.node(send))
    child.add_node("await_resource", bridge.node(wait))
    child.add_edge(START, "send")
    child.add_edge("send", "await_resource")
    child.add_edge("await_resource", END)
    parent = StateGraph(ProbeState)
    parent.add_node("execute", child.compile(interrupt_before=["await_resource"]))
    parent.add_edge(START, "execute")
    parent.add_edge("execute", END)
    return AsyncGraphSession(parent.compile(checkpointer=MemorySaver()), bridge)


class InstalledCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_pause_preserves_position_and_resume_does_not_resend(self):
        self.assertTrue(asyncio.get_running_loop().get_debug())
        events = []
        graph = probe(events)
        self.assertIs(type(graph.checkpointer), MemorySaver)
        self.assertIn("execute", graph.get_graph().nodes)
        config = {"configurable": {"thread_id": "first"}}
        async for _ in graph.astream({"run_id": "first", "sent": 0}, config, subgraphs=True):
            pass
        snapshot = await graph.aget_state(config, subgraphs=True)
        self.assertEqual(snapshot.next, ("execute",))
        child = snapshot.tasks[0].state
        self.assertEqual(child.next, ("await_resource",))
        self.assertEqual(child.values["sent"], 1)
        self.assertEqual(child.values["pending_resource"]["ref"], "otp")
        json.dumps(child.values)
        self.assertEqual(events, [("first", "send")])
        result = await graph.ainvoke(None, config)
        self.assertTrue(result["completed"])
        self.assertEqual(events, [("first", "send"), ("first", "wait")])
        self.assertFalse((await graph.aget_state(config)).next)
        self.assertEqual(graph.active_workers, 0)

    async def test_waiting_runs_have_independent_checkpoints(self):
        events = []
        graph = probe(events)
        for identity in ("first", "second"):
            await graph.ainvoke({"run_id": identity, "sent": 0}, {"configurable": {"thread_id": identity}})
        await graph.ainvoke(None, {"configurable": {"thread_id": "second"}})
        first = await graph.aget_state({"configurable": {"thread_id": "first"}}, subgraphs=True)
        self.assertEqual(first.tasks[0].state.next, ("await_resource",))
        self.assertNotIn(("first", "wait"), events)
        self.assertEqual(events.count(("second", "send")), 1)
        config = {"configurable": {"thread_id": "first"}}
        history = await asyncio.to_thread(lambda: list(graph.checkpointer.list(config)))
        self.assertTrue(history)

    async def test_state_is_readable_while_async_resource_node_is_waiting(self):
        self.assertTrue(asyncio.get_running_loop().get_debug())
        started, cleaned = asyncio.Event(), asyncio.Event()
        bridge = AsyncNodeBridge()

        async def send(state):
            return {"sent": 1, "pending_resource": {"ref": "otp", "status": "requested"}}

        async def wait(state):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        builder = StateGraph(ProbeState)
        builder.add_node("send", bridge.node(send))
        builder.add_node("await_resource", bridge.node(wait))
        builder.add_edge(START, "send")
        builder.add_edge("send", "await_resource")
        builder.add_edge("await_resource", END)
        graph = AsyncGraphSession(builder.compile(checkpointer=MemorySaver()), bridge)
        config = {"configurable": {"thread_id": "read-waiting"}}
        task = asyncio.create_task(graph.ainvoke({"run_id": "read-waiting"}, config))
        try:
            await asyncio.wait_for(started.wait(), 5)
            snapshot = await asyncio.wait_for(graph.aget_state(config), 2)
            self.assertEqual(snapshot.next, ("await_resource",))
            self.assertEqual(snapshot.values["pending_resource"]["ref"], "otp")
            self.assertEqual(snapshot.values["sent"], 1)
            self.assertFalse(task.done())
            self.assertEqual(graph.active_workers, 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(graph.active_workers, 0)

    async def test_cancelled_invocation_joins_worker_and_async_node_cleanup(self):
        for mode in ("invoke", "stream"):
            with self.subTest(mode=mode):
                started, cleaned = asyncio.Event(), asyncio.Event()
                bridge = AsyncNodeBridge()

                async def wait(state):
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await asyncio.sleep(0)
                        cleaned.set()

                builder = StateGraph(ProbeState)
                builder.add_node("wait", bridge.node(wait))
                builder.add_edge(START, "wait")
                builder.add_edge("wait", END)
                graph = AsyncGraphSession(builder.compile(checkpointer=MemorySaver()), bridge)
                config = {"configurable": {"thread_id": "cancel-" + mode}}

                async def run():
                    if mode == "invoke":
                        return await graph.ainvoke({"run_id": "cancel"}, config)
                    async for _ in graph.astream({"run_id": "cancel"}, config):
                        pass

                task = asyncio.create_task(run())
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                self.assertTrue(cleaned.is_set())
                self.assertEqual(graph.active_workers, 0)
