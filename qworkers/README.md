# qworkers

Thread workers that loop on `queuing.Queue` instances, calling a
per-queue process function for every dequeued item. One daemon thread
per `(qname, process_fn)` pair. Workers block on `Queue.wait_for_item()`
when idle — no polling.

## Worker pattern

```python
import sqlite3
from queuing import QueueRegistry
from qworkers import WorkerSystem

registry = QueueRegistry()
registry.register_from_table(
    sqlite3.connect("app.db"),
    table="q_item",
    queue_names=["categorize", "request", "plan"],
    id_column="payload",
)
registry.populate_all()

def categorize(entry, conn, registry):
    conn.execute("BEGIN IMMEDIATE")
    do_categorize(entry, conn)              # writes audit row with q='request'
    conn.commit()
    registry.get("request").enqueue(entry)  # notification

def request(entry, conn, registry):
    ...

ws = WorkerSystem(
    pairs=[
        ("categorize", categorize),
        ("request",    request),
    ],
    registry=registry,
    conn_factory=lambda: sqlite3.connect("app.db", isolation_level=None),
)

with ws:
    main_loop_or_wait_for_signal()
```

## Worker pools

To run N workers on the same queue, repeat the pair:

```python
ws = WorkerSystem(
    pairs=[
        ("categorize", categorize),
        ("research",   agent_process),
        ("research",   agent_process),     # 2nd research worker
        ("research",   agent_process),     # 3rd research worker
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

## Error semantics

If `process(entry, conn, registry)` raises, the exception is logged
and the worker continues to the next item. The consumer's transaction
(if any) rolled back, so the item is dropped from this run; recovery
happens at the next process restart via `populate_all()`. For
intra-run retry, have `process` catch the failure and re-enqueue.

Process functions must be idempotent: running them twice with the
same `q_entry` must produce the same final state.

## API reference

```python
ProcessFn = Callable[[Any, Any, QueueRegistry], None]

class WorkerSystem:
    def __init__(
        pairs: list[tuple[str, ProcessFn]],
        *,
        registry: QueueRegistry | None = None,
        conn_factory: Callable[[], Any] | None = None,
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
