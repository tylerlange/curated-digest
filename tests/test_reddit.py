"""The Reddit source. No network: every test drives mocked HTTP."""
import io
import json
import os
import sys
import time
import unittest
import unittest.mock
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest.sources import reddit
from curated_digest.sources.base import SourceError


def listing(*posts):
    return json.dumps({"data": {"children": [{"data": p} for p in posts]}}).encode()


def post(**kw):
    d = {"name": "t3_abc", "title": "A tip", "selftext": "body text",
         "permalink": "/r/selfhosted/comments/abc/a_tip/",
         "created_utc": 1756000000, "stickied": False}
    d.update(kw)
    return d


class UrlTargeting(unittest.TestCase):
    def test_a_subreddit_name_is_found_in_the_usual_url_shapes(self):
        for url in ("https://www.reddit.com/r/selfhosted/",
                    "https://www.reddit.com/r/selfhosted",
                    "https://old.reddit.com/r/pixel_phones/"):
            self.assertIn(reddit.target(url)[0], ("selfhosted", "pixel_phones"))

    def test_a_url_with_no_subreddit_is_an_error(self):
        with self.assertRaises(SourceError):
            reddit.target("https://example.com/feed.xml")

    def test_sort_is_read_off_the_url(self):
        """Measured: /new on a busy subreddit returns questions and
        availability chatter, so the sort is not a cosmetic detail."""
        self.assertEqual(reddit.target("https://reddit.com/r/x/new/")[1], "new")
        self.assertEqual(reddit.target("https://reddit.com/r/x/top/?t=week")[1], "top")

    def test_a_bare_subreddit_url_defaults_to_hot_not_new(self):
        self.assertEqual(reddit.target("https://reddit.com/r/x/")[1], "hot")

    def test_top_defaults_to_a_week_window(self):
        sub, sort, window = reddit.target("https://reddit.com/r/x/top/")
        self.assertEqual((sort, window), ("top", "week"))

    def test_an_unknown_window_falls_back_rather_than_being_sent(self):
        """Reddit 400s on a bad t=, which would look like a broken source."""
        self.assertEqual(reddit.target("https://reddit.com/r/x/top/?t=fortnight")[2],
                         "week")

    def test_only_top_carries_a_window(self):
        self.assertIsNone(reddit.target("https://reddit.com/r/x/hot/")[2])


class Parsing(unittest.TestCase):
    def test_a_listing_becomes_items(self):
        items = reddit.parse(listing(post()), "s")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "t3_abc")
        self.assertEqual(items[0].title, "A tip")
        self.assertTrue(items[0].url.startswith("https://www.reddit.com/r/"))
        self.assertEqual(items[0].published_at, 1756000000)

    def test_the_id_is_reddits_fullname_so_an_edit_cannot_resurrect_a_post(self):
        """Unlike the RSS link+title fallback, this is genuinely stable."""
        a = reddit.parse(listing(post(title="original")), "s")[0]
        b = reddit.parse(listing(post(title="edited later")), "s")[0]
        self.assertEqual(a.id, b.id)

    def test_stickied_posts_are_skipped(self):
        """Pinned mod threads are never the tip we are looking for."""
        self.assertEqual(reddit.parse(listing(post(stickied=True)), "s"), [])

    def test_flair_is_kept_because_it_carries_signal(self):
        items = reddit.parse(listing(post(link_flair_text="Guide")), "s")
        self.assertIn("[Guide]", items[0].body)

    def test_an_empty_subreddit_is_not_an_error(self):
        """R36 again: quiet and broken must not look the same."""
        self.assertEqual(reddit.parse(listing(), "s"), [])

    def test_a_non_listing_payload_is_an_error(self):
        for raw in (b"<html>rate limited</html>", b'{"error": 429}', b"{}"):
            with self.subTest(raw=raw):
                with self.assertRaises(SourceError):
                    reddit.parse(raw, "s")

    def test_a_post_with_no_id_is_skipped_rather_than_crashing(self):
        bad = post()
        del bad["name"]
        self.assertEqual(reddit.parse(listing(bad), "s"), [])


class Auth(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        os.environ["CURATED_DIGEST_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        self.env = unittest.mock.patch.object(
            reddit, "_env", return_value={
                "REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": "sec",
                "REDDIT_USER_AGENT": "linux:curated-digest:1.0 (by /u/t)"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        os.environ.pop("CURATED_DIGEST_STATE_DIR", None)

    def _token_resp(self, expires_in=3600):
        m = unittest.mock.MagicMock()
        m.__enter__.return_value.read.return_value = json.dumps(
            {"access_token": "TOK", "expires_in": expires_in}).encode()
        return m

    def test_missing_credentials_fail_before_any_request(self):
        with unittest.mock.patch.object(reddit, "_env", return_value={}), \
             unittest.mock.patch("urllib.request.urlopen") as up:
            with self.assertRaises(SourceError):
                reddit._token()
        up.assert_not_called()

    def test_the_token_is_cached_rather_than_re_minted_per_subreddit(self):
        """Three subreddits per digest; minting three tokens would spend the
        rate limit this module exists to avoid."""
        with unittest.mock.patch("urllib.request.urlopen",
                                 return_value=self._token_resp()) as up:
            self.assertEqual(reddit._token()[0], "TOK")
            self.assertEqual(reddit._token()[0], "TOK")
        self.assertEqual(up.call_count, 1)

    def test_an_expiring_token_is_refreshed(self):
        with unittest.mock.patch("urllib.request.urlopen",
                                 return_value=self._token_resp(expires_in=0)) as up:
            reddit._token()
            reddit._token()
        self.assertEqual(up.call_count, 2)

    def test_the_cached_token_is_not_world_readable(self):
        from curated_digest import config
        with unittest.mock.patch("urllib.request.urlopen",
                                 return_value=self._token_resp()):
            reddit._token()
        mode = (config.state_dir() / "reddit-token.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_a_401_discards_the_cached_token(self):
        """Otherwise a stale token fails identically forever."""
        from curated_digest import config
        with unittest.mock.patch("urllib.request.urlopen",
                                 return_value=self._token_resp()):
            reddit._token()
        cache = config.state_dir() / "reddit-token.json"
        self.assertTrue(cache.exists())
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        with unittest.mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SourceError):
                reddit.fetch("https://www.reddit.com/r/selfhosted/")
        self.assertFalse(cache.exists())

    def test_fetch_sends_the_bearer_token_and_a_descriptive_user_agent(self):
        """Reddit throttles generic user agents harder."""
        with unittest.mock.patch("urllib.request.urlopen",
                                 return_value=self._token_resp()):
            reddit._token()
        resp = unittest.mock.MagicMock()
        resp.__enter__.return_value.read.return_value = listing(post())
        with unittest.mock.patch("urllib.request.urlopen",
                                 return_value=resp) as up:
            reddit.fetch("https://www.reddit.com/r/selfhosted/")
        req = up.call_args[0][0]
        self.assertEqual(req.headers["Authorization"], "Bearer TOK")
        self.assertIn("curated-digest", req.headers["User-agent"])
        self.assertIn("oauth.reddit.com", req.full_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
