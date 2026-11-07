"""Reddit via OAuth, because unauthenticated .rss is not usable for a digest.

Measured: reddit.com/r/<sub>/.rss returns 200 for the first request and 429 for
the next several, across www, old, and the /new variant. A digest spanning
three subreddits cannot be built on that. App-only OAuth lifts the limit to
something a scheduled task can live inside.

R2 in practice: this file exists, task.py learns one new `type`, dispatch gains
a registry entry, and curation/rendering/delivery are untouched — they still
only ever see the normalized Item.
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import config
from .base import Item, SourceError

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com/r/{sub}/{sort}"
ENV_FILE = config.env_file("reddit.env", "CURATED_DIGEST_REDDIT_ENV")
SUB_RE = re.compile(r"/r/([A-Za-z0-9_]+)")
SORTS = ("new", "hot", "top", "rising")
WINDOWS = ("hour", "day", "week", "month", "year", "all")


def _env() -> dict:
    vals = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.strip().startswith("#"):
                continue
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return vals


def target(url: str) -> tuple[str, str, str | None]:
    """(subreddit, sort, window) from a normal Reddit URL.

    Sort is not a detail. `/new` is what a subreddit looks like before anyone
    has judged it, and on a busy sub the ten newest posts are almost entirely
    questions, case requests and availability chatter — measured, not assumed.
    A tips digest wants `/top` over a window, where the community has already
    done the first pass. Read it off the URL so a task file says what it means:

        https://www.reddit.com/r/steammachine/top/?t=week
    """
    m = SUB_RE.search(url)
    if not m:
        raise SourceError(url, f"cannot find a subreddit name in {url!r}")
    sub = m.group(1)

    path = url.split(f"/r/{sub}", 1)[-1].split("?")[0].strip("/")
    sort = path if path in SORTS else "hot"

    window = None
    if sort == "top":
        qm = re.search(r"[?&]t=([a-z]+)", url)
        window = qm.group(1) if qm and qm.group(1) in WINDOWS else "week"
    return sub, sort, window


def _token() -> tuple[str, str]:
    """Cached on disk with its expiry. Reddit issues hour-long tokens and
    getting a fresh one per subreddit would spend the rate limit this module
    exists to avoid."""
    env = _env()
    cid, secret = env.get("REDDIT_CLIENT_ID"), env.get("REDDIT_CLIENT_SECRET")
    ua = env.get("REDDIT_USER_AGENT") or "linux:curated-digest:1.0"
    if config.is_placeholder(cid) or config.is_placeholder(secret):
        raise SourceError("reddit", f"REDDIT_CLIENT_ID/SECRET not set in {ENV_FILE}")

    cache = config.state_dir() / "reddit-token.json"
    try:
        blob = json.loads(cache.read_text())
        if blob["expires_at"] > time.time() + 60:
            return blob["token"], ua
    except Exception:
        pass

    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"Authorization": f"Basic {basic}", "User-Agent": ua},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            blob = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SourceError("reddit", f"token request failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise SourceError("reddit", f"token request unreachable: {e.reason}") from e

    token = blob.get("access_token")
    if not token:
        raise SourceError("reddit", "token response carried no access_token")
    cache.write_text(json.dumps(
        {"token": token, "expires_at": time.time() + int(blob.get("expires_in", 3600))}))
    cache.chmod(0o600)
    return token, ua


def fetch(url, *, timeout=20, limit=50, **_ignored):
    """Signature matches rss.fetch so the dispatcher does not special-case it.

    Conditional GET does not apply — Reddit's listing endpoint carries no
    usable etag — so this always returns a body and the store's dedup does the
    work instead.
    """
    sub, sort, window = target(url)
    token, ua = _token()
    params = {"limit": limit}
    if window:
        params["t"] = window
    req = urllib.request.Request(
        API.format(sub=sub, sort=sort) + "?" + urllib.parse.urlencode(params),
        headers={"Authorization": f"Bearer {token}", "User-Agent": ua},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), {}
    except urllib.error.HTTPError as e:
        # 401 usually means a cached token outlived its welcome. Drop it so the
        # next run mints a fresh one rather than failing identically forever.
        if e.code == 401:
            (config.state_dir() / "reddit-token.json").unlink(missing_ok=True)
        raise SourceError(url, f"HTTP {e.code} {e.reason}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise SourceError(url, f"connection failed: {e}") from e


def parse(raw: bytes, source_id: str) -> list[Item]:
    try:
        blob = json.loads(raw)
        children = blob["data"]["children"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # R36: unparseable is broken, and broken must not look like empty.
        raise SourceError(source_id, f"not a Reddit listing: {e}") from e

    items = []
    for child in children:
        d = child.get("data") or {}
        if d.get("stickied"):
            continue            # pinned mod posts are never the tip we want
        # `name` is Reddit's fullname (t3_abc123) and never changes, so unlike
        # the RSS fallback hash an edited title cannot resurrect a post.
        iid = d.get("name") or d.get("id")
        if not iid:
            continue
        body = (d.get("selftext") or "").strip()
        if d.get("link_flair_text"):
            body = f"[{d['link_flair_text']}] {body}"
        items.append(Item(
            id=iid,
            source_id=source_id,
            url=f"https://www.reddit.com{d.get('permalink', '')}",
            title=(d.get("title") or "").strip(),
            body=body[:4000],
            published_at=int(d["created_utc"]) if d.get("created_utc") else None,
            fetched_at=int(time.time()),
        ))
    return items


COMMENTS_API = "https://oauth.reddit.com/r/{sub}/comments/{post_id}"
ENRICH_GAP_SECONDS = 1.0
TOP_COMMENTS = 6
THIN_BODY_CHARS = 200


def enrich(items, *, timeout=20):
    """R19a: gather the context a judgment needs, deterministically, and hand
    it to curation as text.

    Reddit's listing endpoint returns `selftext`, which is empty for link and
    image posts — so the two most useful posts in a week of r/steammachine
    reached the filter as a bare title with 26 characters of flair behind them
    and were correctly cut for having no stated tip. The tip was in the
    comments the whole time.

    Only runs on candidates that survived the prefilter, so this is ~10 calls
    per digest rather than one per fetched post (R22). Failure is never fatal:
    an un-enriched item is judged on what it already had.
    """
    token, ua = _token()
    out = []
    for i, item in enumerate(items):
        if len(item.body) >= THIN_BODY_CHARS:
            out.append(item)
            continue
        if i:
            time.sleep(ENRICH_GAP_SECONDS)
        try:
            comments = _top_comments(item, token, ua, timeout)
        except Exception:
            out.append(item)
            continue
        if not comments:
            out.append(item)
            continue
        body = (item.body + "\n\nTop comments:\n"
                + "\n".join(f"- {c}" for c in comments))
        out.append(Item(
            id=item.id, source_id=item.source_id, url=item.url,
            title=item.title, body=body[:6000],
            published_at=item.published_at, fetched_at=item.fetched_at))
    return out


def _top_comments(item, token, ua, timeout) -> list[str]:
    m = re.search(r"/comments/([a-z0-9]+)", item.url)
    sub_m = SUB_RE.search(item.url)
    if not m or not sub_m:
        return []
    url = (COMMENTS_API.format(sub=sub_m.group(1), post_id=m.group(1))
           + "?" + urllib.parse.urlencode(
               {"limit": TOP_COMMENTS, "depth": 1, "sort": "top"}))
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        blob = json.loads(r.read())

    # The comments endpoint returns [post_listing, comment_listing].
    if not isinstance(blob, list) or len(blob) < 2:
        return []
    out = []
    for child in (blob[1].get("data", {}).get("children") or []):
        d = child.get("data") or {}
        text = (d.get("body") or "").strip()
        # AutoModerator posts rules, never tips.
        if not text or d.get("author") == "AutoModerator" or d.get("stickied"):
            continue
        out.append(" ".join(text.split())[:600])
        if len(out) >= TOP_COMMENTS:
            break
    return out
