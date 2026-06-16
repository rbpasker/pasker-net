# qworkers

Thread workers that loop on `queuing.Queue` instances. One daemon
thread per `(qname, process_fn)` pair. Workers block on
`Queue.wait_for_item()` when idle — no polling.

## Worker pattern

```python
import sqlite3
from queuing import QueueRegistry
from qworkers import WorkerSystem

registry = QueueRegistry()
registry.register_from_table(
    sqlite3.connect("app.db"),
    table="q_item",
    queue_names=["categorize", "request", "plan", "error"],
    id_column="payload",
)
registry.populate_all()

def categorize(entry, conn):
    conn.execute("BEGIN IMMEDIATE")
    do_categorize(entry, conn)        # writes audit row with q='request'
    conn.commit()
    return "request"                  # next queue name

def request(entry, conn):
    do_request(entry, conn)
    return None                       # terminal

ws = WorkerSystem(
    pairs=[
        ("categorize", categorize),
        ("request",    request),
    ],
    registry=registry,
    conn_factory=lambda: sqlite3.connect("app.db", isolation_level=None),
    error_queue="error",
)

with ws:
    main_loop_or_wait_for_signal()
```

`process(entry, conn)` returns:

- `str` — name of the next queue. Worker enqueues the entry there.
- `list[str]` — fan-out to multiple queues. The worker enqueues to
  each. Durability of fan-out is the consumer's responsibility: if
  your storage uses a single-value `q` column, only one branch
  survives a process restart. Multi-branch durability needs either a
  JSON-list `q` column or a separate `routes(instruction_id, q)` table
  with a matching projection.
- `None` — terminal. Nothing to do.

The order of operations inside process: commit the durable state
first (which updates the on-disk `q`), then return the next-queue
name. The worker's `enqueue` is a notification AFTER commit.

## Worker pools

To run N workers on the same queue, repeat the pair:

```python
ws = WorkerSystem(
    pairs=[
        ("categorize", categorize),
        ("research",   agent_process),
        ("research",   agent_process),    # 2nd research worker
        ("research",   agent_process),    # 3rd research worker
    ],
    registry=registry,
    conn_factory=lambda: sqlite3.connect("app.db", isolation_level=None),
)
```

Each thread gets its own connection from `conn_factory`. They compete
for items in the shared deque; `Queue.dequeue()` is atomic.

## Lifecycle

```python
ws = WorkerSystem(pairs, registry=..., conn_factory=...)
ws.start()
# ... do other work ...
ws.stop(join_timeout=5.0)
ws.join()

# or as a context manager (start on enter, stop+join on exit):
with WorkerSystem(pairs, registry=..., conn_factory=...) as ws:
    ...
```

`stop()` calls `wake()` on every distinct queue name so blocked
workers return promptly and observe the stop flag. `wait_timeout`
(default 1.0s) caps how long an idle worker can sit before
re-checking the stop flag.

## Error handling

If `process(entry, conn)` raises (or the returned qname isn't in the
registry — a `KeyError` from `registry.get`), the worker logs the
exception and routes the entry to the configured `error_queue`. The
worker then continues to the next item.

`error_queue` must be pre-registered in the registry before
`WorkerSystem` is constructed — the constructor raises `ValueError`
if it isn't. Typically the consumer registers it with its own
projection so error entries are recoverable across restarts.

If `error_queue` is `None`, exceptions are logged and the entry is
dropped from this run; recovery happens at the next process restart
via `populate()`.

Process functions must be idempotent: running them twice with the
same `entry` must produce the same final state.

## API reference

```python
ProcessFn = Callable[[Any, Any], str | list[str] | None]
#   (entry, conn) -> next qname | list of next qnames | terminal

class WorkerSystem:
    def __init__(
        pairs: list[tuple[str, ProcessFn]],
        *,
        registry: QueueRegistry | None = None,
        conn_factory: Callable[[], Any] | None = None,
        error_queue: str | None = None,
        wait_timeout: float = 1.0,
    )
    registry: QueueRegistry        # read-only property

    def start() -> None
    def stop(*, join_timeout: float | None = None) -> None
    def join(*, timeout: float | None = None) -> None
    def __enter__() -> WorkerSystem
    def __exit__(exc_type, exc_val, exc_tb) -> bool
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
from queuing import QueueRegistry
from qworkers import WorkerSystem
```
