# queuing

In-memory named FIFO queues whose contents are populated from a
projection of the consumer's durable state. Multi-thread safe within
one process; the deque's lock makes `dequeue()` atomic across worker
threads.

## Worker pattern

```python
import sqlite3, time
from queuing import QueueRegistry

reg = QueueRegistry()

def categorize_projection():
    conn = sqlite3.connect("yourapp.db")
    return [row[0] for row in conn.execute(
        "SELECT s.instruction_id FROM stages s "
        "LEFT JOIN stages t "
        "  ON t.instruction_id = s.instruction_id AND t.stage_name='categorize' "
        "WHERE s.q = 'categorize' AND t.instruction_id IS NULL"
    )]

reg.register("categorize", projection=categorize_projection)
reg.register("request",    projection=request_projection)

# Fill every queue from its projection at process start.
reg.populate_all()

# Worker thread for the categorize queue:
q_in  = reg.get("categorize")
q_out = reg.get("request")

while not should_stop:
    payload = q_in.dequeue()
    if payload is None:
        time.sleep(POLL_INTERVAL)
        continue
    conn = sqlite3.connect("yourapp.db", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    do_categorize(payload, conn)            # writes stages.categorize row with q='request'
    conn.commit()
    q_out.enqueue(payload)                  # notification AFTER commit
```

Always commit first; the in-memory `enqueue` is a notification AFTER
the durable state has advanced. A rollback before commit leaves the
on-disk state unchanged; the next process restart re-derives the item
via `populate()`.

## Factory: build a registry from a queue-shaped table

When your durable queue state lives in a table with a `q_name` column
(one row per queue item, queue assignment in `q_name`), the
`register_from_table` factory builds projections for every named queue
in one call:

```python
import sqlite3
from queuing import QueueRegistry

conn = sqlite3.connect("yourapp.db")

reg = QueueRegistry()
reg.register_from_table(
    conn,
    table="q_item",
    queue_names=["categorize", "request", "plan", "research"],
    q_column="q_name",          # default
    id_column="payload",
    where_extra=None,           # optional SQL appended to WHERE
)
reg.populate_all()
queue = reg.get("categorize")
```

Each queue's projection is `SELECT <id_column> FROM <table> WHERE
<q_column> = 'X' [AND <where_extra>]`. Items move between queues by
the consumer's worker doing `UPDATE <table> SET <q_column>='<next>'
WHERE …` in its own transaction — that commit IS the dequeue+enqueue.

## Integrity

All queue interaction goes through `QueueRegistry.get(name)`, which
raises `KeyError` for unregistered names. Workers can neither enqueue
to nor dequeue from a queue they didn't register. The registry is the
contract for which queues exist; nothing outside it can address a queue.

For forensic detection of pre-existing rows with retired queue names,
call `detect_orphans(conn, table)` — returns a list of `q_name` values
present in the table but not registered. Decide what to do — log,
alert, delete, migrate, ignore.

## Multi-thread workers

A single Queue object supports N worker threads pulling from it
concurrently. `dequeue()` is atomic; two threads can't see the same
payload. Run as many threads as the consumer's database can handle
concurrent writes against.

## What goes on the consumer's tables

The consumer's tables carry the queue assignment:

```sql
ALTER TABLE stages ADD COLUMN q TEXT;
ALTER TABLE tasks  ADD COLUMN q TEXT;
```

Projection for queue `Y` over stages:

```sql
SELECT s.instruction_id FROM stages s
LEFT JOIN stages downstream
       ON downstream.instruction_id = s.instruction_id
      AND downstream.stage_name = 'Y'
WHERE  s.q = 'Y'
  AND  downstream.instruction_id IS NULL
ORDER BY s.seq
```

For executor queues over `tasks`:

```sql
SELECT t.id FROM tasks t
WHERE  t.q = 'Y' AND t.status NOT LIKE 'done:%'
ORDER BY t.seq
```

For a bootstrap queue (e.g., ingest), no upstream stage row exists;
project from the source table directly.

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
reg.depths()     # {"categorize": 15, "request": 3, ...}     current depth per queue
reg.snapshot()   # {"categorize": {...}, "request": {...}}    q.snapshot() per queue
```

`pending()` returns a FIFO-ordered list of the currently-pending payloads
for one queue (diagnostics only — don't drive control flow off it).

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

Lower index in the constructor list = higher priority. A gated
queue's blocking `dequeue()` yields its payload only when the queue
has an item AND no higher-priority gated queue has items pending.
Queues outside the gate are unaffected. Membership is fixed for
process lifetime (no `detach`). Build the gate BEFORE starting any
worker threads — attaching to a queue with a live blocked dequeue
is unsupported.

All payloads must be hashable with stable hashes — storage is an
`OrderedDict` keyed by payload (insertion order = FIFO, key membership =
O(1) dedup), so this is a library-wide requirement, gated or not.
`enqueue()` does an explicit `hash(item)` before taking any lock, so an
unhashable payload fails fast with `TypeError`.

Workers running gated queues with `dequeue_timeout=None` need an
external signal handler that calls `WorkerSystem.stop()` from the
main thread; Python delivers SIGTERM only to the main thread and a
worker parked in `cond.wait()` does not unblock on its own.

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
