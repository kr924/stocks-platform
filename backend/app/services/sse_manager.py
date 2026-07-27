"""
Server-Sent Events (SSE) Manager — Real-time event broadcasting.

Provides a lightweight pub/sub event bus that pushes new market events,
news stories, alerts, and filings to all connected SSE clients instantly.

Usage:
    from app.services.sse_manager import sse_manager

    # In a scraper/aggregator after saving a new event:
    sse_manager.broadcast("new_event", {"id": "event_123", "title": "...", ...})

    # In the SSE endpoint:
    async def stream():
        queue = sse_manager.subscribe()
        try:
            while True:
                event = await queue.get()
                yield f"event: {event['type']}\\ndata: {json.dumps(event['data'])}\\n\\n"
        finally:
            sse_manager.unsubscribe(queue)
"""
import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("app.sse_manager")


class SSEManager:
    """
    Manages Server-Sent Events broadcasting to connected clients.

    Each connected client gets its own asyncio.Queue. When a new event is
    broadcast, it is pushed to every queue. Disconnected clients are
    automatically cleaned up when their queue is unsubscribed.
    """

    def __init__(self):
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def client_count(self) -> int:
        """Number of currently connected SSE clients."""
        return len(self._subscribers)

    async def subscribe(self) -> asyncio.Queue:
        """
        Register a new SSE client. Returns a Queue that will receive
        broadcast events as dicts with 'type' and 'data' keys.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.add(queue)
        logger.info(f"SSE client connected (total: {self.client_count})")
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a disconnected client's queue."""
        async with self._lock:
            self._subscribers.discard(queue)
        logger.info(f"SSE client disconnected (total: {self.client_count})")

    async def _async_broadcast(self, event_type: str, data: Dict[str, Any]) -> None:
        """Internal async broadcast to all subscriber queues."""
        message = {
            "type": event_type,
            "data": data,
            "timestamp": time.time(),
        }

        dead_queues = []
        async with self._lock:
            for queue in self._subscribers:
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    # Client is too slow — mark for removal
                    dead_queues.append(queue)
                    logger.warning("SSE client queue full, dropping client")

            for dq in dead_queues:
                self._subscribers.discard(dq)

    def broadcast(self, event_type: str, data: Dict[str, Any]) -> None:
        """
        Broadcast an event to all connected SSE clients.

        This is safe to call from synchronous code (e.g., scraper threads).
        It schedules the broadcast on the running event loop.

        Args:
            event_type: One of 'new_event', 'new_news', 'new_alert',
                        'new_filing', 'heartbeat', 'stats_update'
            data: JSON-serializable dict with the event payload
        """
        try:
            loop = asyncio.get_running_loop()
            # We're in an async context — schedule directly
            loop.create_task(self._async_broadcast(event_type, data))
        except RuntimeError:
            # Called from a sync thread (e.g., background scraper thread)
            # Use the stored event loop reference
            if self._event_loop and self._event_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._async_broadcast(event_type, data),
                    self._event_loop,
                )
            else:
                # No event loop available yet — silently skip
                logger.debug(
                    f"SSE broadcast skipped (no event loop): {event_type}"
                )

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store a reference to the main event loop for cross-thread broadcasts."""
        self._event_loop = loop
        logger.debug("SSE manager event loop reference set")


# Module-level singleton — import this everywhere
sse_manager = SSEManager()
