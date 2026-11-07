"""init and check: an instance is created outside the repo, credentials are
0600 from the first byte, nothing is overwritten, and every problem check
reports says how to fix it."""
import io
import os
import stat
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest import cli, config, setup  # noqa: E402

VARS = ("CURATED_DIGEST_HOME", "CURATED_DIGEST_TASKS_DIR", "CURATED_DIGEST_STATE_DIR",
        "CURATED_DIGEST_HC_PING_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "GEMINI_API_KEY") + tuple(config.ENV_OVERRIDES.values())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name) / "cfg"
        self.saved = {k: os.environ.pop(k, None) for k in VARS}
        os.environ["CURATED_DIGEST_HOME"] = str(self.home)
        os.environ["CURATED_DIGEST_STATE_DIR"] = str(Path(self.tmp.name) / "state")

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def fill(self, name, text):
        p = self.home / name
        p.write_text(text)
        os.chmod(p, 0o600)


class Init(Base):
    def test_every_credential_block_in_env_example_becomes_a_0600_file(self):
        blocks = setup.env_blocks(setup.ENV_EXAMPLE.read_text())
        self.assertEqual(sorted(blocks), sorted(config.ENV_OVERRIDES))
        rc, _ = self.run_cli("init")
        self.assertEqual(rc, 0)
        for name in blocks:
            mode = stat.S_IMODE((self.home / name).stat().st_mode)
            self.assertEqual(mode, 0o600, name)
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.assertIn("TELEGRAM_BOT_TOKEN=replace-me", (self.home / "telegram.env").read_text())

    def test_init_never_overwrites(self):
        self.home.mkdir(parents=True)
        self.fill("telegram.env", "TELEGRAM_BOT_TOKEN=mine\n")
        _, out = self.run_cli("init")
        self.assertEqual((self.home / "telegram.env").read_text(), "TELEGRAM_BOT_TOKEN=mine\n")
        self.assertIn("kept", out)

    def test_task_is_scaffolded_from_a_template_with_its_own_id(self):
        rc, _ = self.run_cli("init", "--task", "rss-generic", "--id", "my-blog")
        self.assertEqual(rc, 0)
        text = (self.home / "tasks" / "my-blog.yaml").read_text()
        self.assertIn("id: my-blog", text)

    def test_every_shipped_template_loads_as_a_valid_task(self):
        from curated_digest import task
        for name in setup.templates():
            t = task.load_task(setup.TEMPLATES / f"{name}.yaml")
            self.assertEqual(t.id, name)
        self.assertGreaterEqual(len(setup.templates()), 3)

    def test_shipped_templates_are_not_git_ignored(self):
        """A root-level `tasks/` ignore once swallowed templates/tasks/ too, so
        a fresh clone had no templates at all."""
        import shutil
        import subprocess
        if not shutil.which("git") or not (config.REPO / ".git").exists():
            self.skipTest("not a git checkout")
        for p in setup.TEMPLATES.glob("*.yaml"):
            r = subprocess.run(["git", "check-ignore", "-q", str(p)], cwd=config.REPO)
            self.assertNotEqual(r.returncode, 0, f"{p} is git-ignored")

    def test_unknown_template_lists_the_choices(self):
        rc, out = self.run_cli("init", "--task", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("rss-generic", out)

    def test_config_home_inside_the_repo_is_refused(self):
        os.environ["CURATED_DIGEST_HOME"] = str(config.REPO / "mycfg")
        rc, out = self.run_cli("init")
        self.assertEqual(rc, 2)
        self.assertFalse((config.REPO / "mycfg").exists())
        self.assertIn("inside the repository", out)


class Check(Base):
    def test_missing_config_home_says_run_init(self):
        r = setup.check()
        self.assertFalse(r.ok)
        self.assertIn("init", r.errors[0])

    def test_fresh_init_fails_until_credentials_are_filled_in(self):
        self.run_cli("init", "--task", "changelog-watch")
        r = setup.check()
        self.assertTrue(any("TELEGRAM_BOT_TOKEN is not set" in e for e in r.errors))
        self.assertTrue(any("REWRITE:" in w for w in r.warnings))
        self.assertTrue(any("watchdog" in w for w in r.warnings))

    def test_filled_instance_passes(self):
        self.run_cli("init", "--task", "changelog-watch")
        self.fill("telegram.env", "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\n")
        self.fill("gemini.env", "GEMINI_API_KEY=k\n")
        r = setup.check()
        self.assertTrue(r.ok, r.errors)

    def test_world_readable_credentials_are_an_error_with_the_fix(self):
        self.run_cli("init", "--task", "changelog-watch")
        self.fill("telegram.env", "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\n")
        os.chmod(self.home / "telegram.env", 0o644)
        r = setup.check()
        self.assertTrue(any("chmod 600" in e for e in r.errors))

    def test_a_broken_task_is_an_error(self):
        self.run_cli("init")
        (self.home / "tasks" / "bad.yaml").write_text("id: bad\n")
        self.assertTrue(any("task file problem" in e for e in setup.check().errors))

    def test_task_verbs_refuse_a_missing_tasks_dir_with_a_fix(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cli("task", "list")
        self.assertIn("init --task", str(e.exception))


class Demo(Base):
    def test_demo_runs_offline_and_reports_the_injection_without_obeying_it(self):
        from curated_digest import demo, providers
        lines = []
        self.assertEqual(demo.run(out=lines.append), 0)
        body = "\n".join(lines)
        self.assertIn("Act", body)
        self.assertIn("contained text addressed to the filter", body)
        self.assertNotIn("community meetup", body.lower())
        # The stub judge is never left registered for real tasks.
        self.assertNotIn("demo", providers._ADAPTERS)
        self.assertFalse(Path(os.environ["CURATED_DIGEST_STATE_DIR"]).exists())


if __name__ == "__main__":
    unittest.main()
