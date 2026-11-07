"""R6-R10: tasks are the only interface for adding work to the system, so a
malformed one must fail loudly and *in isolation* (R9) — these tests exist
mostly to prove that guarantee, plus the schedule grammar it depends on."""
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest.task import Task, TaskError, load_all, load_task, parse_schedule, is_due  # noqa: E402

GOOD = """\
id: {id}
source: {{type: rss, url: https://example.com/feed.xml}}
criteria: |
  What the reader cares about.
schedule: daily at 07:00
model: gemini:gemini-3.6-flash
renderer: telegram
channel: telegram
urgency: digest
heading: "Claude"
"""


def write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body)
    return p


class LoadTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_well_formed_task_loads(self):
        p = write(self.dir, "claude-changelog.yaml", GOOD.format(id="claude-changelog"))
        t = load_task(p)
        self.assertEqual(t.id, "claude-changelog")
        self.assertEqual(t.sources[0].type, "rss")
        self.assertEqual(t.sources[0].url, "https://example.com/feed.xml")
        self.assertEqual(t.model, "gemini:gemini-3.6-flash")
        self.assertEqual(t.urgency, "digest")
        self.assertIsNone(t.link)
        self.assertEqual(t.include, ())
        self.assertEqual(t.exclude, ())

    def test_optional_fields_carry_through(self):
        body = GOOD.format(id="x") + (
            "link: https://example.com/changelog\n"
            "prefilter: {max_age_days: 14, exclude: [sponsored], include: [claude]}\n"
        )
        p = write(self.dir, "x.yaml", body)
        t = load_task(p)
        self.assertEqual(t.link, "https://example.com/changelog")
        self.assertEqual(t.max_age_days, 14)
        self.assertEqual(t.exclude, ("sponsored",))
        self.assertEqual(t.include, ("claude",))

    def test_a_multi_source_task_may_not_carry_one_footer_link(self):
        """The reddit digest shipped a week of Linux tips footnoted with a link
        to r/selfhosted. A footer link is a claim about every bullet."""
        body = GOOD.format(id="multi").replace(
            "source: {type: rss, url: https://example.com/feed.xml}",
            "source:\n"
            "  - {type: rss, url: https://a.example/f.xml, id: a}\n"
            "  - {type: rss, url: https://b.example/f.xml, id: b}",
        ) + "link: https://a.example/\n"
        p = write(self.dir, "multi.yaml", body)
        with self.assertRaises(TaskError) as e:
            load_task(p)
        self.assertIn("single-source", str(e.exception))

    # -- R7: every required field, missing one at a time
    def _drop(self, key):
        lines = GOOD.format(id="x").splitlines()
        # criteria is multi-line (block scalar); drop both its lines together.
        out = []
        skip_next_indent = False
        for line in lines:
            if skip_next_indent and line.startswith("  "):
                continue
            skip_next_indent = False
            if line.startswith(f"{key}:"):
                skip_next_indent = True
                continue
            out.append(line)
        return "\n".join(out) + "\n"

    def test_missing_required_fields(self):
        for key in ("id", "source", "criteria", "schedule", "model",
                    "renderer", "channel", "urgency", "heading"):
            with self.subTest(key=key):
                p = write(self.dir, "x.yaml", self._drop(key))
                with self.assertRaises(TaskError) as cm:
                    load_task(p)
                self.assertIn(key, cm.exception.reason)

    # -- R8: a bare model name is an error, never a default
    def test_bare_model_is_rejected(self):
        body = GOOD.format(id="x").replace(
            "model: gemini:gemini-3.6-flash", "model: gemini-3.6-flash")
        p = write(self.dir, "x.yaml", body)
        with self.assertRaises(TaskError) as cm:
            load_task(p)
        self.assertIn("provider:model", cm.exception.reason)

    def test_bad_urgency_is_rejected(self):
        body = GOOD.format(id="x").replace("urgency: digest", "urgency: whenever")
        p = write(self.dir, "x.yaml", body)
        with self.assertRaises(TaskError):
            load_task(p)

    # -- id must match filename stem, or state silently orphans
    def test_id_filename_mismatch_is_rejected(self):
        p = write(self.dir, "other-name.yaml", GOOD.format(id="claude-changelog"))
        with self.assertRaises(TaskError) as cm:
            load_task(p)
        self.assertIn("other-name", cm.exception.reason)

    def test_unsafe_id_is_rejected(self):
        body = GOOD.format(id="Claude Changelog!")
        p = write(self.dir, "Claude Changelog!.yaml", body)
        with self.assertRaises(TaskError):
            load_task(p)

    # -- an unrecognised top-level key must fail, not be silently ignored
    def test_unknown_key_is_rejected(self):
        body = GOOD.format(id="x") + "critera: this is a typo\n"
        p = write(self.dir, "x.yaml", body)
        with self.assertRaises(TaskError) as cm:
            load_task(p)
        self.assertIn("critera", cm.exception.reason)

    def test_non_http_source_url_is_rejected(self):
        body = GOOD.format(id="x").replace(
            "url: https://example.com/feed.xml", "url: file:///etc/passwd")
        p = write(self.dir, "x.yaml", body)
        with self.assertRaises(TaskError):
            load_task(p)

    def test_non_rss_source_type_is_rejected(self):
        body = GOOD.format(id="x").replace("type: rss", "type: atom")
        p = write(self.dir, "x.yaml", body)
        with self.assertRaises(TaskError):
            load_task(p)


class LoadAllTests(unittest.TestCase):
    def test_one_bad_file_does_not_stop_the_others(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            write(tmp, "good.yaml", GOOD.format(id="good"))
            write(tmp, "bad.yaml", GOOD.format(id="good").replace(
                "urgency: digest", "urgency: whenever"))
            tasks, errors = load_all(tmp)
            self.assertEqual([t.id for t in tasks], ["good"])
            self.assertEqual(len(errors), 1)
            self.assertIn("bad.yaml", errors[0].path)

    def test_empty_directory_loads_nothing(self):
        with TemporaryDirectory() as tmp:
            tasks, errors = load_all(tmp)
            self.assertEqual(tasks, [])
            self.assertEqual(errors, [])


class ParseScheduleTests(unittest.TestCase):
    def test_every_minutes(self):
        self.assertEqual(parse_schedule("every 30m"), {"kind": "every", "seconds": 1800})

    def test_every_hours(self):
        self.assertEqual(parse_schedule("every 6h"), {"kind": "every", "seconds": 21600})

    def test_daily_at(self):
        self.assertEqual(parse_schedule("daily at 07:00"),
                          {"kind": "daily", "hour": 7, "minute": 0})

    def test_bad_schedule_strings_raise(self):
        for s in ("weekly", "every 5", "every 5x", "daily at 25:00", "daily at 7:00", ""):
            with self.subTest(s=s):
                with self.assertRaises(TaskError):
                    parse_schedule(s)


class IsDueTests(unittest.TestCase):
    def test_never_run_is_always_due(self):
        self.assertTrue(is_due("every 30m", None, time.time()))
        self.assertTrue(is_due("daily at 07:00", None, time.time()))

    def test_every_just_under_interval_not_due(self):
        now = 1_000_000.0
        self.assertFalse(is_due("every 30m", now - 1799, now))

    def test_every_just_over_interval_is_due(self):
        now = 1_000_000.0
        self.assertTrue(is_due("every 30m", now - 1800, now))
        self.assertTrue(is_due("every 30m", now - 1801, now))

    # -- daily, using tz_offset_hours=0 (UTC) as the deterministic test seam
    def test_daily_not_yet_reached_today(self):
        # scheduled 07:00, now is 06:00 today, last run was yesterday at 07:01
        now = _epoch_utc(2026, 8, 24, 6, 0)
        last_run = _epoch_utc(2026, 8, 23, 7, 1)
        self.assertFalse(is_due("daily at 07:00", last_run, now, tz_offset_hours=0))

    def test_daily_reached_but_already_ran_today(self):
        now = _epoch_utc(2026, 8, 24, 8, 0)
        last_run = _epoch_utc(2026, 8, 24, 7, 2)
        self.assertFalse(is_due("daily at 07:00", last_run, now, tz_offset_hours=0))

    def test_daily_reached_and_last_ran_yesterday(self):
        now = _epoch_utc(2026, 8, 24, 8, 0)
        last_run = _epoch_utc(2026, 8, 23, 7, 0)
        self.assertTrue(is_due("daily at 07:00", last_run, now, tz_offset_hours=0))

    def test_daily_late_tick_still_fires(self):
        # tick lands at 07:03 for a 07:00 schedule; a missed exact minute
        # must not skip the whole day.
        now = _epoch_utc(2026, 8, 24, 7, 3)
        last_run = _epoch_utc(2026, 8, 23, 7, 0)
        self.assertTrue(is_due("daily at 07:00", last_run, now, tz_offset_hours=0))

    def test_daily_exactly_at_scheduled_minute_is_due(self):
        now = _epoch_utc(2026, 8, 24, 7, 0)
        last_run = _epoch_utc(2026, 8, 23, 7, 0)
        self.assertTrue(is_due("daily at 07:00", last_run, now, tz_offset_hours=0))


def _epoch_utc(y, m, d, h, mi):
    import calendar
    return calendar.timegm((y, m, d, h, mi, 0, 0, 0, -1))


if __name__ == "__main__":
    unittest.main()


class MultipleSources(unittest.TestCase):
    """One digest may span several feeds — three subreddits in one Reddit
    digest rather than three separate messages."""

    def _write(self, tmp, body, name="t.yaml"):
        p = Path(tmp) / name
        p.write_text(body)
        return p

    BASE = """id: t
criteria: c
schedule: every 1h
model: gemini:m
renderer: telegram
channel: telegram
urgency: digest
heading: H
"""

    def test_a_single_source_keeps_the_task_id_as_its_key(self):
        """Load-bearing: existing tasks already hold store state under the bare
        task id. A new key would look like a brand new source — fresh
        watermark, ten items re-reported."""
        from curated_digest.task import load_task
        with TemporaryDirectory() as tmp:
            t = load_task(self._write(
                tmp, self.BASE + "source: {type: rss, url: https://e.com/f.xml}"))
        self.assertEqual(len(t.sources), 1)
        self.assertEqual(t.sources[0].id, "t")

    def test_a_list_of_sources_gets_distinct_namespaced_ids(self):
        from curated_digest.task import load_task
        with TemporaryDirectory() as tmp:
            t = load_task(self._write(tmp, self.BASE + """source:
  - {type: rss, url: https://reddit.com/r/selfhosted/.rss, id: selfhosted}
  - {type: rss, url: https://reddit.com/r/linux/.rss, id: linux}
"""))
        self.assertEqual([s.id for s in t.sources],
                         ["t/selfhosted", "t/linux"])

    def test_ids_are_derived_when_not_given(self):
        from curated_digest.task import load_task
        with TemporaryDirectory() as tmp:
            t = load_task(self._write(tmp, self.BASE + """source:
  - {type: rss, url: https://www.reddit.com/r/selfhosted/.rss}
  - {type: rss, url: https://www.reddit.com/r/pixel_phones/.rss}
"""))
        ids = [s.id for s in t.sources]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(i.startswith("t/") for i in ids))

    def test_duplicate_source_ids_are_rejected(self):
        """Two sources sharing a key would interleave their etags and failure
        counts into nonsense."""
        from curated_digest.task import load_task, TaskError
        with TemporaryDirectory() as tmp:
            with self.assertRaises(TaskError):
                load_task(self._write(tmp, self.BASE + """source:
  - {type: rss, url: https://a.com/f.xml, id: same}
  - {type: rss, url: https://b.com/f.xml, id: same}
"""))

    def test_an_empty_source_list_is_rejected(self):
        from curated_digest.task import load_task, TaskError
        with TemporaryDirectory() as tmp:
            with self.assertRaises(TaskError):
                load_task(self._write(tmp, self.BASE + "source: []"))

    def test_one_bad_url_in_a_list_fails_the_whole_task(self):
        """R9: loudly, at load time. A silently dropped source is a digest that
        is quietly missing a third of its input."""
        from curated_digest.task import load_task, TaskError
        with TemporaryDirectory() as tmp:
            with self.assertRaises(TaskError):
                load_task(self._write(tmp, self.BASE + """source:
  - {type: rss, url: https://a.com/f.xml}
  - {type: rss, url: not-a-url}
"""))
