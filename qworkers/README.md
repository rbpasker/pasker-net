# qworkers

Thread workers that loop on `queuing.Queue` instances. One daemon
thread per `(qname, process_fn)` pair. Workers block in
`Queue.dequeue(timeout=...)` when idle — no polling.

## Worker pattern

```python
import sqlite3
from queuing import QueueRegistry
from qworkers import WorkerSystem

registry = QueueRegistry()
registry.register_from_table(
    sqlite3.connect("app.db"),
    table="q_item",
    queue_names=["ingest", "categorize", "filter", "done", "error"],
    id_column="payload",
)
registry.populate_all()

# Pipeline: ingest -> categorize -> filter -> done

def ingest(entry, conn):
    do_ingest(entry, conn)
    return "categorize"               # next queue name

def categorize(entry, conn):
    conn.execute("BEGIN IMMEDIATE")
    do_categorize(entry, conn)        # writes audit row with q='filter'
    conn.commit()
    return "filter"                   # next queue name

def filter_(entry, conn):
    do_filter(entry, conn)
    return "done"                     # next queue name

def done(entry, conn):
    do_finalize(entry, conn)
    return None                       # terminal

ws = WorkerSystem(
    pairs=[
        ("ingest",     ingest),
        ("categorize", categorize),
        ("filter",     filter_),
        ("done",       done),
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

Two distinct failure paths:

**Transient (retryable):** raise `TransientException` from inside
`process`. The worker catches it specifically, re-enqueues the entry
at the **head** of its source queue, and continues. The next dequeue
on that queue picks the entry up immediately. `error_queue` is NOT
involved. Use this for conditions where the same input is expected to
succeed on a subsequent attempt — DB busy, network blip, rate-limited
downstream, lock contention.

```python
from qworkers import TransientException

def process(entry, conn):
    try:
        do_work(entry, conn)
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            raise TransientException("retry on lock") from e
        raise
    return "next"
```

**Permanent:** any other exception (or a `KeyError` from a
returned-but-unregistered qname) routes the entry to the configured
`error_queue` and the worker continues. `error_queue` must be
pre-registered in the registry before `WorkerSystem` is constructed —
the constructor raises `ValueError` otherwise. Typically the consumer
registers it with its own projection so error entries are recoverable
across restarts.

If `error_queue` is `None`, permanent exceptions are logged and the
entry is dropped from this run (the observer sees a "drop" event);
recovery happens at the next process restart via `populate()`.

Process functions must be idempotent: running them twice with the
same `entry` must produce the same final state.

## Observability

Pass an `observer` callable to the constructor to receive a callback
for every worker-driven event:

| event        | meaning                                                                                       |
|--------------|-----------------------------------------------------------------------------------------------|
| `"deq"`      | worker dequeued `item` from `qname`                                                           |
| `"enq"`      | worker enqueued `item` to `qname` tail (next-queue, fan-out, or error_queue)                  |
| `"enq_head"` | worker re-enqueued `item` at `qname`'s head after process raised `TransientException`         |
| `"drop"`     | `item` was dropped after process raised; no error_queue was set; `qname` is the source queue  |

Signature:

```python
ObserverFn = Callable[[str, str, Any], None]  # event, qname, item
```

`LoggerObserver` is a ready-made observer that logs each event via
Python logging:

```python
from qworkers import LoggerObserver, WorkerSystem
ws = WorkerSystem(..., observer=LoggerObserver())
```

Customize logger and level:

```python
ws = WorkerSystem(..., observer=LoggerObserver(
    logger=logging.getLogger("myapp.queues"),
    level=logging.DEBUG,
))
```

Direct `Queue.enqueue` calls outside the worker loop (e.g., when a
consumer seeds the pipeline) are NOT observed automatically — call the
observer by hand at those sites if you want them logged.

Observer exceptions are caught and logged; they never disrupt the
worker.

## Per-worker stats

Independent of the observer, `WorkerSystem` keeps a `WorkerStats` record
per `(qname, index)` pair — cumulative counters plus a live busy flag —
for dashboards. Read a snapshot at any time:

```python
for (qname, index), st in ws.worker_stats().items():
    print(qname, index,
          "busy" if st.working else "idle",
          st.items_serviced(), f"{st.service_avg_s():.3f}s avg")
```

`worker_stats()` returns a shallow copy of the dict (safe to iterate),
but each `WorkerStats` value is live by reference — its fields may tick
forward while you read them, which is fine for a dashboard.

Each record carries:

| field                      | meaning                                                                                 |
|----------------------------|-----------------------------------------------------------------------------------------|
| `success_count`            | `process()` returned a routing target (next queue, fan-out, or terminal `None`)         |
| `permanent_failure_count`  | `process()` raised a non-Transient exception, or returned `error_queue` as its target   |
| `transient_failure_count`  | `process()` raised `TransientException` (entry head-requeued for immediate retry)       |
| `service_total_s`          | cumulative wall time inside `process()` across all three outcomes                        |
| `working`                  | `True` only while this worker is currently inside a `process()` call (instantaneous)     |

with `items_serviced()` (sum of the three outcome counters) and
`service_avg_s()` (`service_total_s / items_serviced`). One bump per
`process()` attempt — `TransientException` retries are counted too, since
they consumed worker time and signal flakiness.

This is the worker-side complement to `queuing`'s queue-side
`Queue.snapshot()` / `QueueRegistry.snapshot()`: queue stats show backlog
and wait time, worker stats show throughput and service time.

## Sample app

`qworkers/tests/sample_app.py` runs an N-stage pipeline with M
messages, random per-item work times (0.01–3.0 s), and a configurable
per-stage failure probability:

```bash
python -m qworkers.tests.sample_app                                # 5 queues, 20 messages
python -m qworkers.tests.sample_app -q 8 -m 50                     # 8 queues, 50 messages
python -m qworkers.tests.sample_app -q 3 -m 5 --fail-prob 0.3
```

Output is a stream of enq/deq/drop events from `LoggerObserver` plus
a final tally.

## API reference

```python
ProcessFn = Callable[[Any, Any], str | list[str] | None]
#   (entry, conn) -> next qname | list of next qnames | terminal

ObserverFn = Callable[[str, str, Any], None]
#   (event, qname, item) -> None   # event in {"deq","enq","enq_head","drop"}

@dataclass
class WorkerStats:
    success_count: int = 0
    permanent_failure_count: int = 0
    transient_failure_count: int = 0
    service_total_s: float = 0.0
    working: bool = False
    def items_serviced() -> int          # sum of the three outcome counters
    def service_avg_s() -> float         # service_total_s / items_serviced

class WorkerSystem:
    def __init__(
        pairs: list[tuple[str, ProcessFn]],
        *,
        registry: QueueRegistry | None = None,
        conn_factory: Callable[[], Any] | None = None,
        error_queue: str | None = None,
        observer: ObserverFn | None = None,
        wait_timeout: float = 1.0,
    )
    registry: QueueRegistry        # read-only property

    def start() -> None
    def stop(*, join_timeout: float | None = None) -> None
    def join(*, timeout: float | None = None) -> None
    def worker_stats() -> dict[tuple[str, int], WorkerStats]
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
