"""R8/R46: `curated-digest task run --model provider:model`.

The flag exists so a task can be tried against a different model without
editing its YAML, which means the test that matters is not "argparse accepted
the string" — it is that the string reached the provider and that nothing kept
it afterwards. A flag that parses but silently curates with the file's model
would look identical from the outside.
"""
import io
import json
import sys
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest import cli, task                                   # noqa: E402
from curated_digest.providers import register                          # noqa: E402
from curated_digest.providers.base import Completion                   # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TASK = """\
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


class RecordingProvider:
    """Records the model it was handed. Registered under its own name so the
    override test cannot pass by accidentally hitting some other adapter."""
    name = "recorder"
    models = []

    def complete(self, prompt, model, *, system=None, max_tokens=2048, timeout=180):
        RecordingProvider.models.append(model)
        return Completion(text="", model=model, cost_usd=0.0)


register("recorder", RecordingProvider)


class ModelOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks_dir = root / "tasks"
        self.tasks_dir.mkdir()
        self.task_file = self.tasks_dir / "probe.yaml"
        self.task_file.write_text(TASK)

        env = unittest.mock.patch.dict(
            "os.environ", {"CURATED_DIGEST_TASKS_DIR": str(self.tasks_dir),
                           "CURATED_DIGEST_STATE_DIR": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)

        # The one seam that would otherwise reach the network. Everything from
        # parse onwards runs for real, so the model spec travels the same path
        # a scheduled run would take it down.
        feed = (FIXTURES / "rss2.xml").read_bytes()
        fetch = unittest.mock.patch("curated_digest.dispatch.rss.fetch",
                                    return_value=(feed, {}))
        fetch.start()
        self.addCleanup(fetch.stop)

        RecordingProvider.models = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["task", "run", "probe", *args])
        return code, buf.getvalue()

    def test_without_the_flag_the_file_decides(self):
        """Guards the baseline: if this ever stops being 'cheap', the override
        test below proves nothing."""
        code, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(RecordingProvider.models, ["cheap"])

    def test_the_override_reaches_the_provider(self):
        code, _ = self._run("--dry-run", "--model", "recorder:expensive")
        self.assertEqual(code, 0)
        self.assertEqual(RecordingProvider.models, ["expensive"])

    def test_the_override_is_not_persisted(self):
        """It is a way to try a model, not a way to change one. The next run —
        scheduled, with no flag — must be back on the file's model."""
        self._run("--dry-run", "--model", "recorder:expensive")
        self.assertEqual(load_model(self.task_file), "recorder:cheap")

        RecordingProvider.models = []
        self._run("--dry-run")
        self.assertEqual(RecordingProvider.models, ["cheap"])

    def test_a_bare_model_name_is_refused(self):
        """R8: no default to fall back on. Refusing on the command line as well
        as in the schema is what keeps the flag from being the hole in it."""
        code, out = self._run("--dry-run", "--model", "expensive")
        self.assertEqual(code, 2)
        self.assertIn("provider:model", json.loads(out)["error"])
        # Refused before anything was spent, not after.
        self.assertEqual(RecordingProvider.models, [])

    def test_a_half_written_override_is_refused(self):
        for bad in ("recorder:", ":expensive", ":"):
            with self.subTest(bad=bad):
                RecordingProvider.models = []
                code, _ = self._run("--dry-run", "--model", bad)
                self.assertEqual(code, 2)
                self.assertEqual(RecordingProvider.models, [])

    def test_an_unknown_provider_fails_loudly(self):
        """Parsing is not the same as existing. `resolve` refuses the unknown
        provider, and that must surface rather than be swallowed into a
        run that reports success with nothing curated."""
        code, out = self._run("--dry-run", "--model", "nosuch:model")
        self.assertEqual(code, 1)
        self.assertIn("provider_error", out)
        self.assertIn("unknown provider", out)


def load_model(path) -> str:
    return task.load_task(path).model


if __name__ == "__main__":
    unittest.main()
