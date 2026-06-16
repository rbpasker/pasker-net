"""In-memory named FIFO queues whose contents are derived from a
projection of the consuming app's durable state.

Design — three properties carry the abstraction:

1. **The queue is in-memory.** Just a `deque` per named queue. No backing
   table, no broker hop, no serialization tax. Enqueue and dequeue happen
   at Python speed.

2. **The on-disk source of truth is the consuming app's own audit
   tables.** Each queue is constructed with a `projection` callable that
   returns "what should be on this queue right now" by inspecting the
   durable state of the consumer (typically a SQL query over its existing
   rows). The library never owns a row; the consumer's tables are the
   truth.

3. **At-least-once delivery from automatic recovery.** Workers dequeue
   items, do their work inside a database transaction that advances the
   audit state (writing the next-stage row, advancing task status,
   whatever), and commit. On commit, the worker enqueues the payload to
   the next queue's in-memory deque as a latency-saving notification.
   If anything fails between dequeue and commit, the on-disk state did
   not move and the projection will re-derive the same item on its next
   call. Workers must be idempotent — calling them twice with the same
   payload must be safe — which matches the discipline real audit
   pipelines already follow.

What this is NOT: a broker. Single-process only — the in-memory deque is
not shared across processes. There is no peek/ack/nack model and no
queue-level lock; correctness comes from the consumer's transaction plus
the projection. If you outgrow single-process, swap this library for one
that wraps Redis Streams / NATS JetStream / RabbitMQ behind the same
`Queue` interface; the consumer code does not change.
"""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable
from typing import Generic, TypeVar

T = TypeVar("T")


# ---------------------------- Queue ----------------------------------- #


class Queue(Generic[T]):
    """A named in-memory FIFO queue. Items are opaque to the queue —
    typically instruction IDs or task IDs that index rows in the
    consumer's durable tables.

    Thread-safe within one process (a `threading.Lock` guards the deque).
    NOT safe across processes — the deque lives in this process's heap.
    """

    def __init__(
        self,
        name: str,
        projection: Callable[[], Iterable[T]] | None = None,
    ) -> None:
        if not name or not isinstance(name, str):
            raise ValueError(
                f"queue name must be a non-empty string, got {name!r}"
            )
        self.name = name
        self._projection = projection
        self._items: deque[T] = deque()
        self._seen: set[T] = set()           # dedup against re-projection
        self._lock = threading.Lock()

    # ----- producer side -------------------------------------------------

    def enqueue(self, item: T) -> bool:
        """Append `item` to the tail. Idempotent: enqueuing the same item
        twice while it's still pending is a no-op. Returns True if the
        item was added, False if it was already in the queue.

        Call AFTER the worker's transaction has committed — the on-disk
        state already reflects the next-queue assignment; this is a
        latency-saving notification to the in-memory deque."""
        with self._lock:
            if item in self._seen:
                return False
            self._items.append(item)
            self._seen.add(item)
            return True

    def extend(self, items: Iterable[T]) -> int:
        """Bulk enqueue. Returns the number of newly added items
        (duplicates against the current pending set are skipped)."""
        added = 0
        with self._lock:
            for item in items:
                if item in self._seen:
                    continue
                self._items.append(item)
                self._seen.add(item)
                added += 1
        return added

    # ----- consumer side -------------------------------------------------

    def dequeue(self) -> T | None:
        """Pop the oldest item. Returns None if the queue is empty.

        The dequeue is atomic within this process. The item leaves the
        in-memory queue immediately and the worker takes responsibility
        for advancing the on-disk audit state in a transaction. If the
        worker fails before the audit commits, the on-disk state is
        unchanged and the projection will re-derive the item on the next
        `repopulate()` — at-least-once delivery."""
        with self._lock:
            if not self._items:
                return None
            item = self._items.popleft()
            self._seen.discard(item)
            return item

    # ----- projection / recovery -----------------------------------------

    def repopulate(
        self,
        projection: Callable[[], Iterable[T]] | None = None,
    ) -> int:
        """Run the projection callable and enqueue any items it returns
        that are not already in the queue. Returns the number of items
        added.

        Use at boot to derive queue contents from the consumer's audit
        state. Run periodically as a self-heal sweep to patch up any
        items the projection says should be queued but aren't (e.g., a
        worker died between commit and in-memory enqueue, or the process
        restarted).

        If `projection` is omitted, the projection passed to `__init__`
        is used. Calling without either raises ValueError."""
        proj = projection or self._projection
        if proj is None:
            raise ValueError(
                f"queue {self.name!r} has no projection — pass one to "
                "__init__ or to repopulate()"
            )
        # Materialize the projection results OUTSIDE the lock — the
        # callable typically runs a SQL query that may take milliseconds
        # to seconds, and we don't want to block dequeues during it.
        items = list(proj())
        added = 0
        with self._lock:
            for item in items:
                if item in self._seen:
                    continue
                self._items.append(item)
                self._seen.add(item)
                added += 1
        return added

    # ----- inspection ----------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> list[T]:
        """Return a fresh list of currently pending items, in FIFO order.
        For dashboards and diagnostics only — do not use to drive control
        flow. The caller may mutate the returned list without affecting
        queue state."""
        with self._lock:
            return list(self._items)


# ---------------------------- Registry -------------------------------- #


class QueueRegistry:
    """A named collection of Queues, with bulk operations for boot and
    self-heal. Use when the consuming system has a fixed set of named
    queues (one per pipeline stage, one per executor):

        reg = QueueRegistry()
        reg.register("categorize", projection=lambda: db.find_ready_for("categorize"))
        reg.register("request",    projection=lambda: db.find_ready_for("request"))
        reg.register("research",   projection=lambda: db.find_pending_agent("research"))
        reg.repopulate_all()
    """

    def __init__(self) -> None:
        self._queues: dict[str, Queue] = {}
        self._lock = threading.Lock()

    def register(
        self,
        name: str,
        projection: Callable[[], Iterable] | None = None,
    ) -> Queue:
        """Register a queue by name. If a queue with this name already
        exists, return it (idempotent)."""
        with self._lock:
            if name in self._queues:
                return self._queues[name]
            q = Queue(name, projection=projection)
            self._queues[name] = q
            return q

    def get(self, name: str) -> Queue:
        """Return the queue registered under `name`. Raises KeyError if
        not registered."""
        with self._lock:
            return self._queues[name]

    def names(self) -> list[str]:
        """All registered queue names, in registration order."""
        with self._lock:
            return list(self._queues)

    def repopulate_all(self) -> dict[str, int]:
        """Repopulate every queue that has a projection. Returns
        `{queue_name: items_added}`. Queues without a projection are
        skipped silently."""
        with self._lock:
            queues = list(self._queues.items())
        out: dict[str, int] = {}
        for name, q in queues:
            if q._projection is None:
                continue
            out[name] = q.repopulate()
        return out

    def depths(self) -> dict[str, int]:
        """`{queue_name: current_depth}` for every registered queue."""
        with self._lock:
            queues = list(self._queues.items())
        return {name: len(q) for name, q in queues}


# ---------------------------- Public API ------------------------------ #

__all__ = ["Queue", "QueueRegistry"]
