"""Tests for Stats min/max tracking and snapshot triple format."""
from __future__ import annotations

import unittest

from queuing import Queue, Stats


class StatsMinMaxTracking(unittest.TestCase):
    def test_add_tracks_min_and_max(self) -> None:
        s = Stats()
        s.add("foo", 5.0)
        s.add("foo", 2.0)
        s.add("foo", 10.0)
        s.add("foo", 7.0)
        snap = s.snapshot()
        # (sum, min, max) of the increments: sum=24, min=2, max=10
        self.assertEqual(snap["foo"], (24.0, 2.0, 10.0))

    def test_inc_tracks_unit_increments(self) -> None:
        s = Stats()
        for _ in range(5):
            s.inc("count")
        snap = s.snapshot()
        # inc always adds 1, so min=max=1.
        self.assertEqual(snap["count"], (5, 1, 1))

    def test_dec_tracks_negative_increments(self) -> None:
        s = Stats()
        s.inc("x")
        s.dec("x")
        snap = s.snapshot()
        # value: 1 + (-1) = 0; min=-1, max=1
        self.assertEqual(snap["x"], (0, -1, 1))

    def test_sub_records_negated_n(self) -> None:
        s = Stats()
        s.add("x", 5.0)
        s.sub("x", 3.0)
        snap = s.snapshot()
        # value: 5 + (-3) = 2; min=-3, max=5
        self.assertEqual(snap["x"], (2.0, -3.0, 5.0))

    def test_set_clears_tracking(self) -> None:
        s = Stats()
        s.add("foo", 5.0)
        s.add("foo", 10.0)
        # First snapshot: tracked triple.
        self.assertEqual(s.snapshot()["foo"], (15.0, 5.0, 10.0))
        # Set overwrites; tracking is cleared.
        s.set("foo", 100.0)
        self.assertEqual(s.snapshot()["foo"], 100.0)
        # New increments after set start fresh tracking.
        s.add("foo", 1.0)
        self.assertEqual(s.snapshot()["foo"], (101.0, 1.0, 1.0))

    def test_preseeded_keys_are_scalar_until_touched(self) -> None:
        s = Stats({"a": 0, "b": 0.0})
        snap = s.snapshot()
        # Pre-seeded keys haven't been tracked yet → scalar.
        self.assertEqual(snap["a"], 0)
        self.assertEqual(snap["b"], 0.0)
        # Touch one; it becomes a triple.
        s.inc("a")
        snap = s.snapshot()
        self.assertEqual(snap["a"], (1, 1, 1))
        self.assertEqual(snap["b"], 0.0)

    def test_snapshot_returns_fresh_copy(self) -> None:
        s = Stats()
        s.add("foo", 1.0)
        snap1 = s.snapshot()
        s.add("foo", 100.0)
        snap2 = s.snapshot()
        self.assertNotEqual(snap1["foo"], snap2["foo"])
        self.assertEqual(snap1["foo"], (1.0, 1.0, 1.0))
        self.assertEqual(snap2["foo"], (101.0, 1.0, 100.0))

    def test_set_then_inc_raises_on_non_numeric(self) -> None:
        s = Stats()
        s.set("k", "string")
        with self.assertRaises(TypeError):
            s.inc("k")


class QueueSnapshotFlatScalars(unittest.TestCase):
    """Queue.snapshot() unpacks triples back into scalars (for
    backward-compat with existing dashboards) and adds *_min/*_max
    sibling keys."""

    def test_wait_min_max_exposed(self) -> None:
        import time
        q = Queue("q")
        q.enqueue("a")
        # Tiny sleep so wait_total_s isn't zero.
        time.sleep(0.01)
        q.dequeue(timeout=0.1)
        q.enqueue("b")
        time.sleep(0.05)
        q.dequeue(timeout=0.1)

        snap = q.snapshot()
        # Sum semantics preserved.
        self.assertEqual(snap["enq"], 2)
        self.assertEqual(snap["deq"], 2)
        self.assertEqual(snap["depth"], 0)
        # Min/max exposed.
        self.assertIn("wait_min_s", snap)
        self.assertIn("wait_max_s", snap)
        self.assertLessEqual(snap["wait_min_s"], snap["wait_max_s"])
        self.assertGreater(snap["wait_max_s"], 0.0)

    def test_user_added_key_gets_min_max_siblings(self) -> None:
        q = Queue("q")
        q.stats.add("tokens", 100)
        q.stats.add("tokens", 250)
        q.stats.add("tokens", 50)
        snap = q.snapshot()
        self.assertEqual(snap["tokens"], 400)
        self.assertEqual(snap["tokens_min"], 50)
        self.assertEqual(snap["tokens_max"], 250)

    def test_empty_queue_has_no_min_max(self) -> None:
        q = Queue("q")
        snap = q.snapshot()
        # No deqs yet; min/max should be ABSENT, not 0.0. A fabricated
        # 0.0 would be indistinguishable from a real measurement.
        self.assertNotIn("wait_min_s", snap)
        self.assertNotIn("wait_max_s", snap)


if __name__ == "__main__":
    unittest.main()
