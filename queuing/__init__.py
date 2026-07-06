"""queuing — in-memory named FIFO queues backed by a projection of the
consumer's durable state.

Public surface:

    from queuing import Queue, QueuePriority, QueueRegistry, Stats

The consumer's audit tables are the source of truth; this library is
an in-memory working cache for fast dequeue, with `populate()` that
re-derives the cache from a projection callable at boot or as a
self-heal sweep.

Multi-thread within one process; not cross-process. See README.md for
the worker pattern; queue.py for the implementation.
"""
from .queue import Queue, QueuePriority, QueueRegistry, Stats, SupportsExecute

__all__ = ["Queue", "QueuePriority", "QueueRegistry", "Stats", "SupportsExecute"]
