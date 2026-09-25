"""Replaceable text/image transports; Claude remains the default backend.

The JSON parsing, validation and retry policy stays in generate. Providers return
text, never execute renderer instructions. Applications can register a transport
before calling Sofit's library API; CLI users can select the built-in backends.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ModelTransport(Protocol):
    def __call__(
        self, system: str, user: str, model: str, images: list[Path] | None = None
    ) -> str: ...


@dataclass(frozen=True)
class ModelBackend:
    transport: ModelTransport
    cache_key: str


_registered: dict[str, ModelBackend] = {}


def register_backend(name: str, transport: ModelTransport, *, cache_key: str):
    """Explicit process-local extension point. Never load providers from a clip spec."""
    if not name or name in {"api", "claude-cli"} or not cache_key:
        raise ValueError("custom backend needs a distinct name and versioned cache key")
    _registered[name] = ModelBackend(transport, cache_key)


def backend(name: str) -> ModelBackend:
    # Lazy references preserve existing library monkeypatches and avoid cycles.
    from . import generate

    if name == "api":
        return ModelBackend(generate._call_api, "claude-frames-v1:api")
    if name == "claude-cli":
        return ModelBackend(generate._call_claude_cli, "claude-frames-v1:claude-cli")
    if name in _registered:
        return _registered[name]
    raise ValueError(f"unknown model backend: {name}")
