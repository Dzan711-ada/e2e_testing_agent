"""Sync LangGraph/checkpoints with async nodes kept on their owner event loop.

Uses the installed LangGraph APIs and the original volatile MemorySaver without
patching third-party code. Browser/resource objects never leave their loop.
"""
import asyncio
import concurrent.futures
from contextvars import ContextVar
import inspect
import threading

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver


_current_scope = ContextVar("e2e_graph_call_scope", default=None)


class _CallScope:
    def __init__(self):
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.futures = set()
        # Only the owner event loop reads or writes these async tasks.
        self.tasks = set()

    def register(self, future):
        with self.lock:
            self.futures.add(future)
            if self.stopped.is_set():
                future.cancel()

    def discard(self, future):
        with self.lock:
            self.futures.discard(future)

    def cancel(self):
        self.stopped.set()
        with self.lock:
            for future in tuple(self.futures):
                future.cancel()

    async def wait_nodes(self):
        # Flush task creation/cancellation callbacks whose concurrent Future
        # may already be done while its coroutine is still cleaning up.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


class AsyncNodeBridge:
    """Wrap parent/child async nodes before compiling the synchronous graph."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.owner_thread = threading.get_ident()

    def check_loop(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Graph session must use the async nodes' owner event loop")

    def node(self, function):
        takes_config = "config" in inspect.signature(function).parameters

        def invoke(state, config: RunnableConfig):
            if threading.get_ident() == self.owner_thread:
                raise RuntimeError("Use AsyncGraphSession to run bridged graph nodes")
            scope = _current_scope.get()
            if scope is None:
                raise RuntimeError("Async node has no active graph call scope")
            if scope.stopped.is_set():
                raise concurrent.futures.CancelledError()

            async def call():
                task = asyncio.current_task()
                scope.tasks.add(task)
                try:
                    if scope.stopped.is_set():
                        raise asyncio.CancelledError()
                    if takes_config:
                        return await function(state, config=config)
                    return await function(state)
                finally:
                    scope.tasks.discard(task)

            coroutine = call()
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            except BaseException:
                coroutine.close()
                raise
            scope.register(future)
            try:
                return future.result()
            finally:
                scope.discard(future)

        # Do not set __wrapped__: LangGraph needs this wrapper's explicit
        # RunnableConfig parameter rather than the async function's signature.
        invoke.__name__ = getattr(function, "__name__", "async_graph_node")
        return invoke


class AsyncGraphSession:
    """Small async facade over sync stream/invoke/get_state with joined cancellation."""

    def __init__(self, graph, bridge: AsyncNodeBridge):
        self.graph, self.bridge = graph, bridge
        self.checkpointer = graph.checkpointer
        self._lock = asyncio.Lock()
        self._workers = set()

    @property
    def active_workers(self):
        return len(self._workers)

    def get_graph(self, *args, **kwargs):
        return self.graph.get_graph(*args, **kwargs)

    async def _join(self, worker, scope):
        # Caller cancellation must not detach the to_thread worker. Repeated
        # cancellation still revokes node futures and waits for cleanup.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                scope.cancel()
            except BaseException:
                break
        try:
            worker.result()
        except BaseException:
            pass
        cleanup = asyncio.create_task(scope.wait_nodes())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                scope.cancel()
        cleanup.result()
        self._workers.discard(worker)

    async def astream(self, value, config=None, **kwargs):
        self.bridge.check_loop()
        async with self._lock:
            scope, events = _CallScope(), asyncio.Queue()

            def send(kind, payload=None):
                self.bridge.loop.call_soon_threadsafe(events.put_nowait, (kind, payload))

            def run():
                token = _current_scope.set(scope)
                stream = None
                try:
                    stream = self.graph.stream(value, config, **kwargs)
                    for update in stream:
                        if scope.stopped.is_set():
                            break
                        send("update", update)
                except BaseException as error:
                    send("error", error)
                finally:
                    try:
                        if stream is not None:
                            stream.close()
                    finally:
                        _current_scope.reset(token)
                        send("done")

            worker = asyncio.create_task(asyncio.to_thread(run))
            self._workers.add(worker)
            completed = False
            try:
                while True:
                    kind, payload = await events.get()
                    if kind == "update":
                        yield payload
                    elif kind == "error":
                        raise payload
                    else:
                        completed = True
                        break
            finally:
                if not completed:
                    scope.cancel()
                await self._join(worker, scope)

    async def _call(self, method, *args, **kwargs):
        self.bridge.check_loop()
        async with self._lock:
            scope = _CallScope()

            def run():
                token = _current_scope.set(scope)
                try:
                    return method(*args, **kwargs)
                finally:
                    _current_scope.reset(token)

            worker = asyncio.create_task(asyncio.to_thread(run))
            self._workers.add(worker)
            try:
                return await asyncio.shield(worker)
            finally:
                if not worker.done():
                    scope.cancel()
                await self._join(worker, scope)

    async def ainvoke(self, value, config=None, **kwargs):
        return await self._call(self.graph.invoke, value, config, **kwargs)

    async def aget_state(self, config, *, subgraphs=False):
        # A waiting node may itself request the latest checkpoint. Read-only
        # access cannot share the invocation lock held by that same node.
        self.bridge.check_loop()
        scope = _CallScope()
        worker = asyncio.create_task(asyncio.to_thread(
            self.graph.get_state, config, subgraphs=subgraphs))
        self._workers.add(worker)
        try:
            return await asyncio.shield(worker)
        finally:
            await self._join(worker, scope)
