"""D5: RSS/Atom, the first source. No feedparser — stdlib only, so this
hand-rolls the (small) parts of RSS 2.0 / Atom that actually show up in the
wild, deliberately staying loose about the rest."""
import html
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .base import Item, SourceError, item_id

USER_AGENT = "curated-digest/1 (RSS reader)"

# content:encoded lives in this namespace; we only care about its localname.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.I)


def fetch(url, *, timeout=20, max_bytes=5_000_000, etag=None, modified=None):
    """Returns (raw_bytes_or_None, headers). None body means 304 — unchanged,
    not an error, so the caller must not treat it as a failure. Any HTTP
    error, timeout, or connection failure raises SourceError instead of
    propagating urllib's exception types, so callers have one thing to catch."""
    req_headers = {"User-Agent": USER_AGENT}
    if etag:
        req_headers["If-None-Match"] = etag
    if modified:
        req_headers["If-Modified-Since"] = modified
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _read_capped(resp, max_bytes, url)
            headers = _extract_headers(resp.headers)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, _extract_headers(e.headers)
        raise SourceError(url, f"HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise SourceError(url, f"connection failed: {e.reason}") from e
    except TimeoutError as e:
        raise SourceError(url, f"timed out after {timeout}s") from e
    return raw, headers


def _extract_headers(h) -> dict:
    return {"etag": h.get("ETag"), "last-modified": h.get("Last-Modified")}


def _read_capped(resp, max_bytes, url) -> bytes:
    """Content-Length is a claim, not a guarantee — a server can lie or a feed
    can be gzip-bombed. Read one byte past the cap and reject on actual bytes
    seen, never on the declared length. The cap exists because this payload
    gets parsed in-process before anything validates it."""
    chunks, total = [], 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise SourceError(url, f"body exceeded {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _local(tag: str) -> str:
    """Strip the {namespace} prefix ElementTree leaves on tag names — Atom is
    namespaced, RSS mostly isn't, and we want one code path for both."""
    return tag.rsplit("}", 1)[-1]


def _children(elem, name):
    return [c for c in elem if _local(c.tag) == name]


def _child(elem, name):
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def _text(elem) -> str:
    """All descendant text, not just the leading node.

    `elem.text` stops at the first child element, which is fine for escaped
    HTML but silently truncates the two cases that carry real XML children:
    Atom `content type="xhtml"`, and feeds that embed unescaped markup. Those
    lose their entire body after the first tag, so use itertext().
    """
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def _clean_body(raw: str) -> str:
    """Strips tags and decodes entities. Does NOT handle malformed/unclosed
    markup gracefully in any semantic sense — it just removes anything that
    looks like a tag and unescapes what's left. Good enough for feed body
    text, not a general HTML-to-text converter."""
    no_tags = _TAG_RE.sub(" ", raw)
    unescaped = html.unescape(no_tags)
    return _WS_RE.sub(" ", unescaped).strip()


def _parse_rfc822(s: str) -> int | None:
    """RSS dates are RFC 822. A date that won't parse yields None, never
    'now' — a fabricated timestamp would silently corrupt dedup/curation
    watermarks that key off published_at."""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_rfc3339(s: str) -> int | None:
    """Atom dates are RFC 3339. Same no-fallback-to-now rule as RSS."""
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse(raw: bytes, source_id: str) -> list[Item]:
    """R36: a feed that fails to parse, or parses but has no recognisable
    feed root, raises SourceError — that is 'broken'. A feed that parses
    cleanly with zero <item>/<entry> elements returns [] — that is legitimate
    and must never look the same as broken."""
    # ElementTree expands internal entities. Verified, not assumed: a 200-byte
    # feed with five nested entity definitions expands to 100KB, and nine
    # levels reaches a gigabyte — a billion-laughs memory bomb from a payload
    # small enough to sail past max_bytes. External entities are worse: a DTD
    # can point at file:///etc/passwd.
    #
    # No legitimate feed needs a DTD, so reject the whole construct rather than
    # trying to police what is inside it. R36 makes this a *reported* broken
    # source, not silence.
    if _DOCTYPE_RE.search(raw[:4096]):
        raise SourceError(source_id, "feed declares a DTD; refusing to parse")

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise SourceError(source_id, f"not parseable XML: {e}") from e

    tag = _local(root.tag)
    fetched_at = int(time.time())
    if tag == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise SourceError(source_id, "RSS root has no <channel>")
        return [_rss_item(el, source_id, fetched_at) for el in _children(channel, "item")]
    if tag == "feed":
        return [_atom_entry(el, source_id, fetched_at) for el in _children(root, "entry")]
    raise SourceError(source_id, f"unrecognised feed root <{tag}>")


def _rss_item(el, source_id, fetched_at) -> Item:
    title = _text(_child(el, "title"))
    link = _text(_child(el, "link"))
    guid = _text(_child(el, "guid"))
    # content:encoded (namespaced) wins over description when both exist —
    # it's typically the full body vs. description's teaser/summary.
    encoded_el = _child(el, "encoded")
    body_el = encoded_el if encoded_el is not None else _child(el, "description")
    body = _clean_body(_text(body_el))
    published_at = _parse_rfc822(_text(_child(el, "pubDate")))
    return Item(
        id=item_id(guid, link, title), source_id=source_id, url=link,
        title=title, body=body, published_at=published_at, fetched_at=fetched_at,
    )


def _atom_entry(el, source_id, fetched_at) -> Item:
    title = _text(_child(el, "title"))
    guid = _text(_child(el, "id"))
    link = _atom_link(el)
    content_el = _child(el, "content")
    body_el = content_el if content_el is not None else _child(el, "summary")
    body = _clean_body(_text(body_el))
    published_at = _parse_rfc3339(_text(_child(el, "published")) or _text(_child(el, "updated")))
    return Item(
        id=item_id(guid, link, title), source_id=source_id, url=link,
        title=title, body=body, published_at=published_at, fetched_at=fetched_at,
    )


def _atom_link(el) -> str:
    """rel="alternate" is the human-facing page; Atom entries can also carry
    rel="self", rel="enclosure", etc. which are not what we want to store as
    the item's url."""
    links = _children(el, "link")
    for l in links:
        if l.get("rel") == "alternate" and l.get("href"):
            return l.get("href")
    for l in links:
        if l.get("href"):
            return l.get("href")
    return ""
