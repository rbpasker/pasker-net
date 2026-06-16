# qworkers

Thread workers that loop on `queuing.Queue` instances, calling a
per-queue process function for every item dequeued. One daemon thread
per `(qname, process_fn)` pair. Block-on-event when the queue is empty
— no polling, no CPU burn while idle.

## Use case

You've defined some named queues via `queuing`. You want one (or more)
worker thread per queue, each calling a process function on each item.
You want the workers to:

- block efficiently when their queue is empty (`Queue.wait_for_item` +
  bounded-timeout for stop-checks),
- exit cleanly on shutdown,
- support multiple workers on the same queue (pool, e.g. an agent
  pool) via repeated `(qname, process)` pairs,
- log unhandled exceptions in `process` without killing the worker.

That's all `qworkers` does. It does not own queues (those live in
`queuing.QueueRegistry`), does not own database connections (you
provide a `conn_factory`), and does not enforce any retry semantics
(the consumer's at-least-once recovery via the projection in
`queuing` handles that).

## Worker pattern

```python
import sqlite3
from queuing import QueueRegistry
from qworkers import WorkerSystem

# 1. Build a registry (typically via QueueRegistry.register_from_table or
#    by hand). Optionally repopulate from a projection at boot.
registry = QueueRegistry()
registry.register_from_table(
    sqlite3.connect("app.db"),
    table="q_item",
    queue_names=["categorize", "request", "plan"],
    id_column="payload",
)
registry.repopulate_all()                       # boot recovery

# 2. Define a process function per queue. Signature is fixed:
#    process(q_entry, conn, registry) -> None
def categorize(entry, conn, registry):
    conn.execute("BEGIN IMMEDIATE")
    do_categorize(entry, conn)                  # writes audit row with q='request'
    conn.commit()
    registry.get("request").enqueue(entry)      # latency notification

def request(entry, conn, registry):
    ...

# 3. Build and run the WorkerSystem.
ws = WorkerSystem(
    pairs=[
        ("categorize", categorize),
        ("request",    request),
    ],
    registry=registry,
    conn_factory=lambda: sqlite3.connect("app.db", isolation_level=None),
)

with ws:
    # Workers run in background daemon threads.
    # … your main thread does something else, or just waits …
    main_loop_or_wait_for_signal()
# Exiting the context calls stop() and joins workers with a timeout.
```

## Worker pools (multiple workers per queue)

To run N workers on the same queue (e.g. an agent pool), repeat the
pair:

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

Each thread gets its own DB connection from `conn_factory`. They
compete for items in the shared in-memory deque; `Queue.dequeue()`
is atomic, so no item is delivered twice. SQLite's single-writer
constraint serializes the per-thread transactions naturally; for
heavier concurrency, swap the underlying queue for a real broker
behind the same `Queue` interface.

## Lifecycle

`WorkerSystem` exposes a standard threading lifecycle plus a context
manager:

```python
ws = WorkerSystem(pairs, registry=..., conn_factory=...)
ws.start()                  # spawn daemon threads
# ... do other work ...
ws.stop(join_timeout=5.0)   # set stop flag, wake workers, join up to 5s each
ws.join()                   # wait indefinitely for remaining stragglers

# or as a context manager (start on enter, stop+join on exit):
with WorkerSystem(pairs, registry=..., conn_factory=...) as ws:
    ...
```

`stop()` calls `wake()` on every distinct queue name so workers
blocked in `wait_for_item` return promptly and observe the stop
flag. The default `wait_timeout` of 1.0s caps how long an idle
worker can sit before re-checking the stop flag — adjust via the
`wait_timeout` constructor arg.

## Error semantics

If `process(entry, conn, registry)` raises, the exception is logged
and the worker continues to the next item. The consumer's audit
transaction was never committed (it rolled back when the exception
unwound the `with conn:` block in process), so the on-disk state
still shows this item as belonging to its source queue. The next
`Queue.repopulate()` call — at boot or as a self-heal sweep — will
re-derive it from the projection, and another worker pass will
re-attempt the work.

This is **at-least-once delivery**. Process functions must be
idempotent: running them twice with the same `q_entry` must produce
the same final state. Standard audit-pipeline discipline (ON CONFLICT
keys, monotonic status state machines) makes this true by default;
make sure your process functions don't violate it.

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

In the consuming repo:

```bash
git submodule add https://github.com/rbpasker/pasker-net.git vendor/pasker-net
git submodule update --init
```

Then add `vendor/pasker-net/` to `sys.path` early in your entrypoint,
or symlink the `qworkers/` (and `queuing/`) directories under your
project.

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "vendor" / "pasker-net"))
from queuing import QueueRegistry
from qworkers import WorkerSystem
```
