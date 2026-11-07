"""R5a: the model provider is an abstraction.

A task names a model as `provider:model`. The runtime resolves that through an
adapter whose entire contract is: take a prompt, return text. Adding a provider
means adding one adapter — tasks, curation, rendering and delivery do not
change, and no provider-specific concept leaks past this boundary (R5b).
"""
from .base import Completion, Provider, ProviderError, RateLimited

_ADAPTERS = {}


def register(name, factory):
    _ADAPTERS[name] = factory


def resolve(spec: str) -> tuple[Provider, str]:
    """`claude:opus-5` -> (adapter, "opus-5").

    R8 makes this mandatory per task and refuses a default, because a default is
    exactly where cost accumulates without anyone deciding to spend it.
    """
    if ":" not in spec:
        raise ProviderError(
            f"model must be 'provider:model', got {spec!r} — there is no default")
    provider, _, model = spec.partition(":")
    if provider not in _ADAPTERS:
        raise ProviderError(f"unknown provider {provider!r}; "
                            f"have {sorted(_ADAPTERS)}")
    return _ADAPTERS[provider](), model


def _bootstrap():
    from . import claude, gemini          # noqa: F401  (registration side effect)


_bootstrap()

__all__ = ["Completion", "Provider", "ProviderError", "RateLimited",
           "register", "resolve"]
