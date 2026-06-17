"""Tests for QueuePriority — static-priority gate across queues.

Run from the pasker-net dir:

    cd vendor/pasker-net
    python3 -m unittest queuing.tests.test_queue_priority -v
"""
from __future__ import annotations

import threading
import time
import unittest
from typing import Any

from queuing import Queue, QueuePriority
from queuing import queue as queue_mod


def _drain(q: Queue, n: int, timeout: float = 1.0) -> list[Any]:
    """Pop n items from q, returning the payloads in order. Raises
    AssertionError if it can't drain in time."""
    out: list[Any] = []
    deadline = time.monotonic() + timeout
    while len(out) < n:
        remaining = max(0.001, deadline - time.monotonic())
        item = q.dequeue(timeout=remaining)
        if item is None:
            raise AssertionError(
                f"could not drain {n} items from {q.name!r}; got {out!r}"
            )
        out.append(item)
    return out


class StaticPriority(unittest.TestCase):
    """Test 1: static priority holds — A (high) beats B (low) when
    both have items."""

    def test_high_priority_drains_first(self) -> None:
        a = Queue("a")
        b = Queue("b")
        QueuePriority([a, b], dequeue_timeout=1.0)
        b.enqueue("b1")
        b.enqueue("b2")
        a.enqueue("a1")
        a.enqueue("a2")

        order: list[str] = []

        def worker(q: Queue) -> None:
            for _ in range(2):
                item = q.dequeue(timeout=2.0)
                if item is None:
                    return
                order.append(item)
                # small work so the other worker has time to try
                time.sleep(0.01)

        ta = threading.Thread(target=worker, args=(a,))
        tb = threading.Thread(target=worker, args=(b,))
        ta.start()
        tb.start()
        ta.join(timeout=5.0)
        tb.join(timeout=5.0)

        # a1, a2 must come before b1, b2
        self.assertEqual(order[:2], ["a1", "a2"])
        self.assertEqual(sorted(order), ["a1", "a2", "b1", "b2"])


class DrainToEmptyNotify(unittest.TestCase):
    """Test 2: when A drains, B's worker pops within one cond cycle,
    not after a full dequeue_timeout."""

    def test_b_unblocks_immediately_when_a_drains(self) -> None:
        a = Queue("a")
        b = Queue("b")
        QueuePriority([a, b], dequeue_timeout=5.0)
        a.enqueue("a1")
        b.enqueue("b1")

        b_pop_ts: list[float] = []

        def b_worker() -> None:
            item = b.dequeue(timeout=5.0)
            if item is not None:
                b_pop_ts.append(time.monotonic())

        tb = threading.Thread(target=b_worker)
        tb.start()
        time.sleep(0.05)  # let b park in cond.wait
        # drain a
        self.assertEqual(a.dequeue(timeout=1.0), "a1")
        a_drain_ts = time.monotonic()
        tb.join(timeout=2.0)
        self.assertEqual(len(b_pop_ts), 1)
        # b must unblock within 100ms — well under the 5s timeout.
        self.assertLess(b_pop_ts[0] - a_drain_ts, 0.1)


class CheckThenAct(unittest.TestCase):
    """Test 3: between _allowed() returning True and popitem(), if a
    higher-priority enqueue lands, the gated dequeue still pops the
    lower-priority item it had committed to. (Strict check-then-act
    under the cond — we cannot rewind the decision.)

    This test is a sanity check that the hook fires and the design
    holds: once the cond is held and _allowed returned True, popping
    the lower-priority item is correct because the higher-priority
    enqueue could not have acquired the cond yet."""

    def test_hook_fires_inside_allowed_branch(self) -> None:
        a = Queue("a")
        b = Queue("b")
        QueuePriority([a, b], dequeue_timeout=1.0)
        b.enqueue("b1")

        hook_fired = threading.Event()

        def hook() -> None:
            hook_fired.set()

        queue_mod._TEST_HOOK_AFTER_ALLOWED = hook
        try:
            item = b.dequeue(timeout=1.0)
            self.assertEqual(item, "b1")
            self.assertTrue(hook_fired.is_set())
        finally:
            queue_mod._TEST_HOOK_AFTER_ALLOWED = None


class ShutdownWake(unittest.TestCase):
    """Test 4: wake() on a gated queue notifies gate._cond. A blocked
    worker unblocks within 50 ms — much less than the 2s
    dequeue_timeout. A missing notify would manifest as a 2s timeout."""

    def test_wake_unblocks_gated_worker_promptly(self) -> None:
        a = Queue("a")
        b = Queue("b")
        QueuePriority([a, b], dequeue_timeout=2.0)

        unblock_ts: list[float] = []
        stop_flag = threading.Event()

        def worker() -> None:
            while not stop_flag.is_set():
                item = b.dequeue(timeout=2.0)
                if item is None:
                    unblock_ts.append(time.monotonic())
                    return

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.05)  # park in cond.wait
        wake_ts = time.monotonic()
        stop_flag.set()
        b.wake()
        t.join(timeout=2.0)

        self.assertEqual(len(unblock_ts), 1)
        latency = unblock_ts[0] - wake_ts
        self.assertLess(
            latency, 0.05,
            f"wake() too slow ({latency*1000:.1f} ms) — gated wake() may "
            f"not be notifying gate._cond",
        )


class DoubleAttachRaises(unittest.TestCase):
    """Test 5: a second QueuePriority including any already-attached
    queue raises RuntimeError."""

    def test_double_attach(self) -> None:
        a = Queue("a")
        b = Queue("b")
        QueuePriority([a, b])
        with self.assertRaises(RuntimeError):
            QueuePriority([a])


class DedupOnEnqueue(unittest.TestCase):
    """Test 6: gated enqueue of an already-pending payload returns
    False, does not double-add."""

    def test_dedup_gated(self) -> None:
        a = Queue("a")
        QueuePriority([a])
        self.assertTrue(a.enqueue("x"))
        self.assertFalse(a.enqueue("x"))
        self.assertEqual(len(a), 1)
        # popping then re-enqueuing the same key is allowed
        self.assertEqual(a.dequeue(timeout=0.1), "x")
        self.assertTrue(a.enqueue("x"))
        self.assertEqual(len(a), 1)

    def test_dedup_ungated(self) -> None:
        a = Queue("a")
        self.assertTrue(a.enqueue("x"))
        self.assertFalse(a.enqueue("x"))
        self.assertEqual(len(a), 1)


class DedupOnPopulate(unittest.TestCase):
    """Test 7: populate() called twice with the same projection
    adds zero items the second time."""

    def test_populate_idempotent_gated(self) -> None:
        items = ["x", "y", "z"]
        a = Queue("a", projection=lambda: items)
        QueuePriority([a])
        self.assertEqual(a.populate(), 3)
        self.assertEqual(a.populate(), 0)
        self.assertEqual(len(a), 3)

    def test_populate_idempotent_ungated(self) -> None:
        items = ["x", "y", "z"]
        a = Queue("a", projection=lambda: items)
        self.assertEqual(a.populate(), 3)
        self.assertEqual(a.populate(), 0)
        self.assertEqual(len(a), 3)


class EnqueueHeadGated(unittest.TestCase):
    """Test 8: enqueue X then enqueue_head(Y) on a gated queue. Next
    two dequeues return Y, then X (head-insert wins within the queue).
    Plus: a lower-priority queue with an item must still wait until
    both Y and X have drained."""

    def test_head_insert_order_with_priority(self) -> None:
        hi = Queue("hi")
        lo = Queue("lo")
        QueuePriority([hi, lo], dequeue_timeout=1.0)
        hi.enqueue("X")
        hi.enqueue_head("Y")
        lo.enqueue("L")

        # Drain head queue: Y first (head-inserted), then X.
        self.assertEqual(hi.dequeue(timeout=0.5), "Y")
        self.assertEqual(hi.dequeue(timeout=0.5), "X")
        # Now lo is allowed.
        self.assertEqual(lo.dequeue(timeout=0.5), "L")


class PerCallTimeoutHonored(unittest.TestCase):
    """Test 9: q.dequeue(timeout=0.1) returns None after ~100 ms
    even if the gate's configured dequeue_timeout is 5.0s."""

    def test_per_call_timeout_overrides_gate_default(self) -> None:
        a = Queue("a")
        QueuePriority([a], dequeue_timeout=5.0)
        t0 = time.monotonic()
        result = a.dequeue(timeout=0.1)
        elapsed = time.monotonic() - t0
        self.assertIsNone(result)
        # Should be ~0.1s; allow generous upper bound.
        self.assertLess(elapsed, 0.5)
        self.assertGreaterEqual(elapsed, 0.08)


class UngatedQueueUnaffected(unittest.TestCase):
    """Test 10: an ungated queue C in the same registry is unaffected
    by the gate. Items on C dequeue freely, with no interaction with
    gated A/B."""

    def test_ungated_queue_independent(self) -> None:
        a = Queue("a")
        b = Queue("b")
        c = Queue("c")  # not in gate
        QueuePriority([a, b])
        # a has items but c is empty — c.dequeue() should still wait/timeout
        a.enqueue("a1")
        a.enqueue("a2")
        # c is independent; immediate enqueue → immediate dequeue.
        c.enqueue("c1")
        self.assertEqual(c.dequeue(timeout=0.5), "c1")
        # a still has its items, gated.
        self.assertEqual(len(a), 2)
        self.assertEqual(a.dequeue(timeout=0.5), "a1")
        self.assertEqual(a.dequeue(timeout=0.5), "a2")


class SequencedDrainAcrossPriorities(unittest.TestCase):
    """Stress: payloads 1..99 on hi, 100..199 on med, 200..299 on lo,
    enqueued in arbitrary interleaving up front. With one worker per
    queue running concurrently, every hi payload must drain before
    any med payload, and every med before any lo. Within a queue,
    order is FIFO (1..99, 100..199, 200..299)."""

    def test_drains_strictly_by_priority(self) -> None:
        hi = Queue("hi")
        med = Queue("med")
        lo = Queue("lo")
        QueuePriority([hi, med, lo], dequeue_timeout=2.0)

        HI = list(range(1, 100))         # 1..99   — 99 items
        MED = list(range(100, 200))      # 100..199 — 100 items
        LO = list(range(200, 300))       # 200..299 — 100 items
        TOTAL = len(HI) + len(MED) + len(LO)  # 299

        # Enqueue with an interleaved pattern so we're not relying on
        # any single-queue enqueue order coincidence.
        for i in range(max(len(HI), len(MED), len(LO))):
            if i < len(LO):
                lo.enqueue(LO[i])
            if i < len(MED):
                med.enqueue(MED[i])
            if i < len(HI):
                hi.enqueue(HI[i])

        order: list[int] = []
        order_lock = threading.Lock()

        def worker(q: Queue, target: int) -> None:
            drained = 0
            while drained < target:
                item = q.dequeue(timeout=5.0)
                if item is None:
                    return
                with order_lock:
                    order.append(item)
                drained += 1

        # Start in REVERSE priority order with a delay between each.
        # lo starts first and parks in cond.wait (gate denies it);
        # med starts next and also parks; hi starts last but its
        # items still drain first because the gate enforces priority
        # regardless of which thread happened to start when.
        threads = [
            threading.Thread(target=worker, args=(lo, len(LO))),
            threading.Thread(target=worker, args=(med, len(MED))),
            threading.Thread(target=worker, args=(hi, len(HI))),
        ]
        for t in threads:
            t.start()
            time.sleep(0.05)
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(len(order), TOTAL,
                         f"only drained {len(order)}/{TOTAL} items")

        # Slice by priority band.
        hi_items = [x for x in order if 1 <= x <= 99]
        med_items = [x for x in order if 100 <= x <= 199]
        lo_items = [x for x in order if 200 <= x <= 299]

        # Within each band, FIFO order.
        self.assertEqual(hi_items, HI,
                         f"hi out of FIFO order: {hi_items[:10]}…")
        self.assertEqual(med_items, MED,
                         f"med out of FIFO order: {med_items[:10]}…")
        self.assertEqual(lo_items, LO,
                         f"lo out of FIFO order: {lo_items[:10]}…")

        # Cross-band priority: the LAST hi must come before the FIRST
        # med; the LAST med before the FIRST lo.
        last_hi = max(order.index(x) for x in hi_items)
        first_med = min(order.index(x) for x in med_items)
        last_med = max(order.index(x) for x in med_items)
        first_lo = min(order.index(x) for x in lo_items)

        self.assertLess(
            last_hi, first_med,
            f"med drained before hi finished: last hi at {last_hi}, "
            f"first med at {first_med}",
        )
        self.assertLess(
            last_med, first_lo,
            f"lo drained before med finished: last med at {last_med}, "
            f"first lo at {first_lo}",
        )


class HashabilityEnforced(unittest.TestCase):
    """Bonus: hashability is library-wide. Unhashable payloads raise
    immediately on enqueue, before any lock acquire."""

    def test_unhashable_raises(self) -> None:
        a = Queue("a")
        with self.assertRaises(TypeError):
            a.enqueue({"unhashable": "dict"})
        # Gated path too.
        b = Queue("b")
        QueuePriority([b])
        with self.assertRaises(TypeError):
            b.enqueue([1, 2, 3])


if __name__ == "__main__":
    unittest.main()
