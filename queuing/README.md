# queuing

In-memory named FIFO queues, thread-safe within one process. Use them
three ways, each building on the last:

1. **[Pure in-memory](#pure-in-memory-no-backing-store)** — a plain
   thread-safe work queue.
2. **[With priorities](#with-priorities)** — a static priority order
   across a set of queues.
3. **[With a backing store](#with-a-backing-store-durable-crash-recoverable)**
   — back each queue with a projection of your durable state so it
   survives a restart.

For the thread-per-queue worker harness, see the companion
[`qworkers`](../qworkers/README.md) package.

## Pure in-memory (no backing store)

At its simplest a `Queue` is a thread-safe FIFO. Construct one, `enqueue`
payloads, `dequeue` them in order:

```python
from queuing import Queue

q = Queue("jobs")
q.enqueue("a")
q.enqueue("b")
q.dequeue()                     # -> "a"
```

`dequeue()` blocks while the queue is empty until something is enqueued.
Pass `timeout=` to bound the wait (`0` = non-blocking, `None` = block
forever, a positive float = seconds then return `None`), or set a per-queue
default with `Queue("jobs", dequeue_timeout=1.0)`. `wake()` unblocks a
waiting dequeue so a worker can re-check a stop flag and exit.

A `QueueRegistry` is a named collection — register queues by name, then
hand each worker the queue it needs:

```python
from queuing import QueueRegistry

reg = QueueRegistry()
reg.register("jobs")
reg.get("jobs").enqueue(payload)   # KeyError on an unregistered name
```

Multi-thread native: a single `Queue` feeds N worker threads pulling
concurrently — `dequeue()` is atomic, so no two threads ever see the same
payload. Payloads must be hashable; storage is an `OrderedDict` keyed by
payload, which also makes `enqueue` idempotent (re-enqueuing an item
already on the queue is a no-op).

In this mode the queue holds only what you `enqueue` — there's no durable
source of truth, so anything still queued is lost on process exit. That's
fine when the producer lives in the same process and you don't need crash
recovery; add a backing store (below) when a queue must survive a restart.

## With priorities

`QueuePriority` gates a set of queues into a static priority order — lower
index = higher priority. A gated queue's `dequeue()` yields only when no
higher-priority gated queue has anything pending:

```python
from queuing import Queue, QueuePriority

high = Queue("high")
low  = Queue("low")
gate = QueuePriority([high, low])   # high outranks low

low.enqueue("batch")
high.enqueue("urgent")

high.dequeue()   # -> "urgent"
low.dequeue()    # -> "batch"  (only because high is now empty)
```

You never call methods on `gate` directly — constructing it is what does
the work. `QueuePriority(...)` reaches into each queue you pass and rewires
its `dequeue()` to honor the priority, so you keep dequeuing from `high`
and `low` exactly as before and the gating happens invisibly. Just keep
`gate` alive: each member queue holds a reference back to it, so it won't
be garbage-collected, but binding it to a name keeps that explicit.

- Build the gate **before** starting any worker threads — attaching to a
  queue that already has a blocked `dequeue()` is unsupported. Membership
  is fixed for the process lifetime (no `detach`).
- Queues outside the gate are unaffected.
- The gate's `dequeue_timeout` (default `1.0`) becomes the default wait for
  its member queues; a caller-supplied `timeout=` still overrides it.
- A worker on a gated queue with `dequeue_timeout=None` (block forever)
  needs an external signal handler to call `WorkerSystem.stop()` from the
  main thread — Python delivers SIGTERM only to the main thread, and a
  worker parked in `cond.wait()` won't unblock on its own.

## Stats & observability

Every `Queue` owns a `stats` attribute — a `Stats` object (a `dict`
subclass) that accumulates counters for the queue. It is pre-seeded with
three built-in keys and stage code may add its own:

- `enq` — total items enqueued (bumped by `enqueue`, `enqueue_head`,
  `populate`).
- `deq` — total items dequeued.
- `wait_total_s` — cumulative `dequeue_ts - enqueue_ts` across all
  dequeued items (monotonic-clock seconds).
- *your keys* — stage code calls `q.stats.add("tokens", n)`,
  `q.stats.inc("retries")`, etc. to track anything else per queue.

`Stats` is deliberately **unlocked** — counters are updated in place
without acquiring the queue lock, so recording a stat never blocks an
enqueue or dequeue (liveness over precision). The cost is a race window
on a concurrent same-key read-modify-write, which can lose a single ±1.
That window is tiny (a couple of bytecode ops) and, in practice, most
`(queue, key)` pairs have a single writer, so collisions are rare; over a
hot queue's lifetime the worst case is undercounting by a handful. Treat
it as dashboard data, not a ledger. Alongside the running sum,
`inc`/`dec`/`add`/`sub` also track the **min, max, and sample count of the
increments** for each key. `set` writes an absolute value and clears any
tracked min/max/count for that key.

### `Queue.snapshot()`

`snapshot()` returns a flat `dict` — the counters plus derived fields —
suitable for a dashboard row:

```python
q.snapshot()
# {
#   "enq": 1280, "deq": 1265,        # built-in counters (scalar sums)
#   "wait_total_s": 41.7,
#   "depth": 15,                     # len(q) right now
#   "wait_avg_s": 0.0329,            # wait_total_s / deq
#   "wait_min_s": 0.0011,            # per-item wait extremes (omitted
#   "wait_max_s": 0.6203,            #   until at least one dequeue)
#   "tokens": 98230,                 # a stage-added key, as a scalar sum
#   "tokens_min": 12, "tokens_max": 4096, "tokens_n": 1265,
# }
```

- `depth` and `wait_avg_s` are always present (`wait_avg_s` is `0.0` on an
  idle queue).
- `wait_min_s` / `wait_max_s` are **omitted entirely** until at least one
  item has been dequeued — absence means "no samples yet", distinct from a
  real measurement of zero. A dashboard tests for the key rather than
  defaulting it.
- Each stage-added counter `k` rides along with `k_min`, `k_max`, and
  `k_n` siblings. `k_n` is the increment-sample count behind the extremes;
  divide `k / k_n` for a mean that stays within `[k_min, k_max]`. Do **not**
  divide by `deq` — a key may be bumped zero or many times per item, so
  `k / deq` can fall outside the extremes. (`wait_*` is the exception: it's
  bumped exactly once per dequeue, so `deq` is its correct denominator,
  which is why `wait_avg_s` uses it.)

### Registry-wide

```python
reg.depths()     # {"ingest": 0, "categorize": 15, ...}      current depth per queue
reg.snapshot()   # {"ingest": {...}, "categorize": {...}}    q.snapshot() per queue
```

`pending()` returns a FIFO-ordered list of the currently-pending payloads
for one queue (diagnostics only — don't drive control flow off it).

## With a backing store (durable, crash-recoverable)

To survive a restart, back each queue with a **projection** — a callable
that returns the items that should currently be on the queue, by reading
your durable state. The library holds no on-disk state of its own; your
table is the source of truth, and `populate()` rebuilds the in-memory
queue from the projection at boot (or as a self-heal sweep).

Durable state here is a single `WORK` table: one row per item, `QUEUE`
holds the item's current queue, `TIMESTAMP` gives FIFO order.

```python
import sqlite3
from queuing import QueueRegistry

reg = QueueRegistry()

def projection(queue):
    conn = sqlite3.connect("yourapp.db")
    return [row[0] for row in conn.execute(
        "SELECT id FROM WORK WHERE QUEUE = ? ORDER BY TIMESTAMP", (queue,)
    )]

# Pipeline: ingest -> categorize -> filter -> done, plus an error queue.
for queue in ("ingest", "categorize", "filter", "done", "error"):
    reg.register(queue, projection=lambda q=queue: projection(q))

# Fill every queue from its projection at process start.
reg.populate_all()

# One worker body, reused per stage. `compute` does the app's writes;
# `out` is the queue to advance to on success, "error" on failure.
def run_worker(in_name, out_name, compute):
    q_in = reg.get(in_name)
    while not should_stop:
        payload = q_in.dequeue()
        if payload is None:
            continue
        conn = sqlite3.connect("yourapp.db", isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")         # open the txn FIRST
        try:
            compute(payload, conn)              # app's own writes
            target = out_name                   # success -> next stage
        except Exception:
            conn.rollback()                     # discard the failed work
            conn.execute("BEGIN IMMEDIATE")     # fresh txn for the routing move
            target = "error"                    # failure -> error queue
        conn.execute("UPDATE WORK SET QUEUE = ? WHERE id = ?", (target, payload))
        conn.commit()                           # work + queue move land together
        reg.get(target).enqueue(payload)        # notify whichever queue we moved to

# Each stage runs in its own thread; both route failures to "error":
run_worker("categorize", "filter", categorize)   # categorize -> filter | error
run_worker("filter",     "done",   filter_)      # filter -> done | error
```

The transaction is opened **before** the compute call so the app's own
writes and the queue move (`UPDATE WORK SET QUEUE = ...`) are one atomic
unit. Either the item is fully processed AND advanced to the next queue,
or neither happens — there's no window where the app state changed but the
item is still parked on its source queue, nor one where it advanced
without its work being done.

On a caught exception the worker **rolls back the partial app writes**,
opens a fresh transaction, and moves the item to the `error` queue
instead of the next stage. That quarantines the bad item — it leaves
`categorize`/`filter` so the pipeline doesn't reprocess it forever, and an
error handler (or just an operator) can inspect, fix, and re-route it from
`error`. If instead the process dies mid-flight (an *uncaught* failure),
the open transaction rolls back with the on-disk `QUEUE` untouched, so the
next restart re-derives the item onto its source queue via `populate()`
and runs it again — which is why the compute must be idempotent.

The in-memory `enqueue` happens only AFTER the commit: it's a notification
that durable state has already advanced, never a substitute for it. A
crash between commit and enqueue loses only the notification, which
`populate()` rebuilds at restart.

### Factory: build a registry from a queue-shaped table

When your durable queue state lives in a table with a queue-name column
(one row per queue item, queue assignment in that column), the
`register_from_table` factory builds projections for every named queue
in one call:

```python
import sqlite3
from queuing import QueueRegistry

conn = sqlite3.connect("yourapp.db")

reg = QueueRegistry()
reg.register_from_table(
    conn,
    table="WORK",
    queue_names=["ingest", "categorize", "filter", "done"],
    q_column="QUEUE",           # default is "q_name"
    id_column="id",
    where_extra=None,           # optional SQL appended to WHERE
)
reg.populate_all()
queue = reg.get("categorize")
```

Each queue's projection is `SELECT <id_column> FROM <table> WHERE
<q_column> = 'X' [AND <where_extra>]`. Items move between queues by the
consumer's worker doing `UPDATE <table> SET <q_column>='<next>' WHERE …`
in its own transaction — that commit IS the dequeue+enqueue.

### What goes on the consumer's table

The consumer's table carries the queue assignment in a column — the
`QUEUE` column on `WORK` above. The minimum a row needs is an id, the
current queue, and something to order by:

```sql
CREATE TABLE WORK (
    id        INTEGER PRIMARY KEY,
    QUEUE     TEXT,                  -- which queue this item is on (NULL = none)
    TIMESTAMP REAL                   -- FIFO order within a queue
    -- ... plus whatever columns your app's compute reads and writes
);
```

Projection for queue `Y` is then just every row currently assigned to it,
in order:

```sql
SELECT id FROM WORK WHERE QUEUE = 'Y' ORDER BY TIMESTAMP
```

A worker advances an item by updating that column inside its transaction
(`UPDATE WORK SET QUEUE = '<next>' WHERE id = ?`), which is what the
projection re-derives at the next restart. The `QUEUE` column can live on
an existing table you already have — add it with
`ALTER TABLE <your_table> ADD COLUMN QUEUE TEXT` — and the id/order
columns can be any columns that uniquely identify and sequence a row
(a primary key and a `seq`/`created_at`, say). Add a `WHERE` predicate to
the projection to exclude already-finished rows when the same table holds
terminal items.

### Integrity

All queue interaction goes through `QueueRegistry.get(name)`, which
raises `KeyError` for unregistered names. Workers can neither enqueue
to nor dequeue from a queue they didn't register. The registry is the
contract for which queues exist; nothing outside it can address a queue.

For forensic detection of pre-existing rows with retired queue names,
call `detect_orphans(conn, table)` — returns a list of queue-name values
present in the table but not registered. Decide what to do — log, alert,
delete, migrate, ignore.

## Running queue workers → qworkers

The `run_worker` loop above — dequeue, process, route to the next queue or
an error queue, retry on transient failures — is exactly what the
companion [`qworkers`](../qworkers/README.md) package provides as a managed
harness: one daemon thread per `(queue, process_fn)` pair, worker pools,
clean start/stop/join lifecycle plus a context manager, an `error_queue`,
and per-worker stats. Reach for it instead of hand-rolling the loop once
you have more than one stage — see
[`qworkers/README.md`](../qworkers/README.md).

## API reference

```python
class Stats(dict):
    def inc(k: str) -> None                  # k += 1, track +1
    def dec(k: str) -> None                  # k -= 1, track -1
    def add(k: str, n: float) -> None        # k += n, track n
    def sub(k: str, n: float) -> None        # k -= n, track -n
    def set(k: str, v) -> None               # absolute write; clears tracked min/max/n
    def snapshot() -> dict                   # tracked keys -> (value, min, max, count)

class Queue:
    name: str
    stats: Stats                             # live counters (see "Stats & observability")

    def __init__(name: str,
                 projection: Callable[[], Iterable] | None = None,
                 *, dequeue_timeout: float | None = None)
    def enqueue(item) -> bool
    def enqueue_head(item) -> bool
    def dequeue(timeout: float | None = ...) -> item | None
    def wake() -> None
    def populate(projection: Callable | None = None) -> int
    def snapshot() -> dict                    # counters + depth/wait_avg_s/wait_min_s/wait_max_s
    def pending() -> list                     # FIFO-ordered pending payloads (diagnostics)
    def __len__() -> int                      # current depth

class QueueRegistry:
    def register(name: str, projection: Callable | None = None) -> Queue
    def get(name: str) -> Queue                  # KeyError on unknown name
    def __contains__(name: str) -> bool
    def names() -> list[str]
    def populate_all() -> dict[str, int]
    def depths() -> dict[str, int]               # {queue_name: current_depth}
    def snapshot() -> dict[str, dict]            # {queue_name: q.snapshot()}
    def register_from_table(
        conn_or_factory, table: str, queue_names: Iterable[str], *,
        q_column: str = "q_name", id_column: str = "id",
        where_extra: str | None = None,
    ) -> list[Queue]
    def detect_orphans(
        conn_or_factory, table: str, *, q_column: str = "q_name",
    ) -> list[str]

class QueuePriority:
    """Static-priority gate over a set of named queues."""

    def __init__(queues: list[Queue], *,
                 dequeue_timeout: float | None = 1.0)
```

All payloads must be hashable with stable hashes — storage is an
`OrderedDict` keyed by payload (insertion order = FIFO, key membership =
O(1) dedup), so this is a library-wide requirement, gated or not.
`enqueue()` does an explicit `hash(item)` before taking any lock, so an
unhashable payload fails fast with `TypeError`.

## Use as a git submodule

```bash
git submodule add https://github.com/rbpasker/pasker-net.git vendor/pasker-net
git submodule update --init
```

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "vendor" / "pasker-net"))
from queuing import Queue, QueueRegistry
```
