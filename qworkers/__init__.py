"""qworkers — thread workers that loop on named queues calling a
per-queue process function.

Public surface:

    from qworkers import WorkerSystem, ProcessFn

Composes with `queuing`: takes a list of `(qname, process_fn)` pairs
plus an optional pre-built `QueueRegistry`, spawns one daemon thread
per pair, and blocks each thread on `Queue.wait_for_item()` until a
new item arrives, `wake()` is called, or the stop signal fires.

See README.md for usage; worker.py for the implementation.
"""
from .worker import ProcessFn, WorkerSystem

__all__ = ["ProcessFn", "WorkerSystem"]
