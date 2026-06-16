"""WorkerSystem — run one daemon thread per (qname, process) pair,
each looping on its named queue and calling `process(entry, conn)` for
every dequeued item.

- One thread per pair; pass `[(qname, process_fn), ...]`. Repeat a
  qname to run a worker pool on the same queue.
- Workers block in `Queue.wait_for_item()` when the queue is empty,
  with a bounded timeout so the stop signal is observed promptly.
- `process(entry, conn)` returns the next queue's name (`str`), a list
  of next queue names for fan-out (`list[str]`), or `None` for
  terminal. The worker handles routing; process is pure logic.
- If `process` raises, the entry is routed to the configured
  `error_queue` (if any) and the worker continues. Without an
  error_queue, the exception is logged and the entry is dropped;
  recovery happens at the next process restart via `Queue.populate()`.
  Process functions must be idempotent.
- An optional `observer` callback receives `(event, qname, item)` for
  every worker-driven event:
    "deq"  — item was dequeued from `qname`
    "enq"  — item was enqueued to `qname` (next-queue, fan-out, or
             error_queue)
    "drop" — item was dropped after a process exception because no
             error_queue was configured; `qname` is the source queue
  See `LoggerObserver` for a sample that logs each event via Python
  logging.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from queuing import QueueRegistry

log = logging.getLogger(__name__)


# A process function takes (entry, conn) and returns the next queue's
# name, a list of next-queue names (fan-out), or None (terminal).
# Routing is the worker's job; process is pure logic.
ProcessFn = Callable[[Any, Any], "str | list[str] | None"]


# An observer is called with (event, qname, item) for every worker-
# driven enqueue or dequeue. event is "enq" or "deq".
ObserverFn = Callable[[str, str, Any], None]


class LoggerObserver:
    """Sample observer: logs each enq/deq event via Python logging.

    Usage:
        ws = WorkerSystem(..., observer=LoggerObserver())

    or with a specific logger / level:

        ws = WorkerSystem(..., observer=LoggerObserver(
            logger=logging.getLogger("myapp.queues"),
            level=logging.DEBUG,
        ))
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        level: int = logging.INFO,
    ) -> None:
        self._log = logger if logger is not None else logging.getLogger("qworkers")
        self._level = level

    def __call__(self, event: str, qname: str, item: Any) -> None:
        self._log.log(self._level, "%s %s %r", event, qname, item)


class WorkerSystem:
    """Run a thread per (qname, process) pair against a QueueRegistry.

    Lifecycle: `start()` spawns the threads, `stop()` signals shutdown
    and unblocks waiting workers, `join()` waits for them to exit.
    Also usable as a context manager: enter calls `start`, exit calls
    `stop` and waits for clean termination.
    """

    def __init__(
        self,
        pairs: list[tuple[str, ProcessFn]],
        *,
        registry: QueueRegistry | None = None,
        conn_factory: Callable[[], Any] | None = None,
        error_queue: str | None = None,
        observer: ObserverFn | None = None,
        wait_timeout: float = 1.0,
    ) -> None:
        """
        pairs:        [(qname, process_fn), ...]; one thread per entry.
                      Repeat the same qname to run a worker pool.
        registry:     existing QueueRegistry. If None, a fresh one is
                      built and the qnames in `pairs` are registered into
                      it. If given, any qnames in `pairs` not already in
                      `registry` are auto-registered.
        conn_factory: zero-arg callable returning a per-thread DB
                      connection. Each worker calls this once at thread
                      startup and passes the connection to process. If
                      None, process receives None.
        error_queue:  name of the queue to route entries to when process
                      raises. MUST be pre-registered in `registry` —
                      checked at construction. If None, exceptions are
                      logged and the entry is dropped; recovery happens
                      at next process restart via Queue.populate().
        observer:     optional callable invoked with (event, qname, item)
                      for every worker-driven event: "deq" (dequeued
                      from qname), "enq" (enqueued to qname), "drop"
                      (item dropped after process raised and no
                      error_queue is configured; qname is the source
                      queue). See LoggerObserver. Exceptions in the
                      observer are caught and logged; they don't disrupt
                      worker flow.
        wait_timeout: how long a worker blocks in wait_for_item before
                      re-checking the stop flag.
        """
        self._pairs = list(pairs)
        self._registry = registry if registry is not None else QueueRegistry()
        self._conn_factory = conn_factory
        self._error_queue = error_queue
        self._observer = observer
        self._wait_timeout = wait_timeout

        # Auto-register pair qnames. register is idempotent so this is
        # safe whether registry was passed in or freshly built.
        for qname, _ in self._pairs:
            self._registry.register(qname)

        # error_queue must be pre-registered by the caller (typically
        # with its own projection so error entries survive a restart).
        # Checked AFTER the pair auto-registration so an error_queue
        # that happens to be one of the worker queues passes.
        if error_queue is not None and error_queue not in self._registry:
            raise ValueError(
                f"error_queue {error_queue!r} is not registered in the "
                "registry; register it before constructing WorkerSystem"
            )

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def registry(self) -> QueueRegistry:
        return self._registry

    # ----- lifecycle ----------------------------------------------------

    def start(self) -> None:
        """Spawn one daemon thread per pair. Raises if already started."""
        if self._threads:
            raise RuntimeError(
                "WorkerSystem already started; call stop() before start()"
            )
        self._stop.clear()
        for i, (qname, process) in enumerate(self._pairs):
            t = threading.Thread(
                target=self._worker_loop,
                args=(qname, process, i),
                name=f"qworker:{qname}#{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def stop(self, *, join_timeout: float | None = None) -> None:
        """Signal workers to stop and wake any waiting on empty queues.
        If `join_timeout` is given, also wait up to that many seconds
        per thread for clean exit. Idempotent.

        `__init__` guaranteed every qname in `pairs` is registered, so
        `wake()` is unconditional. The set comprehension deduplicates so
        a queue with N workers is only woken once (one Event.set()
        unblocks all waiters)."""
        self._stop.set()
        for qname in {q for q, _ in self._pairs}:
            self._registry.get(qname).wake()
        if join_timeout is not None:
            for t in self._threads:
                t.join(timeout=join_timeout)
        self._threads = [t for t in self._threads if t.is_alive()]

    def join(self, *, timeout: float | None = None) -> None:
        """Wait for all worker threads to exit. Typically called after
        `stop()`. With `timeout=None`, blocks indefinitely until all
        threads have finished."""
        for t in self._threads:
            t.join(timeout=timeout)

    def __enter__(self) -> "WorkerSystem":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        # Best-effort clean shutdown. If we're exiting because of an
        # exception in the body, we still want to bring workers down.
        self.stop(join_timeout=self._wait_timeout * 2)
        return False  # don't suppress the body's exception

    # ----- worker loop --------------------------------------------------

    def _observe(self, event: str, qname: str, item: Any) -> None:
        """Fire the observer callback, isolating its exceptions so a
        buggy observer can't break a worker."""
        if self._observer is None:
            return
        try:
            self._observer(event, qname, item)
        except Exception:
            log.exception("observer raised on %s %s %r", event, qname, item)

    def _worker_loop(self, qname: str, process: ProcessFn, index: int) -> None:
        q = self._registry.get(qname)
        conn = self._conn_factory() if self._conn_factory is not None else None
        log.info("qworker:%s#%d started", qname, index)
        try:
            while not self._stop.is_set():
                entry = q.dequeue()
                if entry is None:
                    q.wait_for_item(timeout=self._wait_timeout)
                    continue
                self._observe("deq", qname, entry)
                try:
                    result = process(entry, conn)
                    if isinstance(result, str):
                        self._registry.get(result).enqueue(entry)
                        self._observe("enq", result, entry)
                    elif result is not None:
                        # list[str] — fan-out. Storage durability is the
                        # consumer's problem; the worker just enqueues.
                        for next_qname in result:
                            self._registry.get(next_qname).enqueue(entry)
                            self._observe("enq", next_qname, entry)
                    # result is None → terminal; nothing to do.
                except Exception:
                    log.exception(
                        "qworker:%s#%d process raised on entry %r",
                        qname, index, entry,
                    )
                    if self._error_queue is not None:
                        self._registry.get(self._error_queue).enqueue(entry)
                        self._observe("enq", self._error_queue, entry)
                    else:
                        # log and drop; recovery at next process restart
                        # via populate().
                        self._observe("drop", qname, entry)
        finally:
            if conn is not None and hasattr(conn, "close"):
                try:
                    conn.close()
                except Exception:
                    log.exception("qworker:%s#%d conn.close failed", qname, index)
            log.info("qworker:%s#%d exited", qname, index)


__all__ = ["WorkerSystem", "ProcessFn", "ObserverFn", "LoggerObserver"]
