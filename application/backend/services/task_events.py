import asyncio
from typing import Callable, Set
from asyncio import Queue

class TaskEventBus:
    def __init__(self):
        self.subscribers: Set[Queue] = set()

    def subscribe(self) -> Queue:
        q: Queue = Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: Queue):
        self.subscribers.discard(q)

    async def publish(self, message: str):
        for q in self.subscribers:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass

_event_bus = TaskEventBus()

def subscribe() -> Queue:
    return _event_bus.subscribe()

def unsubscribe(q: Queue):
    return _event_bus.unsubscribe(q)

async def publish(message: str):
    await _event_bus.publish(message)
