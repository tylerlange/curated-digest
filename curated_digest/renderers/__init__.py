"""R3: renderers turn curated items into an artifact. A renderer must not
fetch, judge, or deliver — that separation is what lets the Telegram digest and
a styled print edition be two renderers over the same content rather than two
projects."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Artifact:
    body: str
    kind: str = "text"          # text | html | pdf
    title: str | None = None
    # The item ids this artifact actually put in front of the reader, in the
    # order it presented them. The caller advances "already reported" state on
    # exactly these — a renderer that caps its tiers has dropped findings the
    # curator kept, and marking those reported loses them for good (R14).
    rendered_ids: tuple[str, ...] = ()
