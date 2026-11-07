"""The provider contract. Text in, text out — nothing else."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    cost_usd: float = 0.0


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    """Distinct from a failure. The caller yields and retries later without
    consuming a retry — the same rule the harness applies to its own units."""

    def __init__(self, message, kind="five_hour", resume_after=None):
        super().__init__(message)
        self.kind = kind
        self.resume_after = resume_after


class Provider(Protocol):
    name: str

    def complete(self, prompt: str, model: str, *, system: str | None = None,
                 max_tokens: int = 2048, timeout: int = 180) -> Completion:
        """R20: no tools, no filesystem, no network available to the model.

        This is the structural mitigation for prompt injection. Fetched content
        reaches a model that has nothing to act on, so an embedded instruction
        is inert rather than merely discouraged. Prompt-level defences are
        assumed fallible; this is what actually holds.
        """
        ...
