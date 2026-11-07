"""The build loop's queue feeder (tools/build-loop/queue_next.py)."""
import os
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent.parent / "tests"))

import queue_next  # noqa: E402
from test_harness import Base  # noqa: E402


class SelfDrivingBuildLoop(Base):
    """The gap that made the harness ceremonial: when a usage window ran out
    mid-build, scheduled tasks resumed and the build did not, because nothing
    queued the next item."""

    def _queue(self, text):
        (self.dir / "NEXT.md").write_text(text)
        os.environ["CURATED_DIGEST_QUEUE"] = str(self.dir / "NEXT.md")
        self.addCleanup(os.environ.pop, "CURATED_DIGEST_QUEUE", None)

    def _submit(self, c):
        return queue_next.submit_if_queued(c)

    def test_nothing_is_queued_while_the_loop_is_off(self):
        """An agent that queues its own work forever must require a deliberate
        start."""
        self._queue("- [ ] do a thing")
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "build", "true", cwd=str(self.dir))
            self.assertEqual(self._submit(c), [])

    def test_an_unchecked_item_is_queued_once_the_loop_is_on(self):
        self._queue("- [ ] do a thing")
        queue_next.building_flag().write_text("on")
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "build", "true", cwd=str(self.dir))
            self.assertEqual(len(self._submit(c)), 1)

    def test_a_finished_queue_queues_nothing(self):
        """Otherwise a completed build spins forever burning a unit per tick."""
        self._queue("- [x] already done")
        queue_next.building_flag().write_text("on")
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "build", "true", cwd=str(self.dir))
            self.assertEqual(self._submit(c), [])

    def test_only_one_build_unit_exists_at_a_time(self):
        """The harness runs one unit per tick, so queuing ahead would pile up
        work that cannot start and would double-execute the same item."""
        self._queue("- [ ] a\n- [ ] b\n- [ ] c")
        queue_next.building_flag().write_text("on")
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "build", "true", cwd=str(self.dir))
            self.assertEqual(len(self._submit(c)), 1)
            self.assertEqual(self._submit(c), [])      # still pending
            self.assertEqual(self._submit(c), [])

    def test_a_rate_limited_build_does_not_get_a_second_unit(self):
        """The whole point. A yielding build must resume, not accumulate."""
        self._queue("- [ ] do a thing")
        queue_next.building_flag().write_text("on")
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "build", "true", cwd=str(self.dir))
            self._submit(c)
            u = self.ledger.claim(c)
            self.ledger.mark_rate_limited(
                c, u["job_id"], u["run_id"], "five_hour", time.time() + 3600)
            self.assertEqual(self._submit(c), [])

    def test_stopping_the_loop_leaves_work_in_flight_alone(self):
        self._queue("- [ ] do a thing")
        queue_next.building_flag().write_text("on")
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "build", "true", cwd=str(self.dir))
            self._submit(c)
            queue_next.building_flag().unlink()
            self.assertEqual(
                c.execute("SELECT COUNT(*) n FROM units WHERE job_id='build' "
                          "AND status='pending'").fetchone()["n"], 1)
