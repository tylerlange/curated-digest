"""R27: one feed item, several findings.

The Claude what's-new feed publishes one entry a week holding four or five
unrelated features. Curation used to be allowed exactly one judgment per item,
so the whole week arrived as a single 150-char bullet — "Fork mode is default;
auto-continue limit retry" — which is the flat, unreadable failure the
editorial spec exists to prevent.

Three levels are covered here because the change touches three: the parser must
stop collapsing on id, the renderer must lay several findings out across tiers,
and the store must not write an item off as rejected while one of its findings
is still undelivered. The last one is the dangerous one — it loses a finding
permanently and looks like nothing happened.
"""
import json
import os
import re
import sys
import unittest
import unittest.mock
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest import curate                                      # noqa: E402
from curated_digest.curate import Judgment                             # noqa: E402
from curated_digest.providers import register                          # noqa: E402
from curated_digest.providers.base import Completion                   # noqa: E402
from curated_digest.renderers import telegram as renderer              # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ITEM_RE = re.compile(r'<untrusted_item id="([^"]+)">')


@dataclass(frozen=True)
class It:
    id: str
    title: str = "Weekly release notes"
    url: str = "https://example.com"


def obj(iid, *, keep=True, tier="act", line="", injection=False):
    return json.dumps({"id": iid, "keep": keep, "tier": tier,
                       "line": line, "injection": injection})


class ParsingSeveralFindingsPerItem(unittest.TestCase):
    def test_two_findings_for_one_id_both_survive(self):
        """The regression this file exists for. Collapsing on id kept the
        first change of the week and silently dropped the other four."""
        text = "\n".join([
            obj("a", tier="act", line="Subagents now run in parallel by default"),
            obj("a", tier="fyi", line="The /cost command reports per-session spend"),
        ])
        got = curate.parse_judgments(text, {"a"})
        self.assertEqual(len(got), 2)
        self.assertEqual([j.tier for j in got], ["act", "fyi"])

    def test_each_finding_keeps_its_own_tier_and_line(self):
        """Tier is per finding, not per item: one change in a release note can
        be worth acting on while the rest of it is trivia."""
        text = "\n".join([obj("a", tier="fyi", line="minor"),
                          obj("a", tier="act", line="breaking change")])
        act = [j for j in curate.parse_judgments(text, {"a"}) if j.tier == "act"]
        self.assertEqual([j.line for j in act], ["breaking change"])

    def test_the_same_sentence_twice_is_still_one_finding(self):
        """A model repeating itself is not a second change. Identity is
        (id, line), so this must still collapse."""
        line = obj("a", line="Subagents now run in parallel")
        self.assertEqual(len(curate.parse_judgments(f"{line}\n{line}", {"a"})), 1)

    def test_findings_per_item_are_capped(self):
        """Attacker-influenceable: a hostile item emitting fifty findings would
        fill a digest that has room for nine bullets and crowd out every other
        source without ever tripping the injection flag."""
        text = "\n".join(obj("a", line=f"change {i}") for i in range(20))
        got = curate.parse_judgments(text, {"a"})
        self.assertEqual(len(got), curate.MAX_FINDINGS_PER_ITEM)

    def test_the_cap_is_per_item_not_per_digest(self):
        """Capping globally would drop whole sources on a busy day."""
        text = "\n".join(obj(i, line=f"change {i}") for i in "abcdefgh")
        self.assertEqual(len(curate.parse_judgments(text, set("abcdefgh"))), 8)

    def test_the_prompt_asks_for_one_object_per_change(self):
        """The parser can only split what the model actually sends. If the
        instruction goes missing the behaviour reverts with every test above
        still passing."""
        self.assertIn("ONE OBJECT PER CHANGE", curate.TASK)
        self.assertIn(str(curate.MAX_FINDINGS_PER_ITEM), curate.TASK)


class RenderingSeveralFindingsPerItem(unittest.TestCase):
    def test_one_item_can_produce_bullets_in_both_tiers(self):
        js = [Judgment("a", True, "act", "w", False, "Parallel subagents, on by default"),
              Judgment("a", True, "fyi", "w", False, "/cost now reports session spend")]
        body = renderer.render(js, {"a": It("a")}, heading="Claude").body
        self.assertEqual(body.count("•"), 2)
        # Each finding lands in the tier it earned, so the actionable half of a
        # release note is not buried under the trivia it shipped with.
        self.assertLess(body.index("Parallel subagents"), body.index("Worth knowing"))
        self.assertLess(body.index("Worth knowing"), body.index("/cost now reports"))

    def test_the_injection_warning_counts_items_not_findings(self):
        """One item that contained an instruction is one item, however many
        changes were pulled out of it. The number is the entire information
        content of that line."""
        js = [Judgment("a", True, "act", "w", True, "one"),
              Judgment("a", True, "fyi", "w", True, "two")]
        body = renderer.render(js, {"a": It("a")}, heading="Claude").body
        self.assertIn("1 item(s)", body)


class StoreStateAcrossSeveralFindings(unittest.TestCase):
    """The dangerous half. `mark_judged` writes an item off permanently and is
    not gated on delivery (nothing was delivered, so nothing can be lost) — but
    that reasoning only holds while a rejection means the *whole item* was
    rejected."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        os.environ["CURATED_DIGEST_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        from curated_digest import dispatch, ledger, store, task
        self.dispatch, self.store, self.task_mod = dispatch, store, task
        self.ctx = ledger.connect()
        self.c = self.ctx.__enter__()
        store.ensure_schema(self.c)

        feed = (FIXTURES / "rss2.xml").read_bytes()
        for target in ("curated_digest.dispatch.rss.fetch",):
            p = unittest.mock.patch(target, return_value=(feed, {}))
            p.start()
            self.addCleanup(p.stop)

        self.sent = []
        channel = unittest.mock.Mock()
        channel.send.side_effect = lambda art: (self.sent.append(art.body), True)[1]
        p = unittest.mock.patch.dict(dispatch.CHANNELS, {"telegram": channel})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("CURATED_DIGEST_STATE_DIR", None)

    def _task(self):
        t = self.task_mod
        return t.Task(id="probe",
                      sources=(t.Source("probe", "rss", "https://example.com/f.xml"),),
                      criteria="anything", schedule="every 30m",
                      model="splitter:m", renderer="telegram", channel="telegram",
                      urgency="digest", heading="Probe")

    def _reported(self):
        return {r["item_id"]: r["reason"] for r in
                self.c.execute("SELECT item_id, reason FROM reported")}

    def test_a_kept_finding_protects_its_item_from_being_written_off(self):
        """One judgment says drop, another says keep. Marking the item judged
        on the first would settle it before delivery, and the kept finding
        would never come back — R14's guarantee bypassed for exactly the item
        it protects."""
        SplittingProvider.reply = lambda ids: "\n".join([
            obj(ids[0], keep=False, line="not interesting"),
            obj(ids[0], keep=True, tier="act", line="this one matters"),
        ])
        out = self.dispatch.run_task(self.c, self._task())
        self.assertTrue(out["delivered"])

        first = ITEM_RE.findall(SplittingProvider.last_prompt)[0]
        self.assertEqual(self._reported()[first], "delivered")

    def test_both_findings_from_one_item_reach_the_digest(self):
        SplittingProvider.reply = lambda ids: "\n".join([
            obj(ids[0], tier="act", line="Parallel subagents, on by default"),
            obj(ids[0], tier="fyi", line="/cost reports session spend"),
        ])
        self.dispatch.run_task(self.c, self._task())
        self.assertIn("Parallel subagents", self.sent[0])
        self.assertIn("/cost reports", self.sent[0])

    def test_an_item_reported_once_is_never_reported_twice(self):
        """R12. Several findings naming one item must not become several
        `reported` rows, and the item must not reappear in the next run's
        candidates."""
        SplittingProvider.reply = lambda ids: "\n".join([
            obj(ids[0], tier="act", line="one"), obj(ids[0], tier="fyi", line="two"),
            obj(ids[1], keep=False, line=""),
        ])
        self.dispatch.run_task(self.c, self._task())
        rows = self.c.execute("SELECT COUNT(*) n FROM reported "
                              "WHERE reason='delivered'").fetchone()["n"]
        self.assertEqual(rows, 1)
        self.assertEqual(self.store.candidates(self.c, "probe", ["probe"]), [])

    def test_an_item_the_cap_held_back_is_not_written_off(self):
        """The render-seam version of R14. The digest caps its tiers, so a run
        can keep more than it shows. Marking the overflow reported writes it
        off unseen — it stays a candidate and arrives in a later digest."""
        # Two items of three findings each: six act findings compete for four
        # slots, so the second item does not fit this run.
        SplittingProvider.reply = lambda ids: "\n".join(
            [obj(ids[0], tier="act", line=f"first item finding {n}")
             for n in range(curate.MAX_FINDINGS_PER_ITEM)]
            + [obj(ids[1], tier="act", line=f"second item, no room {n}")
               for n in range(curate.MAX_FINDINGS_PER_ITEM)])

        out = self.dispatch.run_task(self.c, self._task())
        self.assertTrue(out["delivered"])
        self.assertNotIn("second item, no room", self.sent[0])
        self.assertEqual(self.sent[0].count("•"), curate.MAX_FINDINGS_PER_ITEM)

        shown, held = ITEM_RE.findall(SplittingProvider.last_prompt)[:2]
        self.assertEqual(self._reported(), {shown: "delivered"})
        self.assertEqual([r["item_id"] for r in
                          self.store.candidates(self.c, "probe", ["probe"])],
                         [held])
        self.assertEqual(out.get("held_back"), 1)

    def test_an_item_every_finding_rejected_is_still_written_off(self):
        """The other direction: without this, rejected items are re-judged
        forever and cost stops scaling with new candidates (R22)."""
        SplittingProvider.reply = lambda ids: "\n".join(
            obj(i, keep=False, line="no") for i in ids)
        self.dispatch.run_task(self.c, self._task())
        self.assertEqual(set(self._reported().values()), {"judged"})


class SplittingProvider:
    """Replies as a function of the ids it was actually shown, so the tests
    never hardcode a feed GUID and cannot pass by matching the wrong item."""
    name = "splitter"
    reply = staticmethod(lambda ids: "")
    last_prompt = ""

    def complete(self, prompt, model, *, system=None, max_tokens=2048, timeout=180):
        SplittingProvider.last_prompt = prompt
        ids = ITEM_RE.findall(prompt)
        return Completion(text=SplittingProvider.reply(ids), model=model,
                          cost_usd=0.0)


register("splitter", SplittingProvider)


if __name__ == "__main__":
    unittest.main(verbosity=2)
