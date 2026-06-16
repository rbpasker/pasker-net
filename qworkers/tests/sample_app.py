"""Sample app: M messages bounce between N randomly-chosen queues
until they all fail and land in the error queue.

Run from the pasker-net repo root:

    python -m qworkers.tests.sample_app
    python -m qworkers.tests.sample_app --queues 8 --messages 50
    python -m qworkers.tests.sample_app -q 3 -m 5

Topology:

    q0, q1, …, q(N-1)        every process picks a random next queue
              ↘  error        crashed items end up here, terminal

Each non-error process sleeps a random WORK_LOW..WORK_HIGH seconds,
then crashes periodically: each worker thread picks an independent
crash interval in
    [((WORK_HIGH - WORK_LOW) / 2) * 0.9,
     ((WORK_HIGH - WORK_LOW) / 2) * 1.1]
seconds, and the first process call past that interval raises
RuntimeError (and the timer resets for the next cycle).

The system terminates when all M messages have been routed to the
error queue.

`LoggerObserver` logs every enq / deq event.
"""
from __future__ import annotations

import argparse
import logging
import random
import threading
import time

from queuing import QueueRegistry
from qworkers import LoggerObserver, WorkerSystem


WORK_LOW = 0.01
WORK_HIGH = 3.0


def _work() -> None:
    time.sleep(random.uniform(WORK_LOW, WORK_HIGH))


class _CrashTimer:
    """Per-thread periodic-crash schedule.

    Each worker thread keeps its own next-crash deadline (via
    `threading.local`). The first call seeds the deadline; subsequent
    calls past the deadline raise RuntimeError and reset for the next
    cycle. Interval = uniform(mid * 0.9, mid * 1.1) where
    mid = (WORK_HIGH - WORK_LOW) / 2.
    """

    def __init__(self) -> None:
        mid = (WORK_HIGH - WORK_LOW) / 2.0
        self._low = mid * 0.9
        self._high = mid * 1.1
        self._state = threading.local()

    def maybe_crash(self) -> None:
        now = time.monotonic()
        deadline = getattr(self._state, "next", None)
        if deadline is None or now >= deadline:
            self._state.next = now + random.uniform(self._low, self._high)
            if deadline is not None:
                raise RuntimeError("periodic worker crash")


def _make_process(qnames: list[str], crash_timer: _CrashTimer):
    """Build a process fn that does random work, may crash periodically,
    and otherwise routes to a random queue picked from `qnames`."""
    def process(entry: str, conn) -> str:
        _work()
        crash_timer.maybe_crash()
        return random.choice(qnames)
    return process


def main() -> None:
    parser = argparse.ArgumentParser(description="qworkers sample app")
    parser.add_argument("--queues", "-q", type=int, default=5,
                        help="number of queues (default: 5)")
    parser.add_argument("--messages", "-m", type=int, default=20,
                        help="number of messages to seed (default: 20)")
    args = parser.parse_args()

    if args.queues < 1 or args.messages < 1:
        parser.error("queues and messages must be >= 1")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d  %(threadName)-20s  %(message)s",
        datefmt="%H:%M:%S",
    )

    N = args.queues
    M = args.messages
    qnames = [f"q{i}" for i in range(N)]

    reg = QueueRegistry()
    reg.register("error")

    done = threading.Event()
    errored: list[str] = []
    errored_lock = threading.Lock()

    def collect_errors(entry: str, conn) -> None:
        with errored_lock:
            errored.append(entry)
            if len(errored) >= M:
                done.set()
        return None

    crash_timer = _CrashTimer()
    pairs: list[tuple[str, "callable"]] = [
        (name, _make_process(qnames, crash_timer)) for name in qnames
    ]
    pairs.append(("error", collect_errors))

    observer = LoggerObserver()

    mid = (WORK_HIGH - WORK_LOW) / 2.0
    logging.info(
        "starting: %d worker queues, %d messages "
        "(work=%.2f..%.2fs, crash every ~%.2f-%.2fs per worker)",
        N, M, WORK_LOW, WORK_HIGH, mid * 0.9, mid * 1.1,
    )
    with WorkerSystem(
        pairs=pairs,
        registry=reg,
        observer=observer,
        error_queue="error",
    ) as ws:
        # Seed M messages onto random starting queues.
        for i in range(M):
            item = f"msg-{i:03d}"
            start_q = random.choice(qnames)
            reg.get(start_q).enqueue(item)
            observer("enq", start_q, item)
        done.wait(timeout=600.0)

    logging.info("DONE: all %d messages routed to error queue", len(errored))


if __name__ == "__main__":
    main()
