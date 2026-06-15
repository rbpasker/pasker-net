"""SQL-backed named FIFO queues that share the consuming app's database
and the consuming app's transactions.

Design — three properties carry the abstraction:

1. **One table, all queues.** Items live in a single `q_item` table
   discriminated by the `q` column. Adding a queue costs zero DDL — the
   first `push` with a new `q` value is the queue's birth certificate.
   `SELECT q, COUNT(*) FROM q_item GROUP BY q` is the registry.

2. **Every method takes the caller's connection.** The queue's INSERTs,
   UPDATEs, and DELETEs happen on the connection the caller passes in,
   inside whatever transaction the caller has open. The worker pattern is:

       with conn:                                 # SQLite txn
           payload = q.peek(conn)                 # SELECT + UPDATE locked_at
           if payload is None: return
           do_work(payload, conn)                 # worker writes audit/state
           q.ack(conn, payload)                   # DELETE
       # commit fires here; queue mutation + worker writes are atomic

   No drift possible between "queue says done" and "audit says done" —
   either both committed or neither did.

3. **The projection is the boot-recovery contract, not the runtime
   contract.** At runtime, items move via push/peek/ack. At boot (or as a
   background self-heal sweep), `repopulate(conn, projection=fn)` reads
   the consuming app's durable audit state via `fn`, inserts anything the
   projection says should be in the queue but isn't, and (optionally)
   clears stale locks. The audit tables are the source of truth; the
   queue is the working set.

What this is NOT: a broker. There is no cross-process delivery, no
across-restart visibility timeout, no built-in retry policy. If you need
those, replace this library with one that wraps Redis Streams / NATS
JetStream / RabbitMQ behind the same `Queue` interface — the consumer
code does not change.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any


# ---------------------------- DDL ------------------------------------- #

SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS q_item (
        seq        INTEGER PRIMARY KEY AUTOINCREMENT,
        q          TEXT NOT NULL,
        payload    TEXT NOT NULL,
        locked_at  TEXT,                          -- NULL = visible; ISO ts = taken
        UNIQUE(q, payload)
    );
    """,
    # Partial index over visible items only — the hot path for peek().
    # WHERE locked_at IS NULL keeps the index small (locked rows excluded),
    # so an empty queue is one B-tree leaf read.
    """
    CREATE INDEX IF NOT EXISTS q_item_visible
        ON q_item(q, seq) WHERE locked_at IS NULL;
    """,
)


def install_schema(conn: sqlite3.Connection) -> None:
    """Create the q_item table and its index if they don't exist. Call
    once at app startup. Idempotent; uses IF NOT EXISTS throughout."""
    for stmt in SCHEMA_DDL:
        conn.execute(stmt)


# ---------------------------- Queue ----------------------------------- #


def _now_iso() -> str:
    """ISO 8601 UTC stamp, second precision. Used as the locked_at marker."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Queue:
    """A named FIFO queue backed by the q_item table.

    The queue object is a thin handle — it carries the queue name and
    provides methods that operate on a caller-supplied connection. The
    queue is cheap to construct (just stores the name); construct one
    per logical queue at startup or inline at each use site.
    """

    def __init__(self, name: str) -> None:
        if not name or not isinstance(name, str):
            raise ValueError(f"queue name must be a non-empty string, got {name!r}")
        self.name = name

    # ----- producer side -------------------------------------------------

    def push(self, conn: sqlite3.Connection, payload: str) -> bool:
        """Insert `payload` at the tail of this queue. Idempotent against
        duplicates (UNIQUE(q, payload) — re-pushing the same payload is a
        no-op). Returns True if the row was inserted, False if it was
        already present (visible or in-flight).

        Participates in the caller's transaction: if the caller's
        transaction rolls back, the push is undone."""
        cur = conn.execute(
            "INSERT OR IGNORE INTO q_item (q, payload) VALUES (?, ?)",
            (self.name, payload),
        )
        return cur.rowcount > 0

    def extend(self, conn: sqlite3.Connection, payloads: Iterable[str]) -> int:
        """Bulk push. Returns the number of newly inserted rows (existing
        ones are silently skipped via the UNIQUE constraint)."""
        rows = [(self.name, p) for p in payloads]
        if not rows:
            return 0
        cur = conn.executemany(
            "INSERT OR IGNORE INTO q_item (q, payload) VALUES (?, ?)",
            rows,
        )
        return cur.rowcount

    # ----- consumer side -------------------------------------------------

    def peek(self, conn: sqlite3.Connection) -> str | None:
        """Take the oldest visible item and mark it in-flight by setting
        `locked_at` to now. Returns the payload or None if the queue is
        empty.

        Participates in the caller's transaction. If the caller's
        transaction commits, the lock persists across the gap until
        ack/nack. If it rolls back (worker crashed, exception was raised),
        the lock disappears with the rollback and the item is immediately
        visible to the next peek.

        For SQLite multi-writer correctness, the caller's transaction
        should be opened with `BEGIN IMMEDIATE` so the SELECT + UPDATE
        pair serializes correctly against concurrent peekers. With
        deferred BEGIN, two peekers could both read the same row before
        either UPDATEs."""
        row = conn.execute(
            "SELECT seq, payload FROM q_item "
            "WHERE q = ? AND locked_at IS NULL "
            "ORDER BY seq LIMIT 1",
            (self.name,),
        ).fetchone()
        if row is None:
            return None
        seq, payload = row
        conn.execute(
            "UPDATE q_item SET locked_at = ? WHERE seq = ?",
            (_now_iso(), seq),
        )
        return payload

    def ack(self, conn: sqlite3.Connection, payload: str) -> bool:
        """Confirm work is done — delete the row. Returns True if the
        row was present, False if it was already gone (idempotent).

        Participates in the caller's transaction: the delete commits or
        rolls back with the worker's other writes."""
        cur = conn.execute(
            "DELETE FROM q_item WHERE q = ? AND payload = ?",
            (self.name, payload),
        )
        return cur.rowcount > 0

    def nack(self, conn: sqlite3.Connection, payload: str) -> bool:
        """Release the lock without consuming — item becomes visible
        again. Use when the worker decided not to handle this item (not
        when the worker crashed; rollback handles that automatically).

        Returns True if a locked row was released, False otherwise."""
        cur = conn.execute(
            "UPDATE q_item SET locked_at = NULL "
            "WHERE q = ? AND payload = ? AND locked_at IS NOT NULL",
            (self.name, payload),
        )
        return cur.rowcount > 0

    # ----- projection / self-heal ----------------------------------------

    def repopulate(
        self,
        conn: sqlite3.Connection,
        projection: Callable[[], Iterable[str]],
        *,
        clear_stale_locks: bool = False,
    ) -> int:
        """Run a projection callable and insert any items it returns that
        are not already in this queue (visible or locked).

        Use at boot to derive queue contents from the consuming app's
        durable audit state. Run periodically as a self-heal sweep to
        patch up any items the projection says should be queued but
        aren't (e.g., a worker crashed before pushing the next item,
        leaving the audit committed but the next queue empty).

        `clear_stale_locks=True` additionally clears `locked_at` on every
        row of this queue — use only at boot, when no in-flight work can
        possibly be alive. NEVER set this true during steady-state
        operation; it will yank items out from under live workers.

        Returns the number of items inserted by this call.

        Participates in the caller's transaction."""
        if clear_stale_locks:
            conn.execute(
                "UPDATE q_item SET locked_at = NULL WHERE q = ?",
                (self.name,),
            )
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT payload FROM q_item WHERE q = ?",
                (self.name,),
            )
        }
        new_rows = [
            (self.name, item)
            for item in projection()
            if item not in existing
        ]
        if not new_rows:
            return 0
        cur = conn.executemany(
            "INSERT OR IGNORE INTO q_item (q, payload) VALUES (?, ?)",
            new_rows,
        )
        return cur.rowcount

    # ----- inspection ----------------------------------------------------

    def pending_count(self, conn: sqlite3.Connection) -> int:
        """Number of visible (unlocked) items waiting on this queue."""
        return conn.execute(
            "SELECT COUNT(*) FROM q_item WHERE q = ? AND locked_at IS NULL",
            (self.name,),
        ).fetchone()[0]

    def inflight_count(self, conn: sqlite3.Connection) -> int:
        """Number of items currently locked (in-flight)."""
        return conn.execute(
            "SELECT COUNT(*) FROM q_item WHERE q = ? AND locked_at IS NOT NULL",
            (self.name,),
        ).fetchone()[0]

    def snapshot(
        self, conn: sqlite3.Connection,
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Return (pending_payloads, inflight_payloads_with_lock_time).
        For dashboards / diagnostics only; do not use to drive control
        flow. Each return is a freshly built list — caller may mutate
        without affecting database state."""
        pending = [
            row[0]
            for row in conn.execute(
                "SELECT payload FROM q_item "
                "WHERE q = ? AND locked_at IS NULL ORDER BY seq",
                (self.name,),
            )
        ]
        inflight = [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT payload, locked_at FROM q_item "
                "WHERE q = ? AND locked_at IS NOT NULL ORDER BY seq",
                (self.name,),
            )
        ]
        return pending, inflight


# ---------------------------- Registry -------------------------------- #


def queue_names(conn: sqlite3.Connection) -> list[str]:
    """Return every queue name that has at least one row right now.
    Derived from the data; no separate registry table is maintained."""
    return [
        row[0]
        for row in conn.execute("SELECT DISTINCT q FROM q_item ORDER BY q")
    ]


def queue_depths(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """One-shot overview: `{queue_name: {"pending": N, "inflight": M}}`
    for every queue with any rows. Use in dashboards / health checks."""
    out: dict[str, dict[str, int]] = {}
    rows = conn.execute(
        "SELECT q, "
        "       SUM(CASE WHEN locked_at IS NULL THEN 1 ELSE 0 END) AS pending, "
        "       SUM(CASE WHEN locked_at IS NOT NULL THEN 1 ELSE 0 END) AS inflight "
        "FROM q_item GROUP BY q ORDER BY q"
    )
    for q, pending, inflight in rows:
        out[q] = {"pending": int(pending or 0), "inflight": int(inflight or 0)}
    return out


# ---------------------------- Public API ------------------------------ #

__all__ = [
    "Queue",
    "SCHEMA_DDL",
    "install_schema",
    "queue_names",
    "queue_depths",
]
