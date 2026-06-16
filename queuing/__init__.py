"""queuing — in-memory named FIFO queues backed by a projection of the
consumer's durable state.

Public surface:

    from queuing import Queue, QueueRegistry

The consumer's audit tables are the source of truth; this library is
an in-memory working cache for fast dequeue, with `repopulate()` that
re-derives the cache from a projection callable at boot or as a
self-heal sweep.

Multi-thread within one process; not cross-process. See README.md for
the worker pattern; queue.py for the implementation.
"""
from .queue import Queue, QueueRegistry

__all__ = ["Queue", "QueueRegistry"]
