"""R11-R17. Re-reporting something the reader has already seen is the most visible
way this system can be wrong, so these tests are about identity, retention, and
the exact moment state is allowed to advance."""
import io
import json
import os
import sys
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass(frozen=True)
class FakeItem:
    """Stands in for curated_digest.sources.base.Item so the store is testable on its
    own — the store cares about the shape, not where the shape came from."""
    id: str
    source_id: str
    url: str = "https://example.com/x"
    title: str = "t"
    body: str = "b"
    published_at: int | None = None
    fetched_at: int = 0


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        os.environ["CURATED_DIGEST_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        # NOTE: do not purge curated-digest.* from sys.modules here. config reads the
        # env on every call, so a reload buys nothing — and a re-imported
        # module defines a *new* exception class, so `except SourceError` in
        # another test file silently stops matching.
        from curated_digest import ledger, store
        self.ledger, self.store = ledger, store
        self.ctx = ledger.connect()
        self.c = self.ctx.__enter__()
        store.ensure_schema(self.c)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("CURATED_DIGEST_STATE_DIR", None)

    def items(self, n, source="feed", start=0):
        base = int(time.time()) - 10000
        return [FakeItem(id=f"i{k}", source_id=source, title=f"item {k}",
                         published_at=base + k * 60, fetched_at=base)
                for k in range(start, start + n)]

    # -- R12: identity, and ingesting the same thing twice
    def test_reingesting_the_same_item_is_a_noop(self):
        self.assertEqual(self.store.ingest(self.c, self.items(3)), 3)
        self.assertEqual(self.store.ingest(self.c, self.items(3)), 0)
        self.assertEqual(
            self.c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"], 3)

    def test_an_edited_feed_does_not_resurrect_an_old_item(self):
        old = FakeItem(id="i0", source_id="feed", title="original", fetched_at=1000)
        self.store.ingest(self.c, [old])
        edited = FakeItem(id="i0", source_id="feed", title="rewritten", fetched_at=9999)
        self.store.ingest(self.c, [edited])
        row = self.c.execute("SELECT * FROM items WHERE item_id='i0'").fetchone()
        self.assertEqual(row["fetched_at"], 1000)
        self.assertEqual(row["title"], "original")

    def test_same_id_from_two_sources_is_two_items(self):
        self.store.ingest(self.c, [FakeItem(id="dup", source_id="a"),
                                   FakeItem(id="dup", source_id="b")])
        self.assertEqual(
            self.c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"], 2)

    # -- R14: the rule that matters most
    def test_state_advances_only_after_delivery_is_confirmed(self):
        self.store.ingest(self.c, self.items(3))
        rows = self.store.candidates(self.c, "task", ["feed"])
        self.assertEqual(len(rows), 3)

        # Delivery failed: mark_reported is NOT called. The finding must survive.
        again = self.store.candidates(self.c, "task", ["feed"])
        self.assertEqual(len(again), 3)

        self.store.mark_reported(self.c, "task", rows)
        self.assertEqual(self.store.candidates(self.c, "task", ["feed"]), [])

    def test_an_item_reported_once_is_never_reported_again(self):
        self.store.ingest(self.c, self.items(2))
        self.store.mark_reported(
            self.c, "task", self.store.candidates(self.c, "task", ["feed"]))
        self.store.ingest(self.c, self.items(2))          # refetched, unchanged
        self.assertEqual(self.store.candidates(self.c, "task", ["feed"]), [])

    def test_two_tasks_track_the_same_item_independently(self):
        self.store.ingest(self.c, self.items(2))
        self.store.mark_reported(
            self.c, "a", self.store.candidates(self.c, "a", ["feed"]))
        self.assertEqual(len(self.store.candidates(self.c, "b", ["feed"])), 2)

    # -- R13: retention
    def test_items_are_retained_after_being_reported(self):
        self.store.ingest(self.c, self.items(2))
        self.store.mark_reported(
            self.c, "task", self.store.candidates(self.c, "task", ["feed"]))
        self.assertEqual(
            self.c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"], 2)

    # -- R15/R17: a first run, and a store that came back from the dead
    def test_first_run_caps_at_ten_and_never_resurfaces_the_rest(self):
        self.store.ingest(self.c, self.items(40))
        first = self.store.candidates(self.c, "task", ["feed"])
        self.assertEqual(len(first), 10)
        self.store.mark_reported(self.c, "task", first)
        self.assertEqual(self.store.candidates(self.c, "task", ["feed"]), [])
        self.assertEqual(
            self.c.execute("SELECT COUNT(*) n FROM reported WHERE reason='watermark'"
                           ).fetchone()["n"], 30)

    def test_first_run_keeps_the_newest_items(self):
        self.store.ingest(self.c, self.items(15))
        got = {r["item_id"] for r in self.store.candidates(self.c, "task", ["feed"])}
        self.assertEqual(got, {f"i{k}" for k in range(5, 15)})

    def test_a_rebuilt_store_does_not_flood_the_channel(self):
        """What losing state/curated_digest.db actually looks like: a full backlog with no
        record of what was already sent."""
        self.store.ingest(self.c, self.items(200))
        self.assertEqual(len(self.store.candidates(self.c, "task", ["feed"])), 10)

    # -- R13: retention has an end, and it is not allowed to lose a finding
    def old(self, n, source="feed", start=0, days=200):
        """Items fetched long enough ago to be past the retention window."""
        base = self.store.now() - days * 86400
        return [FakeItem(id=f"o{k}", source_id=source, title=f"old {k}",
                         published_at=base + k * 60, fetched_at=base)
                for k in range(start, start + n)]

    def test_old_items_every_reader_has_finished_with_are_swept(self):
        self.store.ingest(self.c, self.old(3))
        self.store.mark_reported(
            self.c, "task", self.store.candidates(self.c, "task", ["feed"]))
        self.assertEqual(self.store.sweep_items(self.c, {"feed": ["task"]}), 3)
        self.assertEqual(
            self.c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"], 0)

    def test_an_old_item_still_unreported_is_never_swept(self):
        """The finding R14 protects. A task rate-limited or broken for a while
        still owns its backlog; age must not be allowed to delete it."""
        self.store.ingest(self.c, self.old(2))
        self.assertEqual(self.store.sweep_items(self.c, {"feed": ["task"]}), 0)
        self.assertEqual(len(self.store.candidates(self.c, "task", ["feed"])), 2)

    def test_one_reader_finishing_does_not_sweep_it_from_under_another(self):
        self.store.ingest(self.c, self.old(2))
        self.store.mark_reported(
            self.c, "a", self.store.candidates(self.c, "a", ["feed"]))
        self.assertEqual(
            self.store.sweep_items(self.c, {"feed": ["a", "b"]}), 0)
        self.assertEqual(len(self.store.candidates(self.c, "b", ["feed"])), 2)

    def test_recent_items_are_kept_however_settled_they_are(self):
        self.store.ingest(self.c, self.items(3))
        self.store.mark_reported(
            self.c, "task", self.store.candidates(self.c, "task", ["feed"]))
        self.assertEqual(self.store.sweep_items(self.c, {"feed": ["task"]}), 0)

    def test_a_feed_no_task_reads_any_more_is_swept_on_age_alone(self):
        """A source deleted from tasks/ has no reader that can ever settle its
        items, so waiting for one would mean keeping them forever."""
        self.store.ingest(self.c, self.old(4, source="dropped"))
        self.assertEqual(self.store.sweep_items(self.c, {"feed": ["task"]}), 4)

    def test_a_swept_item_does_not_come_back_when_the_feed_still_lists_it(self):
        """The reason `reported` rows outlive the items they point at. Without
        the tombstone this is R12 broken by the cleanup: re-ingested as new,
        re-curated, re-sent."""
        self.store.ingest(self.c, self.old(1))
        self.store.mark_reported(
            self.c, "task", self.store.candidates(self.c, "task", ["feed"]))
        self.store.sweep_items(self.c, {"feed": ["task"]})
        self.store.ingest(self.c, self.old(1))          # feed still lists it
        self.assertEqual(self.store.candidates(self.c, "task", ["feed"]), [])

    def test_a_judged_item_counts_as_settled(self):
        """Rejected items are the bulk of the table. If only delivered ones
        could be swept, the sweep would barely touch what actually grows."""
        self.store.ingest(self.c, self.old(2))
        rows = self.store.candidates(self.c, "task", ["feed"])
        self.store.mark_judged(self.c, "task", rows)
        self.assertEqual(self.store.sweep_items(self.c, {"feed": ["task"]}), 2)

    def test_the_window_is_measured_from_when_we_fetched_it(self):
        """Feeds lie about dates. An item published years ago but first seen
        yesterday is still new to us and stays."""
        stale_date = FakeItem(id="x", source_id="feed", fetched_at=self.store.now(),
                              published_at=self.store.now() - 900 * 86400)
        self.store.ingest(self.c, [stale_date])
        self.store.mark_reported(
            self.c, "task", self.store.candidates(self.c, "task", ["feed"]))
        self.assertEqual(self.store.sweep_items(self.c, {"feed": ["task"]}), 0)

    # -- R36: broken is not the same as empty
    def test_failures_accumulate_and_success_clears_them(self):
        for _ in range(3):
            n = self.store.record_failure(self.c, "feed", "404")
        self.assertEqual(n, 3)
        self.assertEqual(len(self.store.broken_sources(self.c)), 1)
        self.store.record_success(self.c, "feed", etag='"abc"')
        self.assertEqual(self.store.broken_sources(self.c), [])

    def test_an_empty_feed_is_not_a_broken_source(self):
        self.store.record_success(self.c, "feed")
        self.assertEqual(self.store.candidates(self.c, "task", ["feed"]), [])
        self.assertEqual(self.store.broken_sources(self.c), [])

    def test_conditional_headers_round_trip(self):
        self.assertEqual(self.store.conditional_headers(self.c, "feed"), {})
        self.store.record_success(self.c, "feed", etag='"v1"', last_modified="Mon, 1 Jan 2026 00:00:00 GMT")
        h = self.store.conditional_headers(self.c, "feed")
        self.assertEqual(h["etag"], '"v1"')
        self.assertIn("modified", h)

    def test_success_without_new_validators_keeps_the_old_ones(self):
        self.store.record_success(self.c, "feed", etag='"v1"')
        self.store.record_success(self.c, "feed")          # e.g. a 304
        self.assertEqual(self.store.conditional_headers(self.c, "feed")["etag"], '"v1"')

    # -- R36: a feed answering 304 forever is the quiet way to be broken
    def test_a_source_stuck_on_304_is_eventually_reported(self):
        """The failure this prevents: a feed is retired, the server keeps
        answering 304, every layer downstream reads 'no new items' and stays
        correctly silent, and nobody ever finds out."""
        start = self.store.now()
        self.store.record_success(self.c, "feed", etag='"v1"')
        for _ in range(4):
            self.store.record_success(self.c, "feed", changed=False)
        self.assertEqual(
            self.store.unchanged_sources(self.c, {"feed": 3600}, at=start + 3600), [])
        out = self.store.unchanged_sources(self.c, {"feed": 3600},
                                           at=start + 4 * 3600)
        self.assertEqual([s["source_id"] for s in out], ["feed"])
        self.assertGreaterEqual(out[0]["unchanged_for"], 3 * 3600)

    def test_a_304_does_not_pass_for_a_sign_of_life(self):
        """`last_ok_at` advances on a 304, so measuring against it would make a
        dead feed look permanently healthy. Only a body counts."""
        start = self.store.now()
        self.store.record_success(self.c, "feed", etag='"v1"')
        self.store.record_success(self.c, "feed", changed=False)
        r = self.c.execute("SELECT * FROM source_state WHERE source_id='feed'").fetchone()
        self.assertGreaterEqual(r["last_ok_at"], start)
        self.assertLessEqual(r["last_change_at"], r["last_ok_at"])
        self.store.record_success(self.c, "feed", etag='"v2"')
        self.assertEqual(
            self.store.unchanged_sources(self.c, {"feed": 3600}, at=start + 3600), [])

    def test_a_feed_that_publishes_slower_than_we_poll_is_not_reported(self):
        """The false alarm that would sink this: a weekly changelog task polls every 6h
        and publishes weekly, so three poll intervals of quiet is its normal
        week. Firing on that would suppress the watchdog ping five days out of
        seven and train the reader to mute the channel (R16)."""
        week, poll = 7 * 86400, 6 * 3600
        start = self.store.now() - 40 * 86400
        self.store.ingest(self.c, [
            FakeItem(id=f"w{k}", source_id="feed", fetched_at=start + k * week)
            for k in range(6)])
        self.store.record_success(self.c, "feed", etag='"v1"')
        at = self.store.now() + 3 * poll + 60
        self.assertEqual(self.store.unchanged_sources(self.c, {"feed": poll}, at=at), [])
        # ...but three of its OWN weeks of silence is a real signal.
        dead = self.store.now() + 22 * 86400
        self.assertEqual(
            [s["source_id"]
             for s in self.store.unchanged_sources(self.c, {"feed": poll}, at=dead)],
            ["feed"])

    def test_one_bulk_first_fetch_is_one_observation_not_ten(self):
        """Counting a backlog ingested at one instant as ten changes would drag
        the estimated cadence to nearly zero and put the noise straight back."""
        poll = 3600
        start = self.store.now() - 10 * 86400
        self.store.ingest(self.c, [FakeItem(id=f"b{k}", source_id="feed",
                                            fetched_at=start) for k in range(10)])
        self.store.record_success(self.c, "feed", etag='"v1"')
        self.assertEqual(
            self.store.unchanged_sources(self.c, {"feed": poll},
                                         at=self.store.now() + 2 * poll), [])

    def test_a_broken_source_is_not_also_reported_as_unchanged(self):
        """One underlying problem, one alert. A feed that cannot be fetched has
        not been observed to change either, and naming it twice is noise."""
        start = self.store.now()
        self.store.record_success(self.c, "feed", etag='"v1"')
        for _ in range(3):
            self.store.record_failure(self.c, "feed", "HTTP 404")
        self.assertEqual(
            self.store.unchanged_sources(self.c, {"feed": 60}, at=start + 86400), [])

    def test_a_source_never_fetched_has_no_opinion(self):
        self.assertEqual(self.store.unchanged_sources(self.c, {"feed": 60}), [])

    def test_the_change_clock_is_backfilled_for_pre_existing_sources(self):
        """Without the backfill every source that predates the column stays
        invisible to this check forever: last_change_at NULL, last_ok_at moving
        on every 304, and no baseline that can ever age out."""
        self.c.execute("DROP TABLE source_state")
        self.c.execute("""CREATE TABLE source_state (
            source_id TEXT PRIMARY KEY, etag TEXT, last_modified TEXT,
            last_ok_at INTEGER, last_attempt_at INTEGER, last_error TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0)""")
        old = self.store.now() - 86400
        self.c.execute("INSERT INTO source_state(source_id, last_ok_at, "
                       "last_attempt_at) VALUES ('feed',?,?)", (old, old))
        self.store.ensure_schema(self.c)
        self.assertEqual(
            [s["source_id"] for s in
             self.store.unchanged_sources(self.c, {"feed": 3600})], ["feed"])

    # -- R37: silence and death look identical from outside
    def test_a_task_silent_for_three_intervals_is_flagged(self):
        self.store.ingest(self.c, self.items(1))
        self.store.mark_reported(
            self.c, "task", self.store.candidates(self.c, "task", ["feed"]))
        self.assertEqual(self.store.stale_tasks(self.c, {"task": 3600}), [])
        later = self.store.now() + 4 * 3600
        self.assertEqual(self.store.stale_tasks(self.c, {"task": 3600}, at=later),
                         ["task"])

    def test_unknown_task_is_not_flagged_as_stale(self):
        self.assertEqual(self.store.stale_tasks(self.c, {"never-ran": 60}), [])

    # -- R17
    def test_integrity_is_checkable(self):
        self.assertEqual(self.store.integrity(self.c), "ok")


TASK_YAML = """\
id: probe
source: {type: rss, url: https://example.com/feed.xml}
criteria: Anything at all.
schedule: every 30m
model: recorder:cheap
renderer: telegram
channel: telegram
urgency: digest
heading: "Probe"
"""


class SweepIsWired(unittest.TestCase):
    """A retention policy nothing calls is a comment. `curated-digest dispatch` runs every
    tick, and it is the only verb that knows which task reads which source."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks_dir = root / "tasks"
        self.tasks_dir.mkdir()
        (self.tasks_dir / "probe.yaml").write_text(TASK_YAML)
        os.environ["CURATED_DIGEST_TASKS_DIR"] = str(self.tasks_dir)
        os.environ["CURATED_DIGEST_STATE_DIR"] = str(root / "state")
        from curated_digest import cli, ledger, store
        self.cli, self.ledger, self.store = cli, ledger, store

    def tearDown(self):
        self.tmp.cleanup()
        for k in ("CURATED_DIGEST_TASKS_DIR", "CURATED_DIGEST_STATE_DIR"):
            os.environ.pop(k, None)

    def seed(self, reported=True):
        old = int(time.time()) - 200 * 86400
        with self.ledger.connect() as c:
            self.store.ensure_schema(c)
            self.store.ingest(c, [FakeItem(id="i0", source_id="probe",
                                           fetched_at=old, published_at=old)])
            if reported:
                self.store.mark_reported(
                    c, "probe", self.store.candidates(c, "probe", ["probe"]))

    def remaining(self) -> int:
        with self.ledger.connect() as c:
            self.store.ensure_schema(c)
            return c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]

    def dispatch(self) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.cli.main(["dispatch"])
        return json.loads(buf.getvalue())

    def test_a_tick_sweeps(self):
        self.seed()
        self.assertEqual(self.dispatch()["swept"], 1)
        self.assertEqual(self.remaining(), 0)

    def test_a_task_that_will_not_load_suspends_the_sweep(self):
        """A broken file contributes no sources, so its feeds would look unread
        and its undelivered backlog would be swept over a typo."""
        self.seed()
        (self.tasks_dir / "broken.yaml").write_text("id: broken\n")
        out = self.dispatch()
        self.assertEqual(out["swept"], 0)
        self.assertTrue(out["broken"])
        self.assertEqual(self.remaining(), 1)


class TheUnchangedClockIsWired(unittest.TestCase):
    """The store can tell a 304 from a body, but only if the fetch path bothers
    to say which one it got. This is the seam where the whole check would
    silently become decoration."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        os.environ["CURATED_DIGEST_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        from curated_digest import dispatch, ledger, store, task
        self.dispatch, self.store, self.task_mod = dispatch, store, task
        self.ctx = ledger.connect()
        self.c = self.ctx.__enter__()
        store.ensure_schema(self.c)
        self.src = task.Source("feed", "rss", "https://example.com/f.xml")

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("CURATED_DIGEST_STATE_DIR", None)

    def state(self):
        return self.c.execute(
            "SELECT * FROM source_state WHERE source_id='feed'").fetchone()

    def fetch(self, raw):
        impl = unittest.mock.Mock()
        impl.fetch.return_value = (raw, {"etag": '"v2"'})
        impl.parse.return_value = []
        with unittest.mock.patch.dict(self.dispatch.SOURCE_TYPES, {"rss": impl}):
            return self.dispatch._fetch_source(self.c, self.src, {"new": 0})

    def test_a_304_advances_last_ok_but_not_last_change(self):
        self.store.record_success(self.c, "feed", etag='"v1"')
        # Backdate deliberately. Both clocks would otherwise be written in the
        # same second, and this test would pass whether or not the 304 branch
        # said `changed=False` — the exact regression it exists to catch.
        old = self.store.now() - 40 * 86400
        self.c.execute("UPDATE source_state SET last_ok_at=0, last_attempt_at=0, "
                       "last_change_at=?", (old,))
        self.assertEqual(self.fetch(None), "unchanged")
        after = self.state()
        self.assertGreater(after["last_ok_at"], 0)       # still a success
        self.assertEqual(after["last_change_at"], old)   # but not a sign of life
        self.assertEqual(
            [s["source_id"] for s in
             self.store.unchanged_sources(self.c, {"feed": 3600})], ["feed"])

    def test_a_body_restarts_the_clock(self):
        old = self.store.now() - 90 * 86400
        self.c.execute(
            """INSERT INTO source_state(source_id, last_ok_at, last_change_at,
                                        last_attempt_at, consecutive_failures)
               VALUES ('feed',?,?,?,0)""", (old, old, old))
        self.assertEqual(self.fetch(b"<rss/>"), "ok")
        self.assertGreater(self.state()["last_change_at"], old)
        self.assertEqual(
            self.store.unchanged_sources(self.c, {"feed": 3600}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
