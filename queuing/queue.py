"""In-memory named FIFO queues populated from a projection of the
consumer's durable state.

- An `OrderedDict[payload, enq_ts]` per named queue. Enqueue and
  dequeue are O(1) and atomic across threads in this process. The
  same structure serves both ordering (insertion-order = FIFO) and
  dedup (key membership = O(1)).
- Each queue is constructed with a `projection` callable that returns
  the items that should currently be on the queue, by inspecting the
  consumer's audit tables. The library holds no on-disk state of its
  own.
- Workers dequeue items, advance the consumer's audit state in a
  transaction, and commit; on commit, the worker enqueues the payload
  to the next queue's in-memory deque as a notification. At the next
  process start, `populate()` rebuilds the deques from the projection;
  that is the recovery mechanism. Workers must be idempotent.
- A `QueuePriority` can gate a subset of queues into a static
  priority group: a gated queue's blocking `dequeue()` yields only
  when no higher-priority gated queue has items. Membership is fixed
  for process lifetime. Payloads on gated queues must be hashable —
  that's enforced library-wide here because gated and ungated paths
  share the same storage type.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any, Generic, TypeVar

T = TypeVar("T")

log = logging.getLogger(__name__)

# Sentinel for `Queue.dequeue(timeout=...)` to distinguish "caller did
# not pass a timeout, use the queue's configured default" from
# "caller explicitly passed timeout=None, block forever". `None` already
# means "block forever" (matches threading.Event.wait), so we need a
# different sentinel for "use default".
_UNSET = object()


# Test-only hook fired inside `_dequeue_gated`, between `_allowed()`
# returning True and the `popitem(last=False)` that takes the item.
# Tests monkey-patch this to inject a race (e.g., enqueue something
# higher-priority while a worker is committed to popping). Production:
# stays None.
_TEST_HOOK_AFTER_ALLOWED: "Callable[[], None] | None" = None


# ---------------------------- Stats ----------------------------------- #


class Stats(dict):
    """Dict subclass with `inc`/`dec`/`add`/`sub`/`set` counter helpers.

    Per-key tracking of the running sum AND the min/max of the
    increments observed by inc/dec/add/sub. `set` is an absolute
    write — clears any tracked min/max for that key.

    `self[k]` returns the running sum (or whatever `set` wrote);
    `snapshot()` returns a dict where each tracked key is a 3-tuple
    `(value, min, max)` and each set-only key is its raw value.

    Lock-free: counters are best-effort dashboard data. The only race
    is concurrent same-key read-modify-write, which can lose a single
    ±1 increment. In practice most (queue, key) tuples have one writer
    so collisions are rare and the cost is undercounting by a handful
    over a hot queue's lifetime. The min/max side dicts share the same
    best-effort model — snapshot tolerates a partial-tracking view by
    falling back to scalar when either bound is missing. GIGO: `set`
    accepts any value, and `inc` after `set("k", "string")` will raise."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Per-key min/max of the INCREMENTS passed to inc/dec/add/sub.
        # `+inf` / `-inf` are the no-data sentinels: any real value
        # beats `+inf` as a candidate min and `-inf` as a candidate
        # max, so the first tracked sample wins. Snapshot detects the
        # sentinels and reports the key as scalar (no triple) so
        # downstream consumers can distinguish "no samples yet" from
        # a real measurement of zero.
        self._min: dict[str, float] = {}
        self._max: dict[str, float] = {}

    def _track(self, k: str, n: float) -> None:
        if n < self._min.get(k, float("inf")):
            self._min[k] = n
        if n > self._max.get(k, float("-inf")):
            self._max[k] = n

    def inc(self, k: str) -> None:
        self[k] = self.get(k, 0) + 1
        self._track(k, 1)

    def dec(self, k: str) -> None:
        self[k] = self.get(k, 0) - 1
        self._track(k, -1)

    def add(self, k: str, n: float) -> None:
        self[k] = self.get(k, 0) + n
        self._track(k, n)

    def sub(self, k: str, n: float) -> None:
        self[k] = self.get(k, 0) - n
        self._track(k, -n)

    def set(self, k: str, v: Any) -> None:
        self[k] = v
        # An absolute write voids any tracked min/max — those describe
        # the increments, not the new value.
        self._min.pop(k, None)
        self._max.pop(k, None)

    def snapshot(self) -> dict[str, Any]:
        """Coherent shallow copy. Each tracked key becomes a 3-tuple
        `(value, min, max)` where min/max are over the increments
        observed via inc/dec/add/sub since the key was last `set` (or
        since process start). Each untracked key (pre-seeded or written
        by `set`) is its scalar value."""
        out: dict[str, Any] = {}
        for k, v in self.items():
            mn = self._min.get(k)
            mx = self._max.get(k)
            if mn is not None and mx is not None:
                out[k] = (v, mn, mx)
            else:
                out[k] = v
        return out

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

    Payloads must be hashable: storage is an OrderedDict keyed by
    payload. Edmund's queues carry str/int ids so this is invisible
    in-tree.

    Thread-safe within one process (an `RLock` guards the deque for
    ungated queues; a shared `Condition` owned by `QueuePriority`
    guards them for gated queues). NOT safe across processes — the
    storage lives in this process's heap.
    """

    def __init__(
        self,
        name: str,
        projection: Callable[[], Iterable[T]] | None = None,
        *,
        dequeue_timeout: float | None = None,
    ) -> None:
        if not name or not isinstance(name, str):
            raise ValueError(
                f"queue name must be a non-empty string, got {name!r}"
            )
        self.name = name
        self._projection = projection
        # OrderedDict[payload, enq_ts]: insertion-order = FIFO,
        # key-in test = O(1) dedup. Replaces deque[QItem]+set
        # designs by collapsing storage + dedup into one structure.
        self._items: OrderedDict[T, float] = OrderedDict()
        # RLock so a future caller that holds self._lock and calls
        # len(self) doesn't deadlock. Plain Lock would. RLock cost
        # is ~30% more than Lock — negligible at queue rates.
        self._lock = threading.RLock()
        # Counters live in a single user-mutable Stats blob. Built-in
        # keys (`enq`, `deq`, `wait_total_s`) are pre-seeded so
        # `Stats.snapshot()`'s `dict(self)` copy never races a
        # brand-new-key insertion (which CPython surfaces as
        # `RuntimeError: dictionary changed size during iteration`).
        # Stage code adds its own keys (e.g. `tokens`) via
        # `q.stats.add(...)` — first add for a fresh key still has a
        # tiny race window; pre-seed externally if it matters.
        self.stats = Stats({"enq": 0, "deq": 0, "wait_total_s": 0.0})
        # Default for `dequeue(timeout=...)` when the caller omits it.
        # `None` = block forever (rely on enqueue/wake to unblock);
        # `0`    = non-blocking (return None immediately on empty);
        # `>0`   = block up to N seconds, then return None.
        self._dequeue_timeout = dequeue_timeout
        # Wake-up signal for blocked ungated dequeues. Sticky
        # threading.Event: a set() before a wait() is not lost — the
        # wait returns immediately. Set by ungated enqueue and wake().
        # Gated paths use gate._cond, not this Event.
        self._ready = threading.Event()
        # Set by wake(); consumed by the next dequeue that finds the
        # queue empty so the caller (typically a worker checking a
        # stop flag) gets a None return instead of looping back into
        # another wait. Cleared whenever consumed.
        self._wake_pending = False
        # Set by QueuePriority via _attach_gate. None = ungated.
        self._gate: "QueuePriority | None" = None

    # ----- gate attach ---------------------------------------------------

    def _attach_gate(
        self,
        gate: "QueuePriority",
        dequeue_timeout: float | None,
    ) -> None:
        """Called once by QueuePriority.__init__ for each member queue.
        Construction-time only — must run before any worker thread
        starts a `dequeue()` on this queue."""
        if self._gate is not None:
            raise RuntimeError(
                f"{self.name}: already attached to a QueuePriority"
            )
        # The `with self._lock:` is retained so an ungated `enqueue`
        # already mid-flight cannot interleave its _items mutation
        # between our `_wake_pending=False` reset and the
        # `self._gate=gate` flip — without it, the next gated
        # dequeue could observe a stale wake state.
        with self._lock:
            if (
                self._dequeue_timeout is not None
                and self._dequeue_timeout != dequeue_timeout
            ):
                log.warning(
                    "%s: per-queue dequeue_timeout=%r overridden by "
                    "QueuePriority's dequeue_timeout=%r",
                    self.name, self._dequeue_timeout, dequeue_timeout,
                )
            self._dequeue_timeout = dequeue_timeout
            # Pre-attach wake() leaves _wake_pending=True sticky.
            # Drop it so the first gated dequeue doesn't return None
            # spuriously.
            self._wake_pending = False
            # self._ready is not cleared — gated path ignores it.
            self._gate = gate  # last write — flips routing

    def _len_locked(self) -> int:
        """Caller MUST hold the right lock (self._lock if ungated,
        self._gate._cond if gated)."""
        return len(self._items)

    # ----- producer side -------------------------------------------------

    def _contains_payload(self, payload: T) -> bool:
        """O(1) dedup check. Caller must hold the right lock."""
        return payload in self._items

    def enqueue(self, item: T) -> bool:
        """Append `item` to the tail. Idempotent on payload: enqueuing
        the same payload while a prior one is still pending is a no-op.
        Returns True if the item was added, False if it was already in
        the queue. The enqueue timestamp is `time.monotonic()` at this
        call, used by dequeue() to accumulate wait time.

        Payloads must be hashable; an explicit `hash(item)` runs before
        any lock acquire and raises `TypeError` immediately."""
        hash(item)  # fast-fail before any lock
        if self._gate is None:
            with self._lock:
                if item in self._items:
                    return False
                self._items[item] = time.monotonic()
            self.stats.inc("enq")
            self._ready.set()
            return True
        cond = self._gate._cond
        with cond:
            if item in self._items:
                return False
            self._items[item] = time.monotonic()
            self.stats.inc("enq")
            cond.notify_all()
        return True

    def enqueue_head(self, item: T) -> bool:
        """Insert `item` at the HEAD of the queue, so the next
        `dequeue()` returns it. Same payload-dedup as `enqueue`."""
        hash(item)
        if self._gate is None:
            with self._lock:
                if item in self._items:
                    return False
                self._items[item] = time.monotonic()
                self._items.move_to_end(item, last=False)
            self.stats.inc("enq")
            self._ready.set()
            return True
        cond = self._gate._cond
        with cond:
            if item in self._items:
                return False
            self._items[item] = time.monotonic()
            self._items.move_to_end(item, last=False)
            self.stats.inc("enq")
            cond.notify_all()
        return True

    # ----- consumer side -------------------------------------------------

    def dequeue(self, timeout: float | None = _UNSET) -> T | None:  # type: ignore[assignment]
        """Block until an item is available, then return its payload.

        `timeout` semantics:
        - omitted          → use `self._dequeue_timeout` (set at __init__).
        - `None`           → block forever (until enqueue, populate, or wake).
        - `0`              → non-blocking; return None immediately on empty.
        - positive float   → block up to that many seconds, then return None.

        Returns `None` on timeout or wake().
        Accumulates `(deq_ts - enq_ts)` into `stats["wait_total_s"]`.

        Shutdown contract: `wake()` sets a one-shot `_wake_pending`
        flag. The next dequeue that finds the queue empty (ungated)
        or that runs the wake-check (gated) consumes the flag and
        returns None, giving the caller a chance to check a stop
        signal.

        Gated queues honor the caller-supplied timeout verbatim — the
        gate's configured `dequeue_timeout` is only the default when
        the caller omits the argument.
        """
        if self._gate is None:
            return self._dequeue_ungated(timeout)
        return self._dequeue_gated(timeout)

    def _dequeue_ungated(self, timeout: Any) -> T | None:
        eff = self._dequeue_timeout if timeout is _UNSET else timeout
        deadline = (time.monotonic() + eff) if (eff is not None and eff > 0) else None
        while True:
            with self._lock:
                if self._items:
                    payload, enq_ts = self._items.popitem(last=False)
                    popped: tuple[T, float] | None = (payload, enq_ts)
                elif self._wake_pending:
                    self._wake_pending = False
                    return None
                else:
                    popped = None
                    self._ready.clear()
            if popped is not None:
                payload, enq_ts = popped
                self.stats.inc("deq")
                self.stats.add("wait_total_s", time.monotonic() - enq_ts)
                return payload
            if eff == 0:
                return None
            remaining: float | None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
            else:
                remaining = None
            self._ready.wait(timeout=remaining)

    def _dequeue_gated(self, timeout: Any) -> T | None:
        eff = self._dequeue_timeout if timeout is _UNSET else timeout
        deadline = (time.monotonic() + eff) if (eff is not None and eff > 0) else None
        cond = self._gate._cond  # type: ignore[union-attr]
        payload: T
        enq_ts: float
        with cond:
            while True:
                # Order: wake → empty → allowed → pop. Wake is checked
                # BEFORE items (inverted from ungated): under shutdown
                # we don't want a worker about to exit to consume an
                # in-flight item; populate-on-restart will replay it.
                if self._wake_pending:
                    self._wake_pending = False
                    return None
                if self._items and self._gate._allowed(self):  # type: ignore[union-attr]
                    if _TEST_HOOK_AFTER_ALLOWED is not None:
                        _TEST_HOOK_AFTER_ALLOWED()
                    payload, enq_ts = self._items.popitem(last=False)
                    cond.notify_all()  # drain-to-empty notify
                    break
                if eff == 0:
                    return None
                remaining = (deadline - time.monotonic()) if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    return None
                cond.wait(remaining)
        self.stats.inc("deq")
        self.stats.add("wait_total_s", time.monotonic() - enq_ts)
        return payload

    def wake(self) -> None:
        """Unblock a thread blocked in `dequeue()`, even when no item
        is available. Used by worker harnesses to interrupt waits on
        shutdown — the next dequeue that finds the queue empty
        consumes the wake and returns None, letting the worker check
        its stop flag and exit."""
        if self._gate is None:
            with self._lock:
                self._wake_pending = True
            self._ready.set()
            return
        with self._gate._cond:
            self._wake_pending = True
            self._gate._cond.notify_all()

    # ----- projection / boot recovery -----------------------------------

    def populate(
        self,
        projection: Callable[[], Iterable[T]] | None = None,
    ) -> int:
        """Run the projection callable and enqueue any items it returns
        that are not already in the queue. Returns the number of items
        added.

        Call once at process start to derive the queue's contents from
        the consumer's audit state. The in-memory queue is empty until
        you do — the library does not populate itself.

        The projection must return unique items; the cross-call dedup
        (skip items already in the queue) is here to handle the case
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
        if self._gate is None:
            with self._lock:
                for item in items:
                    if item in self._items:
                        continue
                    self._items[item] = time.monotonic()
                    added += 1
            if added:
                self.stats.add("enq", added)
                self._ready.set()
            return added
        cond = self._gate._cond
        with cond:
            for item in items:
                if item in self._items:
                    continue
                self._items[item] = time.monotonic()
                added += 1
            if added:
                self.stats.add("enq", added)
                cond.notify_all()
        return added

    # ----- inspection ----------------------------------------------------

    def __len__(self) -> int:
        if self._gate is None:
            with self._lock:
                return len(self._items)
        with self._gate._cond:
            return len(self._items)

    def snapshot(self) -> dict[str, Any]:
        """Return a flat dict copy of `self.stats` with derived keys
        folded in:

        - `depth`: items pending right now, i.e. `len(self)`.
        - `wait_avg_s`: `wait_total_s / deq` — average wait per item.
        - `wait_min_s`/`wait_max_s`: min and max per-item wait
          observed (extracted from `wait_total_s`'s tracked triple).

        Scalars: this method UNPACKS the triples Stats.snapshot returns
        for tracked keys back into scalar sums (`enq`, `deq`,
        `wait_total_s`, any user-added counters like `tokens`), so
        existing dashboards keep working. Tracked extremes ride along
        as `<key>_min`/`<key>_max` siblings for any user-added key. The
        built-in `wait_*` exposes the min/max under the customary
        `wait_min_s` / `wait_max_s` field names.
        """
        raw = self.stats.snapshot()
        s: dict[str, Any] = {}
        for k, v in raw.items():
            if isinstance(v, tuple) and len(v) == 3:
                value, mn, mx = v
                s[k] = value
                if k == "wait_total_s":
                    s["wait_min_s"] = mn
                    s["wait_max_s"] = mx
                else:
                    s[f"{k}_min"] = mn
                    s[f"{k}_max"] = mx
            else:
                s[k] = v
        s["depth"] = len(self)
        deq = s.get("deq", 0)
        s["wait_avg_s"] = (s.get("wait_total_s", 0.0) / deq) if deq else 0.0
        # Deliberately NOT defaulting wait_min_s/wait_max_s. Absence
        # means "no samples yet" — fabricating 0.0 here would make an
        # idle queue look like it had a real measurement of zero. The
        # dashboard's _has_data check distinguishes absence from a
        # real zero.
        return s

    def pending(self) -> list[T]:
        """Return a fresh list of currently pending payloads, in FIFO
        order. For dashboards and diagnostics only — do not use to
        drive control flow."""
        if self._gate is None:
            with self._lock:
                return list(self._items)
        with self._gate._cond:
            return list(self._items)


# ---------------------------- QueuePriority --------------------------- #


class QueuePriority:
    """Static-priority gate over a set of named queues. Lower index in
    the constructor list = higher priority. A gated queue's blocking
    dequeue() yields its payload only when this queue is at the top of
    the priority group. Queues outside the gate are unaffected.

    Membership is fixed for process lifetime — no detach().

    Must be constructed BEFORE any worker thread that will dequeue
    from a member queue starts. Attaching to a queue with a live
    blocked dequeue() is a programmer error.
    """

    def __init__(
        self,
        queues: list[Queue],
        *,
        dequeue_timeout: float | None = 1.0,
    ) -> None:
        self._queues: list[Queue] = list(queues)
        self._prio: dict[str, int] = {q.name: i for i, q in enumerate(queues)}
        self._cond = threading.Condition()
        for q in queues:
            q._attach_gate(self, dequeue_timeout)

    def _allowed(self, q: Queue) -> bool:
        """Caller MUST hold self._cond. True iff no higher-priority
        gated queue has items pending. O(N) over the gate's members
        (N ≤ ~10 expected); each member contributes one O(1) len read."""
        my = self._prio[q.name]
        for other in self._queues:
            if other is q:
                continue
            if other._len_locked() > 0 and self._prio[other.name] < my:
                return False
        return True


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

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """`{queue_name: q.snapshot()}` for every registered queue. The
        per-queue dict carries built-in counters (`enq`/`deq`/
        `wait_total_s`), derived `depth` and `wait_avg_s`, and any
        stage-supplied keys (e.g. `tokens`)."""
        with self._lock:
            queues = list(self._queues.items())
        return {name: q.snapshot() for name, q in queues}

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

__all__ = ["Queue", "QueuePriority", "QueueRegistry", "Stats"]
