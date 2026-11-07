"""H20: the harness is proved by behaviour, not by configuration.

Every test here corresponds to something that can actually go wrong in an
unattended loop: a crash mid-flight, a rate limit, a job that exits 0 without
doing its work, a runaway spend, a human reclaiming the quota.
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class Base(unittest.TestCase):
    """Fixture: an isolated state dir and a scratch working directory."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ["CURATED_DIGEST_STATE_DIR"] = str(self.dir / "state")
        # NOTE: do not purge curated-digest.* from sys.modules here. config reads the
        # env on every call, so a reload buys nothing — and a re-imported
        # module defines a *new* exception class, so `except SourceError` in
        # another test file silently stops matching.
        from curated_digest import config, ledger, ratelimit, runner
        self.config, self.ledger, self.ratelimit, self.runner = \
            config, ledger, ratelimit, runner
        self.cfg = config.load()
        self.counter = self.dir / "runs.txt"
        self.marker = self.dir / "marker"

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("CURATED_DIGEST_STATE_DIR", None)

    # -- helpers ---------------------------------------------------------
    def job(self, conn, cmd, verify=None, jid="j"):
        self.ledger.define_job(conn, jid, cmd, verify_cmd=verify, cwd=str(self.dir))
        return self.ledger.submit(conn, jid)

    def unit(self, conn, jid="j", rid=None):
        q = "SELECT * FROM units WHERE job_id=?"
        a = [jid]
        if rid:
            q += " AND run_id=?"
            a.append(rid)
        return conn.execute(q, a).fetchone()

    # side effect: append a line and create a marker. verify prints the marker.
    SIDE_EFFECT = "echo x >> runs.txt && touch marker"
    VERIFY = "test -f marker && echo present || echo absent"

    def runs(self):
        return self.counter.read_text().count("x") if self.counter.exists() else 0


class Harness(Base):
    # -- H2/H3: a unit exists before it runs, and completes only when observed
    def test_row_exists_before_execution_and_done_requires_side_effect(self):
        with self.ledger.connect() as c:
            rid = self.job(c, self.SIDE_EFFECT, self.VERIFY)
            self.assertEqual(self.unit(c)["status"], "pending")
            self.assertIsNone(self.unit(c)["started_at"])
            out = self.runner.tick(c, self.cfg)
            self.assertEqual(out["outcome"], "done")
            self.assertEqual(self.runs(), 1)

    def test_exit_zero_without_side_effect_is_not_done(self):
        """The failure that shipped looking correct last time."""
        with self.ledger.connect() as c:
            self.job(c, "true", self.VERIFY)
            out = self.runner.tick(c, self.cfg)
            self.assertEqual(out["outcome"], "pending")   # requeued, not done
            self.assertEqual(self.unit(c)["attempts"], 1)

    # -- H20: kill mid-flight, restart, no double execution
    def test_crash_after_side_effect_reconciles_to_done_without_rerunning(self):
        with self.ledger.connect() as c:
            self.job(c, self.SIDE_EFFECT, self.VERIFY)
            # Simulate: claimed, baseline taken, side effect landed, then killed
            u = self.ledger.claim(c)
            base = self.runner.witness(
                c.execute("SELECT * FROM jobs WHERE job_id='j'").fetchone(), str(self.dir))
            self.ledger.set_baseline(c, u["job_id"], u["run_id"], base)
            os.system(f"cd {self.dir} && {self.SIDE_EFFECT}")
            self.assertEqual(self.unit(c)["status"], "running")

            self.runner.tick(c, self.cfg)                 # restart
            self.assertEqual(self.unit(c)["status"], "done")
            self.assertEqual(self.runs(), 1)              # NOT re-executed

    def test_crash_before_side_effect_requeues_and_reruns(self):
        with self.ledger.connect() as c:
            self.job(c, self.SIDE_EFFECT, self.VERIFY)
            u = self.ledger.claim(c)
            job = c.execute("SELECT * FROM jobs WHERE job_id='j'").fetchone()
            self.ledger.set_baseline(c, u["job_id"], u["run_id"],
                                     self.runner.witness(job, str(self.dir)))
            self.assertEqual(self.unit(c)["status"], "running")

            # A tick reconciles first, then claims (H12), so the requeued unit
            # runs in the same tick. What matters: it ran, exactly once, and the
            # crash is on the record as an attempt.
            self.runner.tick(c, self.cfg)
            u = self.unit(c)
            self.assertEqual(u["status"], "done")
            self.assertEqual(u["attempts"], 1)
            self.assertEqual(self.runs(), 1)

    # -- H8/H9: rate limits yield without burning a retry
    def test_rate_limit_yields_without_consuming_a_retry(self):
        with self.ledger.connect() as c:
            self.job(c, "echo \"You've hit your session limit - resets 3pm\" >&2; exit 1")
            out = self.runner.tick(c, self.cfg)
            u = self.unit(c)
            self.assertEqual(out["outcome"], "rate_limited")
            self.assertEqual(u["status"], "rate_limited")
            self.assertEqual(u["attempts"], 0)            # H9
            self.assertGreater(u["resume_after"], time.time())

    def test_rate_limited_unit_is_not_claimed_until_window_reopens(self):
        with self.ledger.connect() as c:
            self.job(c, "echo \"you've hit your session limit\" >&2; exit 1")
            self.runner.tick(c, self.cfg)
            self.assertEqual(self.runner.tick(c, self.cfg)["action"], "idle")
            c.execute("UPDATE units SET resume_after=? WHERE job_id='j'",
                      (int(time.time()) - 1,))
            self.assertEqual(self.runner.tick(c, self.cfg)["action"], "ran")   # H13

    def test_api_error_is_not_a_rate_limit(self):
        v = self.ratelimit.classify(
            "API Error: you've hit your session limit", self.cfg)
        self.assertFalse(v.is_rate_limit)

    def test_weekly_limit_is_handled_distinctly(self):
        v = self.ratelimit.classify("You've hit your weekly limit", self.cfg)
        self.assertTrue(v.is_rate_limit)
        self.assertEqual(v.kind, "weekly")           # H11
        self.assertGreater(v.resume_after, time.time() + 6 * 86400)

    def test_sidecar_reset_time_beats_parsed_text(self):
        at = int(time.time()) + 4200
        self.config.sidecar_path().write_text(
            json.dumps({"rate_limits": {"five_hour": {"resets_at": at}}}))
        v = self.ratelimit.classify("you've hit your session limit", self.cfg)
        self.assertEqual(v.source, "sidecar")
        self.assertEqual(v.resume_after, at)

    def test_stale_sidecar_is_ignored(self):
        p = self.config.sidecar_path()
        p.write_text(json.dumps(
            {"rate_limits": {"five_hour": {"resets_at": int(time.time()) + 4200}}}))
        old = time.time() - 10000
        os.utime(p, (old, old))
        v = self.ratelimit.classify("you've hit your session limit", self.cfg)
        self.assertNotEqual(v.source, "sidecar")

    # -- H6/H14: retries cap, waiting units eventually need a human
    def test_retries_cap_then_stuck_and_requeue_recovers(self):
        with self.ledger.connect() as c:
            self.job(c, "exit 7")
            for _ in range(3):
                self.runner.tick(c, self.cfg)
            u = self.unit(c)
            self.assertEqual(u["status"], "stuck")
            self.assertEqual(u["attempts"], 3)
            self.assertIn("exit 7", u["last_error"])
            self.assertEqual(self.runner.tick(c, self.cfg)["action"], "idle")
            self.ledger.requeue(c, job_id="j")
            self.assertEqual(self.unit(c)["status"], "pending")

    def test_rate_limited_over_24h_becomes_stuck(self):
        with self.ledger.connect() as c:
            self.job(c, "echo \"you've hit your session limit\" >&2; exit 1")
            self.runner.tick(c, self.cfg)
            c.execute("UPDATE units SET rate_limited_since=? WHERE job_id='j'",
                      (int(time.time()) - 90000,))
            self.runner.tick(c, self.cfg)
            self.assertEqual(self.unit(c)["status"], "stuck")

    # -- H7: isolation
    def test_one_failing_unit_does_not_block_another(self):
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "bad", "exit 1", cwd=str(self.dir))
            self.ledger.define_job(c, "good", self.SIDE_EFFECT,
                                   verify_cmd=self.VERIFY, cwd=str(self.dir))
            self.ledger.submit(c, "bad")
            self.ledger.submit(c, "good")
            self.runner.tick(c, self.cfg)
            self.runner.tick(c, self.cfg)
            self.assertEqual(self.unit(c, "good")["status"], "done")
            self.assertEqual(self.unit(c, "bad")["status"], "pending")
            self.assertEqual(self.unit(c, "bad")["attempts"], 1)

    # -- H16/H17: pause
    def test_pause_stops_new_claims(self):
        with self.ledger.connect() as c:
            self.job(c, self.SIDE_EFFECT, self.VERIFY)
            self.config.pause_flag().write_text("test")
            self.assertEqual(self.runner.tick(c, self.cfg)["action"], "paused")
            self.assertEqual(self.unit(c)["status"], "pending")
            self.assertEqual(self.runs(), 0)
            self.config.pause_flag().unlink()
            self.assertEqual(self.runner.tick(c, self.cfg)["outcome"], "done")

    # -- H19: spend ceiling
    def test_spend_ceiling_stops_the_unit_loudly(self):
        with self.ledger.connect() as c:
            self.job(c, 'printf \'{"outcome":"ok","cost_usd":99.5}\' > "$CURATED_DIGEST_RESULT"',
                     self.VERIFY)
            self.runner.tick(c, self.cfg)
            u = self.unit(c)
            self.assertEqual(u["status"], "stuck")       # surfaced, not silent
            self.assertIn("spend ceiling", u["last_error"])

    # -- H12: oldest first
    def test_oldest_eligible_unit_is_claimed_first(self):
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "j", "true", cwd=str(self.dir))
            first = self.ledger.submit(c, "j")
            c.execute("UPDATE units SET next_at=next_at-100 WHERE run_id=?",
                      (first,))
            self.ledger.submit(c, "j")
            self.assertEqual(self.ledger.claim(c)["run_id"], first)

    # -- H10: no in-process waiting
    def test_tick_returns_promptly_when_rate_limited(self):
        with self.ledger.connect() as c:
            self.job(c, "echo \"you've hit your session limit\" >&2; exit 1")
            t0 = time.time()
            self.runner.tick(c, self.cfg)
            self.assertLess(time.time() - t0, 5)   # it exits, it does not sleep

    # -- H5: build units count only when committed
    def test_build_unit_is_done_only_after_a_commit(self):
        repo = self.dir / "repo"
        repo.mkdir()
        os.system(f"cd {repo} && git init -q && git config user.email t@e && "
                  f"git config user.name t && git commit -q --allow-empty -m base")
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "edit", "echo hi > f.txt",
                                   cwd=str(repo), kind="build")
            self.ledger.submit(c, "edit")
            self.runner.tick(c, self.cfg)
            u = self.unit(c, "edit")
            self.assertEqual(u["status"], "pending")     # edited but not committed
            self.assertIn("side effect not observed", u["last_error"])

            self.ledger.define_job(
                c, "edit", "echo hi > f.txt && git add -A && git commit -qm work",
                cwd=str(repo), kind="build")
            self.runner.tick(c, self.cfg)
            self.assertEqual(self.unit(c, "edit")["status"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Confinement(Base):
    """R34: confinement is enforcement, not documentation. The hook is inert
    unless CLAUDE_CONFINE_ROOTS is set, so the runner must set it."""

    def test_runner_sets_confine_roots_by_default(self):
        with self.ledger.connect() as c:
            self.job(c, 'echo "$CLAUDE_CONFINE_ROOTS" > roots.txt', self.VERIFY)
            self.runner.tick(c, self.cfg)
        self.assertIn(str(self.dir), (self.dir / "roots.txt").read_text())

    def test_job_can_narrow_confinement(self):
        with self.ledger.connect() as c:
            self.ledger.define_job(c, "j", 'echo "$CLAUDE_CONFINE_ROOTS" > roots.txt',
                                   verify_cmd=self.VERIFY, cwd=str(self.dir),
                                   confine="/only/here")
            self.ledger.submit(c, "j")
            self.runner.tick(c, self.cfg)
        self.assertEqual((self.dir / "roots.txt").read_text().strip(), "/only/here")

    def test_secrets_are_not_passed_to_jobs(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "should-not-leak"
        try:
            with self.ledger.connect() as c:
                self.job(c, 'echo "${TELEGRAM_BOT_TOKEN:-absent}" > tok.txt',
                         self.VERIFY)
                self.runner.tick(c, self.cfg)
            self.assertEqual((self.dir / "tok.txt").read_text().strip(), "absent")
        finally:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
