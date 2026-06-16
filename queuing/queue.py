"""In-memory named FIFO queues populated from a projection of the
consumer's durable state.

- A `deque` per named queue. Enqueue and dequeue are O(1) and atomic
  across threads in this process.
- Each queue is constructed with a `projection` callable that returns
  the items that should currently be on the queue, by inspecting the
  consumer's audit tables. The library holds no on-disk state of its
  own.
- Workers dequeue items, advance the consumer's audit state in a
  transaction, and commit; on commit, the worker enqueues the payload
  to the next queue's in-memory deque as a notification. At the next
  process start, `populate()` rebuilds the deques from the projection;
  that is the recovery mechanism. Workers must be idempotent.
"""
from __future__ import annotations

import re
import threading
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any, Generic, TypeVar

T = TypeVar("T")

# Identifier validator for table/column names that get interpolated into
# SQL. SQLite has no `?` binding for identifiers, so the only safe way to
# accept a user-supplied table or column name is to enforce a strict
# allowlist regex before string-formatting it into the query.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENT_RE.match(value):
        raise ValueError(
            f"invalid SQL identifier for {label}: {value!r} "
            "(must match /^[A-Za-z_][A-Za-z0-9_]*$/)"
        )
    return value


def _coerce_conn_or_factory(conn_or_factory: Any) -> Callable[[], Any]:
    """Normalize a connection-or-factory argument to a zero-arg callable
    that returns a DB-API connection.

    `callable()` is the wrong predicate for distinguishing the two —
    sqlite3.Connection is itself callable (it aliases to .cursor()) —
    so we detect connections by the presence of `.execute`.
    """
    if hasattr(conn_or_factory, "execute"):
        _conn = conn_or_factory
        def get_conn():
            return _conn
        return get_conn
    if callable(conn_or_factory):
        return conn_or_factory
    raise TypeError(
        f"expected a DB-API connection or zero-arg callable returning one, "
        f"got {type(conn_or_factory).__name__}"
    )


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
        self._lock = threading.Lock()
        # Wake-up signal for blocked workers — set by enqueue. Sticky
        # threading.Event: a set() before a wait() is not lost. Consumed
        # by future worker harnesses that want to block on an empty queue
        # rather than poll.
        self._ready = threading.Event()

    # ----- producer side -------------------------------------------------

    def enqueue(self, item: T) -> bool:
        """Append `item` to the tail. Idempotent: enqueuing the same item
        twice while it's still pending is a no-op. Returns True if the
        item was added, False if it was already in the queue.

        Call AFTER the worker's transaction has committed — the on-disk
        state already reflects the next-queue assignment; this is a
        latency-saving notification to the in-memory deque.

        Sets the wake-up event so any thread blocked in
        `wait_for_item()` is unparked."""
        with self._lock:
            if item in self._items:
                return False
            self._items.append(item)
        self._ready.set()
        return True

    # ----- consumer side -------------------------------------------------

    def dequeue(self) -> T | None:
        """Pop the oldest item. Returns None if the queue is empty.

        The dequeue is atomic within this process. The item leaves the
        in-memory queue immediately and the worker takes responsibility
        for advancing the on-disk audit state in a transaction. If the
        worker fails before the audit commits, the on-disk state is
        unchanged; recovery happens at the next process restart when
        `populate()` re-derives the item from the projection."""
        with self._lock:
            if not self._items:
                return None
            return self._items.popleft()

    def wait_for_item(self, timeout: float | None = None) -> bool:
        """Block until an item is available, `wake()` is called, or
        `timeout` expires. Returns True if the event signaled (item or
        wake), False on timeout.

        Spurious returns are possible — `wake()` may have fired with no
        new item, or a concurrent dequeue may have drained the item
        that triggered the signal. Callers should re-check via
        `dequeue()` after this returns.

        Uses a clear-then-recheck pattern to close the missed-signal
        race: a producer that calls `enqueue` between our "looks empty"
        check and our `wait()` would set `_ready` AFTER our clear, so
        the wait returns immediately."""
        with self._lock:
            if self._items:
                return True
            self._ready.clear()
        # Re-check after clear: a producer may have raced between our
        # first check and our clear; if so, _items has the new item OR
        # _ready was set again after our clear (the wait below catches
        # that). Either way, the next iteration sees the truth.
        with self._lock:
            if self._items:
                return True
        return self._ready.wait(timeout)

    def wake(self) -> None:
        """Unblock anyone waiting in `wait_for_item()`, even when no
        item is available. Used by worker harnesses to interrupt waits
        on shutdown — `wait_for_item` returns True, the caller loops,
        checks its stop flag, and exits."""
        self._ready.set()

    # ----- projection / boot recovery -----------------------------------

    def populate(
        self,
        projection: Callable[[], Iterable[T]] | None = None,
    ) -> int:
        """Run the projection callable and enqueue any items it returns
        that are not already in the queue. Returns the number of items
        added.

        Call once at process start to derive the queue's contents from
        the consumer's audit state. The in-memory deque is empty until
        you do — the library does not populate itself.

        The projection must return unique items; the cross-call dedup
        (skip items already in the deque) is here to handle the case
        where `populate` is called more than once, not to fix a
        consumer that emits duplicates within one call.

        If `projection` is omitted, the projection passed to `__init__`
        is used. Calling without either raises ValueError."""
        proj = projection or self._projection
        if proj is None:
            raise ValueError(
                f"queue {self.name!r} has no projection — pass one to "
                "__init__ or to populate()"
            )
        # Materialize the projection results OUTSIDE the lock — the
        # callable typically runs a SQL query that may take milliseconds
        # to seconds, and we don't want to block dequeues during it.
        items = list(proj())
        added = 0
        with self._lock:
            for item in items:
                if item in self._items:
                    continue
                self._items.append(item)
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
    """A named collection of Queues, with bulk boot-population. Use when
    the consuming system has a fixed set of named queues (one per
    pipeline stage, one per executor):

        reg = QueueRegistry()
        reg.register("categorize", projection=lambda: db.find_ready_for("categorize"))
        reg.register("request",    projection=lambda: db.find_ready_for("request"))
        reg.register("research",   projection=lambda: db.find_pending_agent("research"))
        reg.populate_all()
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

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._queues

    def names(self) -> list[str]:
        """All registered queue names, in registration order."""
        with self._lock:
            return list(self._queues)

    def populate_all(self) -> dict[str, int]:
        """Populate every queue that has a projection. Returns
        `{queue_name: items_added}`. Queues without a projection are
        skipped silently. Call once at process start."""
        with self._lock:
            queues = list(self._queues.items())
        out: dict[str, int] = {}
        for name, q in queues:
            if q._projection is None:
                continue
            out[name] = q.populate()
        return out

    def depths(self) -> dict[str, int]:
        """`{queue_name: current_depth}` for every registered queue."""
        with self._lock:
            queues = list(self._queues.items())
        return {name: len(q) for name, q in queues}

    def register_from_table(
        self,
        conn_or_factory: Any,
        table: str,
        queue_names: Iterable[str],
        *,
        q_column: str = "q_name",
        id_column: str = "id",
        where_extra: str | None = None,
    ) -> list[Queue]:
        """Register one Queue per name in `queue_names`, each backed by a
        projection over `table`. The projection per queue X is:

            SELECT <id_column> FROM <table>
            WHERE <q_column> = 'X' [AND <where_extra>]

        This factory does NOT verify the table is free of orphan q values
        at construction time — that check is brittle at init (the table
        might not exist yet, might be empty, or might carry pre-existing
        rows from a retired queue name that would block startup). The
        integrity property the consumer actually wants — that new writes
        can only target registered queues — is enforced by
        `QueueRegistry.get()` raising KeyError on unknown names; workers
        can only enqueue to queues they obtained through `get()`.

        For forensic detection of pre-existing orphans, call
        `detect_orphans()` separately whenever it makes sense (after
        migration, in a maintenance task, periodically). That decouples
        init from data inspection.

        `conn_or_factory` is either a DB-API connection (reused for every
        projection call) or a zero-arg callable returning one (use the
        callable form when projections run in worker threads with
        per-thread connections).

        `where_extra` is appended verbatim to the WHERE clause —
        consumers are responsible for the SQL safety of anything they
        pass in.

        Returns the list of Queue instances registered, in `queue_names`
        order. Idempotent against previously-registered names (the
        existing Queue is returned and reused).
        """
        _validate_ident(table, "table")
        _validate_ident(q_column, "q_column")
        _validate_ident(id_column, "id_column")

        names = list(queue_names)
        if not names:
            raise ValueError("queue_names must be non-empty")
        for n in names:
            if not isinstance(n, str) or not n:
                raise ValueError(
                    f"queue name must be a non-empty string, got {n!r}"
                )

        get_conn = _coerce_conn_or_factory(conn_or_factory)

        sql = f"SELECT {id_column} FROM {table} WHERE {q_column} = ?"
        if where_extra:
            sql = f"{sql} AND ({where_extra})"

        registered: list[Queue] = []
        for name in names:
            def make_projection(qn: str) -> Callable[[], list]:
                # Late-binding guard: capture `qn` as a default arg so
                # each closure binds the right queue name.
                def projection(_qn=qn):
                    return [row[0] for row in get_conn().execute(sql, (_qn,))]
                return projection
            q = self.register(name, projection=make_projection(name))
            registered.append(q)
        return registered

    def detect_orphans(
        self,
        conn_or_factory: Any,
        table: str,
        *,
        q_column: str = "q_name",
    ) -> list[str]:
        """Return the list of `<q_column>` values present in `table` that
        are not registered in this registry. NULL q values are ignored
        (rows with q_column IS NULL are not on any queue, which is a
        valid terminal state).

        Call this whenever the consuming app wants to verify data
        integrity — after schema migration, in a maintenance task, as a
        periodic sweep. Does NOT raise on orphans; returns them so the
        caller decides what to do (log, alert, delete, migrate, ignore).

        Returns an empty list when the table is empty or contains only
        registered q values. Raises whatever the DB driver raises if the
        table doesn't exist — call this AFTER ensuring the schema is in
        place.
        """
        _validate_ident(table, "table")
        _validate_ident(q_column, "q_column")

        get_conn = _coerce_conn_or_factory(conn_or_factory)

        with self._lock:
            registered = set(self._queues)

        found = {
            row[0]
            for row in get_conn().execute(
                f"SELECT DISTINCT {q_column} FROM {table} "
                f"WHERE {q_column} IS NOT NULL"
            )
        }
        return sorted(found - registered)


# ---------------------------- Public API ------------------------------ #

__all__ = ["Queue", "QueueRegistry"]
