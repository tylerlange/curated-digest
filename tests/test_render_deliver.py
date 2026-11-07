"""R3/R4/R14/R26/R27.

The delivery tests are mostly about one thing: `send` must not claim success it
has not observed, because the caller advances 'already reported' state on that
return value and a lost finding never comes back."""
import io
import json
import re
import sys
import unittest
import unittest.mock
import urllib.error
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest.channels import telegram as channel
from curated_digest.channels.telegram import DeliveryError
from curated_digest.curate import Judgment
from curated_digest.renderers import Artifact, telegram as renderer


@dataclass(frozen=True)
class It:
    id: str
    title: str
    url: str = "https://example.com"


def items(*pairs):
    return {i: It(i, t) for i, t in pairs}


def visible(line: str) -> str:
    """Line as rendered on the phone: markup stripped, entities decoded."""
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", line))


class Rendering(unittest.TestCase):
    def test_nothing_kept_renders_nothing_at_all(self):
        """R26: silence is the correct outcome. An empty digest would train
        the reader to ignore the channel that also carries real alerts."""
        out = renderer.render([Judgment("a", False, "fyi", "no")],
                              items(("a", "T")), heading="Claude")
        self.assertIsNone(out)

    def test_act_comes_before_worth_knowing(self):
        js = [Judgment("a", True, "fyi", "interesting"),
              Judgment("b", True, "act", "do this")]
        body = renderer.render(js, items(("a", "Aye"), ("b", "Bee")),
                               heading="Claude").body
        self.assertLess(body.index("Act"), body.index("Worth knowing"))
        self.assertLess(body.index("Bee"), body.index("Aye"))

    def test_an_empty_tier_drops_its_header(self):
        """A good week is not a formatting problem."""
        body = renderer.render([Judgment("a", True, "fyi", "w")],
                               items(("a", "T")), heading="Claude").body
        self.assertNotIn("Act", body)
        self.assertIn("Worth knowing", body)

    def test_tiers_are_capped(self):
        js = ([Judgment(f"a{i}", True, "act", "w") for i in range(9)]
              + [Judgment(f"f{i}", True, "fyi", "w") for i in range(9)])
        by_id = {j.item_id: It(j.item_id, f"Title {j.item_id}") for j in js}
        body = renderer.render(js, by_id, heading="Claude").body
        self.assertEqual(body.count("•"), renderer.MAX_ACT + renderer.MAX_FYI)

    def test_long_titles_are_truncated_to_one_line(self):
        """LINE_CHARS budgets what the reader reads, so measure the visible text."""
        body = renderer.render([Judgment("a", True, "act", "w")],
                               items(("a", "T" * 500)), heading="C").body
        self.assertTrue(all(len(visible(l)) <= renderer.LINE_CHARS
                            for l in body.split("\n")))

    def test_each_bullet_links_to_its_own_item(self):
        """The bug this replaced: one footer link on a three-subreddit digest
        put every Linux tip under a link to r/selfhosted."""
        by_id = {"a": It("a", "Aye", "https://reddit.com/r/selfhosted/x"),
                 "b": It("b", "Bee", "https://reddit.com/r/linux/y")}
        art = renderer.render([Judgment("a", True, "act", "w"),
                               Judgment("b", True, "fyi", "w")],
                              by_id, heading="Reddit")
        self.assertEqual(art.kind, "html")
        self.assertIn('<a href="https://reddit.com/r/selfhosted/x">Aye', art.body)
        self.assertIn('<a href="https://reddit.com/r/linux/y">Bee', art.body)

    def test_curator_text_is_escaped(self):
        """Curator output is untrusted: unescaped markup is a 400 from Telegram
        and a digest that never arrives."""
        body = renderer.render([Judgment("a", True, "act", "w")],
                               {"a": It("a", "Tom & <b>Jerry</b>")},
                               heading="C & D").body
        self.assertIn("Tom &amp; &lt;b&gt;Jerry&lt;/b&gt;", body)
        self.assertIn("C &amp; D", body)
        self.assertNotIn("<b>", body)

    def test_a_non_http_url_is_not_linked(self):
        body = renderer.render([Judgment("a", True, "act", "w")],
                               {"a": It("a", "Aye", "javascript:alert(1)")},
                               heading="C").body
        self.assertNotIn("<a href", body)
        self.assertIn("• Aye", body)

    def test_an_item_with_no_url_still_gets_a_bullet(self):
        body = renderer.render([Judgment("a", True, "act", "w")],
                               {"a": It("a", "Aye", "")}, heading="C").body
        self.assertIn("• Aye", body)

    def test_only_the_items_it_showed_are_reported(self):
        """The caller marks `rendered_ids` reported. Anything the cap held back
        must not be in there, or it is written off unseen."""
        js = [Judgment(str(i), True, "fyi", "w") for i in range(9)]
        by_id = {str(i): It(str(i), f"T{i}") for i in range(9)}
        art = renderer.render(js, by_id, heading="C")
        self.assertEqual(len(art.rendered_ids), renderer.MAX_FYI)
        for iid in art.rendered_ids:
            self.assertIn(f"T{iid}", art.body)

    def test_an_item_is_held_back_whole_rather_than_split(self):
        """'Reported' is recorded per item, so showing three of an item's four
        findings and reporting it loses the fourth. It waits instead."""
        js = ([Judgment("a", True, "act", "w") for _ in range(3)]
              + [Judgment("b", True, "act", "w") for _ in range(2)])
        art = renderer.render(js, items(("a", "Aye"), ("b", "Bee")), heading="C")
        self.assertEqual(art.rendered_ids, ("a",))
        self.assertNotIn("Bee", art.body)

    def test_an_item_bigger_than_the_whole_digest_is_still_shown(self):
        """Holding it back would starve it: it will not fit next run either."""
        js = [Judgment("a", True, "act", "w") for _ in range(renderer.MAX_ACT + 2)]
        art = renderer.render(js, items(("a", "Aye")), heading="C")
        self.assertEqual(art.rendered_ids, ("a",))
        self.assertEqual(art.body.count("•"), renderer.MAX_ACT)

    def test_a_missing_item_does_not_consume_a_slot(self):
        js = ([Judgment("ghost", True, "act", "w")]
              + [Judgment(str(i), True, "act", "w") for i in range(renderer.MAX_ACT)])
        by_id = {str(i): It(str(i), f"T{i}") for i in range(renderer.MAX_ACT)}
        art = renderer.render(js, by_id, heading="C")
        self.assertEqual(art.body.count("•"), renderer.MAX_ACT)
        self.assertNotIn("ghost", art.rendered_ids)

    def test_a_broken_source_is_reported_even_with_no_items(self):
        """R36/R38: this is the case where silence is actively wrong."""
        out = renderer.render([], {}, heading="Claude",
                              broken_sources=[{"source_id": "blog",
                                               "consecutive_failures": 5}])
        self.assertIsNotNone(out)
        self.assertIn("Not working", out.body)
        self.assertIn("blog", out.body)

    def test_an_injection_attempt_is_surfaced(self):
        out = renderer.render(
            [Judgment("a", True, "fyi", "w", injection=True)],
            items(("a", "T")), heading="Claude")
        self.assertIn("addressed to the filter", out.body)

    def test_a_missing_item_does_not_crash_the_digest(self):
        """The renderer takes judgments and items from different places; a
        mismatch must degrade, not take down the whole delivery."""
        out = renderer.render([Judgment("ghost", True, "act", "w"),
                               Judgment("a", True, "act", "w")],
                              items(("a", "Real")), heading="C")
        self.assertIn("Real", out.body)


class Delivery(unittest.TestCase):
    def setUp(self):
        self.env = unittest.mock.patch.dict(
            "os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"})
        self.env.start()
        self.art = Artifact("hello")

    def tearDown(self):
        self.env.stop()

    def _resp(self, ok=True):
        m = unittest.mock.MagicMock()
        m.__enter__.return_value.read.return_value = json.dumps({"ok": ok}).encode()
        return m

    def test_confirmed_send_returns_true(self):
        with unittest.mock.patch("urllib.request.urlopen", return_value=self._resp()):
            self.assertTrue(channel.send(self.art))

    def test_telegram_saying_not_ok_is_a_failure(self):
        """HTTP 200 with {"ok": false} is Telegram's way of refusing. Treating
        the status code as success loses the finding."""
        with unittest.mock.patch("urllib.request.urlopen",
                                 return_value=self._resp(ok=False)):
            with self.assertRaises(DeliveryError):
                channel.send(self.art)

    def test_a_4xx_is_not_retried(self):
        err = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b"{}"))
        with unittest.mock.patch("urllib.request.urlopen", side_effect=err) as up:
            with self.assertRaises(DeliveryError):
                channel.send(self.art, sleep=lambda _: None)
        self.assertEqual(up.call_count, 1)

    def test_a_5xx_is_retried_then_gives_up_loudly(self):
        err = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b"{}"))
        with unittest.mock.patch("urllib.request.urlopen", side_effect=err) as up:
            with self.assertRaises(DeliveryError):
                channel.send(self.art, sleep=lambda _: None)
        self.assertEqual(up.call_count, channel.RETRIES)

    def test_a_transient_failure_that_recovers_succeeds(self):
        err = urllib.error.URLError("boom")
        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=[err, self._resp()]):
            self.assertTrue(channel.send(self.art, sleep=lambda _: None))

    def test_a_long_digest_is_split_on_line_boundaries(self):
        body = "\n".join(f"• bullet number {i}" for i in range(600))
        with unittest.mock.patch("urllib.request.urlopen",
                                 return_value=self._resp()) as up:
            self.assertTrue(channel.send(Artifact(body)))
        self.assertGreater(up.call_count, 1)
        for call in up.call_args_list:
            sent = call[0][0].data.decode()
            self.assertNotIn("bullet+number+%0A", sent)   # never cut mid-line

    def test_missing_credentials_fail_loudly_before_any_request(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True), \
             unittest.mock.patch.object(channel, "ENV_FILE", Path("/nonexistent")):
            with self.assertRaises(DeliveryError):
                channel.send(self.art)


if __name__ == "__main__":
    unittest.main(verbosity=2)
