"""R16/R37/R38/R39/R42b.

The watchdog paradox is the thing being tested: Telegram is the notification
channel, so curated-digest cannot be the only thing that reports curated-digest is dead.
"""
import os
import sys
import unittest
import unittest.mock
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass(frozen=True)
class S:
    id: str


@dataclass(frozen=True)
class T:
    id: str
    schedule: str = "every 1h"
    sources: tuple = ()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        os.environ["CURATED_DIGEST_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        from curated_digest import health, ledger, store
        self.health, self.ledger, self.store = health, ledger, store
        self.ctx = ledger.connect()
        self.c = self.ctx.__enter__()
        store.ensure_schema(self.c)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("CURATED_DIGEST_STATE_DIR", None)


class HealthTests(Base):
    def test_a_healthy_system_produces_no_alert(self):
        report = self.health.check(self.c, [T("a")])
        self.assertTrue(self.health.is_healthy(report))
        self.assertIsNone(self.health.render_alert(report))

    def test_a_broken_source_makes_the_system_unhealthy(self):
        for _ in range(3):
            self.store.record_failure(self.c, "feed", "HTTP 404")
        report = self.health.check(self.c, [T("feed")])
        self.assertFalse(self.health.is_healthy(report))
        self.assertIn("404", self.health.render_alert(report))

    def test_the_alert_is_built_without_a_model(self):
        """R42b: an alert saying the pipeline is broken has to work when the
        pipeline is broken, which is when a model call is least likely to."""
        for _ in range(3):
            self.store.record_failure(self.c, "feed", "boom")
        with unittest.mock.patch("curated_digest.providers.resolve") as resolve:
            text = self.health.render_alert(self.health.check(self.c, [T("feed")]))
        resolve.assert_not_called()
        self.assertIn("curated-digest needs attention", text)

    def test_a_source_stuck_on_304_reaches_the_alert(self):
        """R36 end to end. The store can spot it, but if `check` never asks,
        the one condition nothing else in the pipeline can see stays invisible:
        curation and rendering both read a retired feed as 'nothing new' and
        stay correctly silent (D7)."""
        old = self.store.now() - 30 * 86400
        self.c.execute(
            """INSERT INTO source_state(source_id, last_ok_at, last_change_at,
                                        last_attempt_at, consecutive_failures)
               VALUES ('feed',?,?,?,0)""", (self.store.now(), old, self.store.now()))
        report = self.health.check(self.c, [T("task", sources=(S("feed"),))])
        self.assertFalse(self.health.is_healthy(report))
        text = self.health.render_alert(report)
        self.assertIn("feed", text)
        self.assertIn("30d", text)

    def test_a_source_no_task_reads_is_not_judged(self):
        """Intervals come from the tasks that read a source. A feed dropped from
        every task file has no schedule to be late against."""
        old = self.store.now() - 300 * 86400
        self.c.execute(
            """INSERT INTO source_state(source_id, last_ok_at, last_change_at,
                                        last_attempt_at, consecutive_failures)
               VALUES ('orphan',?,?,?,0)""", (old, old, old))
        report = self.health.check(self.c, [T("task")])
        self.assertEqual(report["unchanged_sources"], [])

    def test_a_ping_is_impossible_without_a_configured_url(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True), \
             unittest.mock.patch.object(self.health, "ENV_FILE",
                                        Path("/nonexistent")):
            self.assertFalse(self.health.ping())

    def test_a_failed_ping_is_not_treated_as_a_digest_failure(self):
        """The watchdog notices a missing ping on its own. That is the design."""
        import urllib.error
        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=urllib.error.URLError("down")):
            self.assertFalse(self.health.ping("https://hc-ping.com/x"))


class AlertThrottling(Base):
    """R16: a condition that persists escalates, rather than repeating
    identically until the reader mutes the channel."""

    def test_the_first_occurrence_always_alerts(self):
        send, hours = self.store.should_alert(self.c, "source:feed")
        self.assertTrue(send)
        self.assertEqual(hours, 0)

    def test_a_repeat_inside_the_window_is_suppressed(self):
        self.store.should_alert(self.c, "source:feed")
        send, _ = self.store.should_alert(self.c, "source:feed")
        self.assertFalse(send)

    def test_a_persistent_condition_re_alerts_with_new_information(self):
        at = self.store.now()
        self.store.should_alert(self.c, "source:feed", at=at)
        send, hours = self.store.should_alert(
            self.c, "source:feed", min_interval=3600, at=at + 8 * 3600)
        self.assertTrue(send)
        self.assertEqual(hours, 8)          # not the same message again

    def test_a_resolved_condition_forgets_its_history(self):
        """So a recurrence next week is reported as new, not folded into an
        old escalation that says 'ongoing 168h'."""
        at = self.store.now()
        self.store.should_alert(self.c, "source:feed", at=at)
        self.store.clear_alert(self.c, "source:feed")
        send, hours = self.store.should_alert(self.c, "source:feed", at=at + 60)
        self.assertTrue(send)
        self.assertEqual(hours, 0)

    def test_condition_keys_are_stable_across_runs(self):
        """A key that changes every tick would defeat the throttle entirely."""
        for _ in range(3):
            self.store.record_failure(self.c, "feed", "boom")
        r1 = self.health.check(self.c, [T("feed")])
        self.store.record_failure(self.c, "feed", "different error text")
        r2 = self.health.check(self.c, [T("feed")])
        self.assertEqual([k for k, _ in self.health.conditions(r1)],
                         [k for k, _ in self.health.conditions(r2)])

    def test_a_throttled_alert_renders_nothing_rather_than_an_empty_shell(self):
        for _ in range(3):
            self.store.record_failure(self.c, "feed", "boom")
        report = self.health.check(self.c, [T("feed")])
        throttle = lambda k: self.store.should_alert(self.c, k)
        self.assertIsNotNone(self.health.render_alert(report, throttle))
        self.assertIsNone(self.health.render_alert(report, throttle))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StalenessMeasuresRunningNotDelivering(Base):
    """R37 vs R26. Silence is the correct default outcome, so a task that runs
    and delivers nothing is healthy. Measuring delivery put those two
    requirements in direct conflict and alerted on a task doing its job."""

    def test_a_task_that_runs_but_delivers_nothing_is_not_stale(self):
        # Observed live: a weekly changelog task, every 6h, ran fine, nothing worth
        # sending for 24h, reported as possibly broken every tick.
        at = self.store.now()
        self.c.execute(
            "INSERT INTO task_state(task_id, initialized_at, last_run_at, "
            "last_delivered_at) VALUES ('t', ?, ?, ?)",
            (at - 5 * 86400, at - 600, at - 86400))
        self.assertEqual(self.store.stale_tasks(self.c, {"t": 21600}, at=at), [])

    def test_a_task_that_has_stopped_running_is_stale(self):
        """Running is the thing that can actually stop — a dead timer, a broken
        schedule, a permanent yield."""
        at = self.store.now()
        self.c.execute(
            "INSERT INTO task_state(task_id, initialized_at, last_run_at) "
            "VALUES ('t', ?, ?)", (at - 5 * 86400, at - 3 * 86400))
        self.assertEqual(self.store.stale_tasks(self.c, {"t": 21600}, at=at), ["t"])

    def test_a_task_that_has_never_run_falls_back_to_when_it_was_added(self):
        at = self.store.now()
        self.c.execute(
            "INSERT INTO task_state(task_id, initialized_at) VALUES ('t', ?)",
            (at - 3 * 86400,))
        self.assertEqual(self.store.stale_tasks(self.c, {"t": 21600}, at=at), ["t"])


class WatchdogPingIsDrivenByHealthNotByAlerts(Base):
    """The dead-man's switch must never be told the system is fine while it is
    not — that is the single thing it exists to prevent."""

    def _unhealthy(self):
        for _ in range(3):
            self.store.record_failure(self.c, "feed", "HTTP 404")
        return self.health.check(self.c, [T("feed")])

    def test_an_unhealthy_report_does_not_ping_even_when_the_alert_is_throttled(self):
        report = self._unhealthy()
        throttle = lambda k: self.store.should_alert(self.c, k)
        self.assertIsNotNone(self.health.render_alert(report, throttle))
        # Second pass: throttled, so no alert text is produced. The old code
        # read that as healthy and pinged.
        self.assertIsNone(self.health.render_alert(report, throttle))
        self.assertFalse(self.health.is_healthy(report))

    def test_a_healthy_report_still_pings(self):
        report = self.health.check(self.c, [T("ok")])
        self.assertTrue(self.health.is_healthy(report))
        self.assertIsNone(self.health.render_alert(report))
