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

## API reference

```python
class Queue:
    name: str

    def __init__(name: str,
                 projection: Callable[[], Iterable] | None = None)
    def enqueue(item) -> bool
    def dequeue() -> item | None
    def wait_for_item(timeout: float | None = None) -> bool
    def wake() -> None
    def populate(projection: Callable | None = None) -> int
    def stats() -> tuple[int, int, int]   # (enq, deq, depth)
    def snapshot() -> list
    def __len__() -> int

class QueueRegistry:
    def register(name: str, projection: Callable | None = None) -> Queue
    def get(name: str) -> Queue                  # KeyError on unknown name
    def __contains__(name: str) -> bool
    def names() -> list[str]
    def populate_all() -> dict[str, int]
    def depths() -> dict[str, int]
    def stats() -> dict[str, tuple[int, int, int]]    # per-queue (enq, deq, depth)
    def register_from_table(
        conn_or_factory, table: str, queue_names: Iterable[str], *,
        q_column: str = "q_name", id_column: str = "id",
        where_extra: str | None = None,
    ) -> list[Queue]
    def detect_orphans(
        conn_or_factory, table: str, *, q_column: str = "q_name",
    ) -> list[str]
```

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
