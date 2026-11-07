"""The editorial voice, loaded from one file so there is exactly one place to
change how curated-digest sounds.

Lookup order: CURATED_DIGEST_EDITORIAL_FILE, else voice.md in the config home
(the operator's own voice, written by `curated-digest init`), else the default
prompts/voice.md in this repo. Pointing the variable at a file another tool
already uses keeps one copy. Two copies would drift, and the copy that
drifts is always the one nobody is reading.
"""
import os
from pathlib import Path

from . import config

DEFAULT_VOICE = Path(__file__).resolve().parent.parent / "prompts" / "voice.md"


def _voice_file() -> Path:
    if os.environ.get("CURATED_DIGEST_EDITORIAL_FILE"):
        return Path(os.environ["CURATED_DIGEST_EDITORIAL_FILE"]).expanduser()
    own = config.config_home() / "voice.md"
    return own if own.exists() else DEFAULT_VOICE


VOICE_FILE = _voice_file()

# Enough to be safe if the file is missing. Deliberately not a full copy of it:
# a fallback that is nearly as good as the real thing is a fallback nobody
# notices has kicked in.
FALLBACK = """Write for a busy technical reader who does not know product jargon.
Never name a feature without saying what it does for them in the same sentence.
Lead with the consequence. One self-contained line per item. Cut anything the
reader can do nothing about. Silence is a correct outcome."""


def voice() -> str:
    """The agent body, with YAML frontmatter stripped — the frontmatter is
    routing metadata for Claude Code, not instructions to a filter."""
    try:
        text = VOICE_FILE.read_text()
    except OSError:
        return FALLBACK
    if text.startswith("---"):
        _, _, rest = text.partition("---")
        _, _, body = rest.partition("---")
        text = body
    return text.strip() or FALLBACK
