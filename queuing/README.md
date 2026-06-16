# queuing

In-memory named FIFO queues whose contents are derived from a projection
of the consumer's durable state. The library holds no on-disk state of
its own — your audit tables are the source of truth, and the in-memory
deque is a working cache that can be lost and rebuilt at any time from
the projection.

## When this fits

You have a pipeline (or multi-task workflow) whose stages or executors
talk to each other by handing payloads (IDs, references) through named
queues. You want:

- **Single-process, multi-thread workers** competing for items on the
  same queue. The deque's lock gives you atomic dequeue for free.
- **Crash recovery without locks or visibility timeouts.** If a worker
  dies mid-work, the audit row it was about to write was never committed,
  so on restart the projection re-derives the same item. The library
  doesn't track in-flight state and doesn't need to.
- **Adding a queue to cost zero.** Register a name, optionally pass a
  projection; the queue exists.
- **Boot recovery and self-heal** by calling the same projection callable
  the queue was constructed with.

What this is NOT: a broker. Single-process only. There is no peek/ack/
nack, no cross-restart visibility, no built-in retry policy. If you
outgrow this, the `Queue` interface is small enough to port behind Redis
Streams / NATS JetStream / RabbitMQ — consumer code doesn't change.

## Why no peek/ack model

Real brokers need peek/ack because the broker is the only thing that
knows whether a worker actually finished. In this library, the
**consumer's transaction** is what advances the durable state, and the
absence of a committed audit row is the "this item still needs work"
signal. The library doesn't need its own ack — the projection reads the
audit and re-derives "what's still queued" from scratch.

Failure modes:

| where the worker dies | on-disk state | recovery |
|---|---|---|
| dequeued, then died before any SQL | unchanged | projection sees the upstream still points here; re-enqueues |
| committed, then died before `enqueue()` to next queue | next-stage row exists with `q='<next>'` | projection of the next queue picks up the orphan |
| transaction itself raised and rolled back | unchanged | same as the first case |

In every case, the in-memory loss is irrelevant. The on-disk state is
authoritative; the projection regenerates the cache.

## Worker pattern

```python
import sqlite3
from queuing import QueueRegistry

reg = QueueRegistry()

def categorize_projection(conn=conn):
    return [row[0] for row in conn.execute(
        "SELECT s.instruction_id FROM stages s "
        "LEFT JOIN stages t "
        "  ON t.instruction_id = s.instruction_id AND t.stage_name='categorize' "
        "WHERE s.q = 'categorize' AND t.instruction_id IS NULL"
    )]

reg.register("categorize", projection=categorize_projection)
reg.register("request",    projection=request_projection)

# Boot recovery — rebuild every queue's deque from durable state.
reg.repopulate_all()

# Worker thread for the categorize queue:
q_in  = reg.get("categorize")
q_out = reg.get("request")

while not should_stop:
    payload = q_in.dequeue()
    if payload is None:
        time.sleep(POLL_INTERVAL)
        continue
    try:
        conn = sqlite3.connect("yourapp.db", isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        do_categorize(payload, conn)           # writes stages.categorize row with q='request'
        conn.commit()
        q_out.enqueue(payload)                  # latency notification only
    except Exception:
        log.exception("worker failed; projection sweep will recover")
        # No need to re-enqueue manually — the rollback left the on-disk
        # state untouched, so the next reproject of this queue picks it up.
```

Note that `q_out.enqueue(payload)` runs AFTER `conn.commit()`. If it
runs before commit, a rollback would leave a phantom item in the next
queue's deque. Always commit first; enqueue is a notification.

## At-least-once and idempotency

The library delivers **at least once**. A worker that crashes between
dequeue and commit will have its item redelivered by the next projection
sweep. Workers must be idempotent: running them twice with the same
payload must be safe. This is the same discipline real audit pipelines
already follow (ON CONFLICT on stage rows, monotonic state machines on
tasks, etc.) — the library doesn't add any new requirement.

Duplicate-work window: if `repopulate()` runs while a worker is in the
middle of processing an item, the projection (reading the not-yet-
committed on-disk state) may re-add that item to the queue, and a second
worker may dequeue it. Both workers will then race; the first to commit
wins (the second's ON CONFLICT or status-check no-ops). To minimize the
window, run `repopulate()` only at boot and at long intervals (minutes).
Don't run it on every empty-queue tick.

## Multi-thread workers

A single Queue object supports N worker threads pulling from it
concurrently. Each `dequeue()` is atomic (a `threading.Lock` guards the
deque); two threads can't see the same payload. Run as many threads as
your DB can handle concurrent writes against (for SQLite, that's
typically one writer + many readers, so the bottleneck is at the DB
layer, not the queue).

For executor pools (e.g., N research-agent workers competing on the
"research" queue), this is the native shape.

For multi-process workers, you need a real broker. The `Queue` interface
is small enough to port — `enqueue`, `dequeue`, `repopulate` — behind
Redis Streams / NATS JetStream / RabbitMQ.

## API reference

```python
class Queue:
    name: str

    def __init__(name: str,
                 projection: Callable[[], Iterable] | None = None)
    def enqueue(item) -> bool
    def extend(items: Iterable) -> int
    def dequeue() -> item | None
    def repopulate(projection: Callable | None = None) -> int
    def snapshot() -> list
    def __len__() -> int

class QueueRegistry:
    def register(name: str, projection: Callable | None = None) -> Queue
    def get(name: str) -> Queue
    def names() -> list[str]
    def repopulate_all() -> dict[str, int]
    def depths() -> dict[str, int]
```

## What goes on the consumer's tables

Because this library holds no SQL state, the consumer's tables carry the
queue assignment. The recommended shape:

```sql
-- on each audit row, record where the worker routed the item next:
ALTER TABLE stages ADD COLUMN q TEXT;   -- the next queue this stage handed off to
ALTER TABLE tasks  ADD COLUMN q TEXT;   -- the executor queue this task is in
```

The projection for queue `Y` then looks like:

```sql
SELECT s.instruction_id FROM stages s
LEFT JOIN stages downstream
       ON downstream.instruction_id = s.instruction_id
      AND downstream.stage_name = 'Y'
WHERE  s.q = 'Y'
  AND  downstream.instruction_id IS NULL
  AND  <instruction is not cancelled>
ORDER BY s.seq
```

For executor queues over `tasks`:

```sql
SELECT t.id FROM tasks t
JOIN   instructions i ON ...
WHERE  t.q = 'Y' AND t.status NOT LIKE 'done:%'
  AND  i.state != 'cancelled'
ORDER BY t.seq
```

For the bootstrap queue (e.g., INGEST), there's no upstream stage row;
project from the source table directly:

```sql
SELECT i.id FROM instructions i
LEFT JOIN stages s ON s.instruction_id = i.id AND s.stage_name = 'ingest'
WHERE s.instruction_id IS NULL AND i.state != 'cancelled'
```

The consumer writes its own projection callables and registers them; the
library just runs them on demand.

## Use as a git submodule

In the consuming repo:

```bash
git submodule add https://github.com/rbpasker/pasker-net.git vendor/pasker-net
git submodule update --init
```

Then add `vendor/pasker-net/` to `sys.path` early in the entrypoint, or
symlink the `queuing/` directory under your project.

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "vendor" / "pasker-net"))
from queuing import Queue, QueueRegistry
```
