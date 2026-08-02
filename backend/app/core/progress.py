"""
ProgressBus — real-time workflow progress broadcast to WebSocket clients.

The workflow nodes emit structured events (node start/finish, gates, commit);
main.py forwards them to every connected /ws/refactor client. Zero deps:
an in-process pub/sub with async queues.
"""

import asyncio
import logging
import uuid
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class ProgressBus:
    """In-process event bus for workflow progress."""

    def __init__(self):
        self._subscribers: Dict[str, asyncio.Queue] = {}

    def subscribe(self) -> str:
        """Register a client; returns a subscription id."""
        sub_id = uuid.uuid4().hex[:10]
        self._subscribers[sub_id] = asyncio.Queue(maxsize=200)
        return sub_id

    def unsubscribe(self, sub_id: str):
        self._subscribers.pop(sub_id, None)

    def publish(self, event: Dict[str, Any]):
        """Emit an event to all subscribers (drops oldest when a queue is full)."""
        for queue in list(self._subscribers.values()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    async def next_event(self, sub_id: str, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
        """Block for the next event (or None on timeout)."""
        queue = self._subscribers.get(sub_id)
        if queue is None:
            return None
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


bus = ProgressBus()
