"""Telegram delivery.

R14 hangs on this file: `send` returns True only when Telegram has confirmed
it, because the caller advances "already reported" state on that return value.
Reporting success optimistically loses the finding permanently — marked seen,
never sent, never seen again.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import config

API = "https://api.telegram.org/bot{token}/sendMessage"
CHUNK = 3900                 # Telegram hard-limits a message at 4096
RETRIES = 3

# R33: credentials outside the repo, 0600, referenced by name — never in the
# unit file, the repo, or git history.
ENV_FILE = config.env_file("telegram.env", "CURATED_DIGEST_TELEGRAM_ENV")


class DeliveryError(Exception):
    pass


def _cfg() -> tuple[str, str]:
    vals = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip().strip('"').strip("'")
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or vals.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or vals.get("TELEGRAM_CHAT_ID")
    if config.is_placeholder(token) or config.is_placeholder(chat):
        raise DeliveryError(
            f"TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in {ENV_FILE}")
    return token, chat


def _chunks(text: str):
    """Split on line boundaries so a bullet is never cut in half."""
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > CHUNK and cur:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur:
        out.append(cur)
    return out


def _post(token, chat_id, text, timeout, parse_mode=None) -> None:
    fields = {
        "chat_id": chat_id, "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        fields["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(API.format(token=token), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    if not body.get("ok"):
        raise DeliveryError(f"telegram refused: {json.dumps(body)[:300]}")


def send(artifact, destination=None, *, timeout=20, sleep=time.sleep) -> bool:
    """True only once every chunk is confirmed sent.

    A partial send returns False rather than True-with-a-warning, so the caller
    does not advance state on a digest the reader only half received.
    """
    token, default_chat = _cfg()
    chat_id = destination or default_chat
    # The renderer escapes its own text and emits one bullet per line, so no
    # tag ever straddles a chunk boundary. Malformed HTML is a 400, which is
    # not retried — and R14 then leaves the findings undelivered rather than
    # marking them reported.
    parse_mode = "HTML" if artifact.kind == "html" else None

    for chunk in _chunks(artifact.body):
        for attempt in range(RETRIES):
            try:
                _post(token, chat_id, chunk, timeout, parse_mode)
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                # 429 carries retry_after; anything 4xx else is our bug and
                # retrying it just burns time.
                if e.code == 429 and attempt < RETRIES - 1:
                    sleep(min(60, 2 ** attempt * 5))
                    continue
                if 400 <= e.code < 500:
                    raise DeliveryError(f"telegram {e.code}: {detail}") from e
                if attempt == RETRIES - 1:
                    raise DeliveryError(f"telegram {e.code}: {detail}") from e
                sleep(2 ** attempt)
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == RETRIES - 1:
                    raise DeliveryError(f"telegram unreachable: {e}") from e
                sleep(2 ** attempt)
        else:
            return False
    return True
