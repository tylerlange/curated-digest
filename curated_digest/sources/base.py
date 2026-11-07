"""R1: the normalized item shape. Everything downstream — dedup, curation,
rendering — consumes only this, never a feed-specific structure (R2)."""
import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    id: str                    # stable identity (see item_id below)
    source_id: str             # which configured source produced it
    url: str
    title: str
    body: str                  # plain text: tags stripped, entities decoded, whitespace collapsed
    published_at: int | None   # unix epoch UTC, None if the feed gives nothing usable
    fetched_at: int


class SourceError(Exception):
    """A source that is broken (fetch failure, unparseable payload) must never
    be indistinguishable from a source that is legitimately empty (R36)."""

    def __init__(self, source_id, reason):
        self.source_id = source_id
        self.reason = reason
        super().__init__(f"{source_id}: {reason}")


def item_id(guid: str | None, link: str, title: str) -> str:
    """Identity is the feed's own GUID when it bothers to provide one — stable
    across re-fetches even if title/link get edited. Falls back to a hash of
    link+title, which is not update-stable but is the best available signal
    when the feed gives us nothing else (D5: dedup depends on this)."""
    if guid:
        return guid
    return hashlib.sha256((link + title).encode()).hexdigest()
