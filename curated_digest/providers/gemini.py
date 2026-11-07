"""Gemini via the REST API. A plain HTTPS call has no tools by construction,
which is why fetched feed content is judged here by preference: an injected
instruction reaches a model with nothing to act on and no quota to spend."""
import json
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config

from . import register
from .base import Completion, ProviderError, RateLimited

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent")

# R33: credentials live outside the repo, 0600, referenced by name. This key is
# scoped to curated-digest and is deliberately NOT the key the chat stack uses.
KEY_FILE = config.env_file("gemini.env", "CURATED_DIGEST_GEMINI_KEY_FILE")

# R43/R44: the spend ceiling needs a number to compare against, and Gemini
# reports tokens rather than dollars. USD per million tokens (input, output).
# These drift — they are a ceiling input, not an invoice. An unknown model
# falls back to the most expensive entry so a pricing change cannot make the
# ceiling silently permissive.
PRICING = {
    "gemini-3.7-flash": (0.15, 0.60),
    "gemini-3.6-flash": (0.30, 1.20),
    "gemini-3.5-flash": (0.30, 1.20),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
}


def estimate_cost(model: str, usage: dict) -> float:
    rate_in, rate_out = max(PRICING.values(), key=lambda p: p[1])
    for name, rates in PRICING.items():
        if model.startswith(name):
            rate_in, rate_out = rates
            break
    tin = usage.get("promptTokenCount", 0) or 0
    tout = ((usage.get("candidatesTokenCount", 0) or 0)
            + (usage.get("thoughtsTokenCount", 0) or 0))
    return (tin * rate_in + tout * rate_out) / 1_000_000


def _api_key() -> str:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    try:
        for line in KEY_FILE.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "GEMINI_API_KEY":
                v = v.strip().strip('"').strip("'")
                if config.is_placeholder(v):
                    raise ProviderError(
                        f"GEMINI_API_KEY in {KEY_FILE} is still the placeholder; "
                        "put your key in that file (mode 0600)")
                return v
    except OSError:
        pass
    raise ProviderError(f"no GEMINI_API_KEY in env or {KEY_FILE}; "
                        "run `bin/curated-digest init` and fill it in")


# Measured, not guessed: gemini-3.7-flash returns 503 on roughly half of all
# requests while 3.6 and 3.5 return none, and a failed call succeeds on
# immediate retry. The 50% discount is doing what a discount does to demand.
#
# So a 503 is a coin flip that clears in a second, not a wall. Yielding the
# unit to the harness on the first one turned a one-second retry into a
# ten-minute delay, and on a task with several curation calls made a successful
# run nearly impossible. Retry in-process first; yield only if it persists,
# which is what a real capacity wall looks like.
INLINE_RETRIES = 4
RETRYABLE = (500, 502, 503, 504)


class GeminiProvider:
    name = "gemini"

    def _call(self, req, timeout):
        for attempt in range(INLINE_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code not in RETRYABLE or attempt == INLINE_RETRIES - 1:
                    raise
                # Jittered, so several tasks retrying at once do not sync up
                # and hammer the same overloaded shard together.
                time.sleep((2 ** attempt) * 0.5 + random.random() * 0.3)
        raise AssertionError("unreachable")

    def complete(self, prompt, model, *, system=None, max_tokens=2048, timeout=180):
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        req = urllib.request.Request(
            ENDPOINT.format(model=model),
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": _api_key()},
        )
        try:
            out = self._call(req, timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            # Same contract as the Claude adapter: a capacity wall is a yield,
            # not a failure, so it never burns a retry.
            #
            # 503 belongs here as much as 429 does. Observed live on
            # gemini-3.7-flash: "This model is currently experiencing high
            # demand." That is a temporary shortage of someone else's
            # capacity, not a defect in the task, and treating it as a failure
            # would spend all three retries and mark a perfectly healthy task
            # stuck. H14 still catches it if it never clears — 24h of yielding
            # becomes `stuck` and asks for a human.
            if e.code in (429, 500, 502, 503, 504):
                wait = 3600 if e.code == 429 else 600
                raise RateLimited(f"gemini {e.code}: {detail}", "provider_quota",
                                  int(time.time() + wait)) from e
            raise ProviderError(f"gemini {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ProviderError(f"gemini unreachable: {e.reason}") from e

        try:
            parts = out["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError) as e:
            # A blocked or empty candidate is a real outcome, not a crash.
            raise ProviderError(
                f"gemini returned no usable candidate: {json.dumps(out)[:300]}") from e
        return Completion(text=text, model=model,
                          cost_usd=estimate_cost(model, out.get("usageMetadata", {})))


register("gemini", GeminiProvider)
