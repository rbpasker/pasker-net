# queuing

SQL-backed named FIFO queues that live in the caller's database and ride
on the caller's transactions. The queue's state changes commit (or roll
back) atomically with whatever durable writes the worker is making —
no drift between "the queue says done" and "the audit says done."

## Use case

You have a pipeline (or multi-task workflow) whose stages or executors
talk to each other by handing instructions/tasks/whatever-payload through
named queues. You want:

- the queue's state to be a SQL row, not an in-memory deque that vanishes
  on restart;
- queue mutations to be **transactional with the worker's other writes**
  in the same database — no broker hop, no two-phase commit;
- adding a new queue to cost zero schema migrations;
- self-healing from a **projection** of your durable state, so a wiped
  queue can be re-derived from the audit at boot.

This library does all four. What it is not: a real broker. There is no
cross-process delivery, no acks that survive an OS-level restart of the
consuming process, no built-in retry policy. If you outgrow this, the
`Queue` interface is small enough to port behind Redis Streams / NATS
JetStream / RabbitMQ without touching consumer code.

## Schema

One table, one partial index. Install at boot:

```python
import sqlite3
from queuing import install_schema

conn = sqlite3.connect("yourapp.db")
install_schema(conn)
conn.commit()
```

The table:

```sql
CREATE TABLE q_item (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    q          TEXT NOT NULL,
    payload    TEXT NOT NULL,
    locked_at  TEXT,                          -- NULL = visible
    UNIQUE(q, payload)
);
CREATE INDEX q_item_visible ON q_item(q, seq) WHERE locked_at IS NULL;
```

All queues share this table. The `q` column discriminates rows; `seq` is
a global AUTOINCREMENT so monotonic ordering across queues works for
diagnostics ("the categorize push happened before the dispatch push").
`payload` is opaque text — the consuming app interprets it (instruction
UUID, task ID, JSON blob, whatever).

## Worker pattern

Every queue method takes the caller's connection. Wrap the peek + work +
ack in a single transaction; commit on success, let the rollback handle
crashes:

```python
import sqlite3
from queuing import Queue

q = Queue("categorize")

with sqlite3.connect("yourapp.db") as conn:    # SQLite transaction
    payload = q.peek(conn)                     # SELECT + UPDATE locked_at
    if payload is None:
        return                                 # queue empty
    do_work(payload, conn)                     # writes durable audit/state
    q.ack(conn, payload)                       # DELETE
# commit fires here; queue mutation + audit writes are atomic
```

If `do_work` raises, the `with` block rolls back — `locked_at` is undone,
the audit writes are undone, the queue row is back to visible. The next
worker peeks it. No special recovery path needed.

For multi-writer SQLite deployments, open the worker's transaction with
`BEGIN IMMEDIATE` rather than the default `BEGIN DEFERRED`, so that the
peek's SELECT+UPDATE pair serializes correctly against other peekers.

## Producer pattern

A worker that just finished its work for one queue often needs to push
the payload onto the next queue. Same transaction:

```python
with sqlite3.connect("yourapp.db") as conn:
    payload = q_in.peek(conn)
    if payload is None:
        return
    do_work(payload, conn)
    q_in.ack(conn, payload)
    q_out.push(conn, payload)
# all four operations commit together
```

If anything raises, all four roll back — no half-consumed item, no
orphan in the next queue.

## Projection pattern (boot recovery, self-heal)

At boot, the in-memory broker is empty but the consuming app's durable
audit tables already know what should be queued. Recover with a
projection callable that derives "items that should be on this queue
right now" from the audit:

```python
def categorize_projection(conn=conn):
    return [
        row[0] for row in conn.execute("""
            SELECT s.instruction_id
            FROM stages s
            LEFT JOIN stages t
              ON t.instruction_id = s.instruction_id AND t.stage_name = 'categorize'
            WHERE s.next = 'categorize' AND t.instruction_id IS NULL
        """)
    ]

q = Queue("categorize")
with sqlite3.connect("yourapp.db") as conn:
    q.repopulate(conn, categorize_projection, clear_stale_locks=True)
```

`clear_stale_locks=True` is correct *only at boot* — it assumes no
in-flight work is alive. NEVER pass it during steady-state operation; it
will yank items out from under live workers.

The same `repopulate` call (without `clear_stale_locks`) can run as a
background self-heal sweep, patching up items the projection says should
be queued but somehow aren't. It's a no-op when nothing has drifted.

## Inspection

Cheap dashboards:

```python
from queuing import queue_names, queue_depths

queue_names(conn)
# ["categorize", "research", "evaluation"]

queue_depths(conn)
# {"categorize": {"pending": 3, "inflight": 0},
#  "research":   {"pending": 0, "inflight": 1},
#  "evaluation": {"pending": 5, "inflight": 0}}
```

Per-queue snapshot (diagnostics only — don't drive control flow with it):

```python
pending, inflight = Queue("categorize").snapshot(conn)
```

## API reference

```python
class Queue:
    name: str

    def push(conn, payload: str) -> bool
    def extend(conn, payloads: Iterable[str]) -> int
    def peek(conn) -> str | None
    def ack(conn, payload: str) -> bool
    def nack(conn, payload: str) -> bool
    def repopulate(conn, projection: Callable[[], Iterable[str]],
                   *, clear_stale_locks: bool = False) -> int
    def pending_count(conn) -> int
    def inflight_count(conn) -> int
    def snapshot(conn) -> tuple[list[str], list[tuple[str, str]]]

SCHEMA_DDL: tuple[str, ...]
def install_schema(conn) -> None
def queue_names(conn) -> list[str]
def queue_depths(conn) -> dict[str, dict[str, int]]
```

## Use as a git submodule

In the consuming repo:

```bash
git submodule add https://github.com/rbpasker/pasker-net.git vendor/pasker-net
git submodule update --init
```

Then add `vendor/pasker-net/` to `sys.path` early in your entrypoint, or
symlink the `queuing/` directory under your project.

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "vendor" / "pasker-net"))
from queuing import Queue, install_schema
```
