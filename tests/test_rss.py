"""R36: the whole point of this source is that broken and empty must never
look the same. Every failure-mode test here checks that distinction."""
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest.sources.base import SourceError, item_id
from curated_digest.sources import rss

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class ParseRss2(unittest.TestCase):
    def setUp(self):
        self.items = rss.parse(load("rss2.xml"), "src-rss")

    def test_count(self):
        self.assertEqual(len(self.items), 2)

    def test_first_item_fields(self):
        it = self.items[0]
        self.assertEqual(it.source_id, "src-rss")
        self.assertEqual(it.url, "https://example.com/posts/1")
        self.assertEqual(it.title, "First & Only Post")
        self.assertEqual(it.published_at, 1768206600)

    def test_body_tags_stripped_and_entities_decoded(self):
        it = self.items[0]
        self.assertNotIn("<", it.body)
        self.assertNotIn(">", it.body)
        self.assertIn("Hello & welcome to the post.", it.body)

    def test_content_encoded_preferred_over_description(self):
        it = self.items[1]
        self.assertIn("Full body with extra spaces.", it.body)
        self.assertNotIn("Teaser only", it.body)

    def test_unparseable_date_yields_none_not_now(self):
        self.assertIsNone(self.items[1].published_at)

    def test_guid_used_as_id(self):
        self.assertEqual(self.items[0].id, "urn:uuid:abc-123")

    def test_missing_guid_falls_back_to_hash_and_is_stable(self):
        it = self.items[1]
        expected = item_id(None, it.url, it.title)
        self.assertEqual(it.id, expected)
        # parsing the same fixture again must yield the same id (dedup depends on this)
        again = rss.parse(load("rss2.xml"), "src-rss")[1]
        self.assertEqual(it.id, again.id)


class ParseAtom(unittest.TestCase):
    def setUp(self):
        self.items = rss.parse(load("atom.xml"), "src-atom")

    def test_count(self):
        self.assertEqual(len(self.items), 2)

    def test_first_entry_fields(self):
        it = self.items[0]
        self.assertEqual(it.title, "Atom & Entry One")
        self.assertEqual(it.id, "urn:uuid:entry-1")
        self.assertEqual(it.published_at, 1768206600)

    def test_rel_alternate_link_preferred_over_rel_self(self):
        self.assertEqual(self.items[0].url, "https://example.com/entries/1")

    def test_body_from_content_tags_stripped(self):
        self.assertEqual(self.items[0].body, "Body of entry one.")

    def test_second_entry_uses_summary_and_updated_and_no_id(self):
        it = self.items[1]
        self.assertEqual(it.body, "Summary text for entry two.")
        self.assertEqual(it.published_at, 1768288500)
        self.assertEqual(it.url, "https://example.com/entries/2")
        # no <id> in the fixture entry -> hash fallback, not a crash
        self.assertEqual(it.id, item_id(None, it.url, it.title))


class BrokenVsEmpty(unittest.TestCase):
    def test_zero_entries_is_not_an_error(self):
        self.assertEqual(rss.parse(load("empty.xml"), "src"), [])

    def test_unparseable_xml_raises(self):
        with self.assertRaises(SourceError):
            rss.parse(load("not_xml.txt"), "src")

    def test_html_error_page_raises(self):
        with self.assertRaises(SourceError):
            rss.parse(load("error_page.html"), "src")

    def test_source_error_carries_source_id(self):
        try:
            rss.parse(load("not_xml.txt"), "my-source")
        except SourceError as e:
            self.assertEqual(e.source_id, "my-source")
        else:
            self.fail("expected SourceError")


class ItemId(unittest.TestCase):
    def test_guid_wins_when_present(self):
        self.assertEqual(item_id("g1", "https://x/1", "t"), "g1")

    def test_hash_fallback_is_deterministic(self):
        a = item_id(None, "https://x/1", "title")
        b = item_id(None, "https://x/1", "title")
        self.assertEqual(a, b)

    def test_hash_fallback_differs_for_different_input(self):
        a = item_id(None, "https://x/1", "title")
        b = item_id(None, "https://x/2", "title")
        self.assertNotEqual(a, b)

    def test_empty_guid_treated_as_absent(self):
        a = item_id("", "https://x/1", "title")
        b = item_id(None, "https://x/1", "title")
        self.assertEqual(a, b)


class FakeResponse:
    """Stand-in for the object urllib.request.urlopen's context manager
    yields, just enough surface (.read, .headers) for _read_capped/fetch."""

    def __init__(self, body: bytes, headers=None):
        self._body = body
        self._pos = 0
        self.headers = headers or {}

    def read(self, n):
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FetchCapEnforcement(unittest.TestCase):
    def test_max_bytes_overrun_raises(self):
        big = b"x" * 100
        with unittest.mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse(big)
        ):
            with self.assertRaises(SourceError):
                rss.fetch("https://example.com/feed", max_bytes=10)

    def test_under_cap_succeeds(self):
        body = b"<rss><channel></channel></rss>"
        with unittest.mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse(body)
        ):
            raw, headers = rss.fetch("https://example.com/feed", max_bytes=1000)
        self.assertEqual(raw, body)


if __name__ == "__main__":
    unittest.main()


class NestedMarkupAndHostileInput(unittest.TestCase):
    """Two things real feeds do that a naive parser gets wrong: put actual XML
    children inside the body, and be hostile."""

    XHTML = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:1</id>
    <title>Nested</title>
    <link rel="alternate" href="https://example.com/1"/>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
      <p>First paragraph.</p><p>Second paragraph.</p></div></content>
  </entry>
</feed>"""

    def test_xhtml_content_keeps_text_after_the_first_child(self):
        from curated_digest.sources import rss
        item = rss.parse(self.XHTML, "s")[0]
        self.assertIn("First paragraph.", item.body)
        self.assertIn("Second paragraph.", item.body)   # .text alone loses this

    def test_billion_laughs_does_not_hang_or_expand(self):
        """A feed is untrusted input parsed in-process. This must fail fast,
        not consume the box."""
        from curated_digest.sources import rss
        bomb = b"""<?xml version="1.0"?>
<!DOCTYPE rss [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
  <!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
]>
<rss version="2.0"><channel><item><title>&e;</title></item></channel></rss>"""
        t0 = time.time()
        with self.assertRaises(SourceError):
            rss.parse(bomb, "s")
        self.assertLess(time.time() - t0, 5)

    def test_entities_really_do_expand_without_the_guard(self):
        """Pins the reason the guard exists. If ElementTree ever stops
        expanding, this fails and the DTD rejection can be reconsidered —
        until then, removing the guard reintroduces a memory bomb."""
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            '<!DOCTYPE r [<!ENTITY a "0123456789">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            '<r>&b;</r>')
        self.assertEqual(len(root.text), 100)

    def test_external_entity_is_not_resolved(self):
        """XXE: a feed must not be able to make us read /etc/passwd."""
        from curated_digest.sources import rss
        xxe = (b'<?xml version="1.0"?>\n<!DOCTYPE rss ['
               b'<!ENTITY x SYSTEM "file:///etc/passwd">]>\n'
               b'<rss version="2.0"><channel><item><title>&x;</title>'
               b'</item></channel></rss>')
        with self.assertRaises(SourceError):
            rss.parse(xxe, "s")
