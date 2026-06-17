"""WorkerSystem — run one daemon thread per (qname, process) pair,
each looping on its named queue and calling `process(entry, conn)` for
every dequeued item.

- One thread per pair; pass `[(qname, process_fn), ...]`. Repeat a
  qname to run a worker pool on the same queue.
- Workers block in `Queue.dequeue(timeout=...)` when the queue is
  empty, with a bounded timeout so the stop signal is observed
  promptly.
- `process(entry, conn)` returns the next queue's name (`str`), a list
  of next queue names for fan-out (`list[str]`), or `None` for
  terminal. The worker handles routing; process is pure logic.
- If `process` raises `TransientException`, the entry is re-enqueued
  at the HEAD of its source queue (immediate retry); error_queue is
  not involved.
- If `process` raises any other exception, the entry is routed to the
  configured `error_queue` (if any) and the worker continues. Without
  an error_queue, the exception is logged and the entry is dropped;
  recovery happens at the next process restart via `Queue.populate()`.
  Process functions must be idempotent.
- An optional `observer` callback receives `(event, qname, item)` for
  every worker-driven event:
    "deq"      — item was dequeued from `qname`
    "enq"      — item was enqueued to `qname` tail (next-queue,
                 fan-out, or error_queue)
    "enq_head" — item was re-enqueued at `qname`'s head after process
                 raised `TransientException`
    "drop"     — item was dropped after a process exception because no
                 error_queue was configured; `qname` is the source
                 queue
  See `LoggerObserver` for a sample that logs each event via Python
  logging.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from queuing import QueueRegistry

log = logging.getLogger(__name__)


class TransientException(Exception):
    """Raised by a process function to signal a retryable failure.

    The worker catches this specifically and re-enqueues the entry at
    the HEAD of its source queue for immediate retry. The entry does
    NOT route to the error_queue and is NOT dropped.

    Use for transient conditions where the same input is expected to
    succeed on a subsequent attempt — DB busy, network blip, rate-
    limited downstream, temporary lock contention. Use any other
    Exception type for permanent failures (those route to error_queue
    or are logged-and-dropped).
    """


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


@dataclass
class WorkerStats:
    """Per-worker counters maintained by WorkerSystem. One bump per
    `process()` call attempt — TransientException retries are also
    counted (they consumed worker time and signal flakiness).

    - `success_count`: `process()` returned a non-error routing target
      (next queue, fan-out, or terminal None).
    - `permanent_failure_count`: `process()` raised a non-Transient
      exception (routed to `error_queue`) OR returned `error_queue` as
      its next-queue label.
    - `transient_failure_count`: `process()` raised TransientException
      (entry head-requeued for immediate retry).
    - `service_total_s`: cumulative wall time inside `process()` across
      ALL outcomes above.

    `items_serviced()` = sum of the three outcome counters.
    `service_avg_s()` = service_total_s / items_serviced.
    """
    success_count: int = 0
    permanent_failure_count: int = 0
    transient_failure_count: int = 0
    service_total_s: float = 0.0

    def items_serviced(self) -> int:
        return (self.success_count + self.permanent_failure_count
                + self.transient_failure_count)

    def service_avg_s(self) -> float:
        n = self.items_serviced()
        return self.service_total_s / n if n else 0.0


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
                      for every worker-driven event:
                        "deq"      — dequeued from qname
                        "enq"      — enqueued to qname tail
                        "enq_head" — re-enqueued at qname's head after
                                     a TransientException
                        "drop"     — dropped after process raised and
                                     no error_queue is configured;
                                     qname is the source queue
                      See LoggerObserver. Exceptions in the observer
                      are caught and logged; they don't disrupt worker
                      flow.
        wait_timeout: how long a worker blocks in dequeue() before
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
        # Per-worker stats keyed by (qname, index). Initialized lazily
        # in _worker_loop so a not-yet-started system has an empty dict.
        self._worker_stats: dict[tuple[str, int], WorkerStats] = {}
        self._stats_lock = threading.Lock()

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
        with self._stats_lock:
            stats = self._worker_stats.setdefault((qname, index), WorkerStats())
        try:
            while not self._stop.is_set():
                entry = q.dequeue(timeout=self._wait_timeout)
                if entry is None:
                    continue
                self._observe("deq", qname, entry)
                t0 = time.monotonic()
                outcome = "ok"  # "ok" | "permanent" | "transient"
                try:
                    result = process(entry, conn)
                    if isinstance(result, str):
                        self._registry.get(result).enqueue(entry)
                        self._observe("enq", result, entry)
                        if result == self._error_queue:
                            outcome = "permanent"
                    elif result is not None:
                        # list[str] — fan-out. Storage durability is the
                        # consumer's problem; the worker just enqueues.
                        for next_qname in result:
                            self._registry.get(next_qname).enqueue(entry)
                            self._observe("enq", next_qname, entry)
                    # result is None → terminal; outcome stays "ok".
                except TransientException:
                    log.warning(
                        "qworker:%s#%d transient on entry %r; "
                        "re-queuing at head", qname, index, entry,
                    )
                    q.enqueue_head(entry)
                    self._observe("enq_head", qname, entry)
                    outcome = "transient"
                except Exception:
                    log.exception(
                        "qworker:%s#%d process raised on entry %r",
                        qname, index, entry,
                    )
                    if self._error_queue is None:
                        raise RuntimeError(
                            f"qworker:{qname}#{index}: process raised and no "
                            f"error_queue is configured; refusing to drop "
                            f"entry {entry!r}"
                        )
                    self._registry.get(self._error_queue).enqueue(entry)
                    self._observe("enq", self._error_queue, entry)
                    outcome = "permanent"
                elapsed = time.monotonic() - t0
                with self._stats_lock:
                    stats.service_total_s += elapsed
                    if outcome == "ok":
                        stats.success_count += 1
                    elif outcome == "permanent":
                        stats.permanent_failure_count += 1
                    else:  # transient
                        stats.transient_failure_count += 1
        finally:
            if conn is not None and hasattr(conn, "close"):
                try:
                    conn.close()
                except Exception:
                    log.exception("qworker:%s#%d conn.close failed", qname, index)
            log.info("qworker:%s#%d exited", qname, index)

    def worker_stats(self) -> dict[tuple[str, int], WorkerStats]:
        """Snapshot of per-worker counters. Returns a shallow copy of
        the dict so iteration is safe against concurrent updates; the
        WorkerStats values are returned by reference (their fields may
        tick forward while a caller reads them — acceptable for a
        dashboard)."""
        with self._stats_lock:
            return dict(self._worker_stats)


__all__ = [
    "LoggerObserver",
    "ObserverFn",
    "ProcessFn",
    "TransientException",
    "WorkerSystem",
]
