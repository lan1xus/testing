import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

Callback = Union[Callable[[str, Dict[str, Any]], Awaitable[None]], Callable[[str, Dict[str, Any]], None]]


class EventBus:
    """A very small async-first event bus.

    - Subscribers register for a topic and receive (topic, payload) pairs.
    - Topics are arbitrary strings; you may use dotted namespaces like
      "bot.status", "party.update", etc.
    - Callbacks may be async or regular functions. Regular functions are
      executed in the event loop thread.
    """

    def __init__(self) -> None:
        self._subs: Dict[str, List[Callback]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str, callback: Callback) -> Callable[[], Awaitable[None]]:
        async with self._lock:
            self._subs.setdefault(topic, []).append(callback)

        async def _unsubscribe() -> None:
            async with self._lock:
                items = self._subs.get(topic, [])
                try:
                    items.remove(callback)
                except ValueError:
                    pass
        return _unsubscribe

    # Convenience alias
    async def on(self, topic: str, callback: Callback) -> Callable[[], Awaitable[None]]:
        return await self.subscribe(topic, callback)

    async def publish(self, topic: str, payload: Dict[str, Any]) -> None:
        # Copy target callbacks to avoid holding the lock while invoking
        async with self._lock:
            callbacks = list(self._subs.get(topic, []))
            # Support hierarchical wildcard topics: e.g., "party.*"
            parts = topic.split(".")
            for i in range(len(parts), 0, -1):
                wildcard = ".".join(parts[:i - 1] + ["*"]) if i > 0 else "*"
                callbacks.extend(self._subs.get(wildcard, []))

        for cb in callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(topic, payload)  # type: ignore[misc]
                else:
                    cb(topic, payload)  # type: ignore[misc]
            except Exception:
                # Silently ignore subscriber errors to avoid breaking publishers
                # Real implementation could integrate logging here
                pass

    # Convenience alias
    async def emit(self, topic: str, payload: Dict[str, Any]) -> None:
        await self.publish(topic, payload)
