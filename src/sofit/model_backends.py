"""Replaceable text/image transports; Claude remains the default backend.

The JSON parsing, validation and retry policy stays in generate. Providers return
text, never execute renderer instructions. Applications can register a transport
before calling Sofit's library API; CLI users can select the built-in backends.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
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
    if not name or name in {"api", "claude-cli", "codex-cli"} or not cache_key:
        raise ValueError("custom backend needs a distinct name and versioned cache key")
    _registered[name] = ModelBackend(transport, cache_key)


def backend(name: str) -> ModelBackend:
    # Lazy references preserve existing library monkeypatches and avoid cycles.
    from . import generate

    if name == "api":
        return ModelBackend(generate._call_api, "claude-frames-v1:api")
    if name == "claude-cli":
        return ModelBackend(generate._call_claude_cli, "claude-frames-v1:claude-cli")
    if name == "codex-cli":
        return ModelBackend(_call_codex_cli, "codex-frames-v1:codex-cli")
    if name in _registered:
        return _registered[name]
    raise ValueError(f"unknown model backend: {name}")


def _call_codex_cli(system: str, user: str, model: str, images=None) -> str:
    """Native images through an authenticated Codex CLI, isolated from project tools."""
    from .generate import GenerationError, CLI_TIMEOUT

    executable = shutil.which("codex")
    if not executable:
        raise GenerationError("codex CLI not found; install and authenticate Codex")
    timeout = (
        min(CLI_TIMEOUT, float(os.environ.get("SOFIT_VISUAL_TIMEOUT", "180")))
        if images
        else CLI_TIMEOUT
    )
    with tempfile.TemporaryDirectory(prefix="sofit-model-") as directory:
        output = Path(directory) / "result.json"
        command = [
            executable,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--disable",
            "shell_tool",
            "--disable",
            "multi_agent",
            "--disable",
            "hooks",
            "--enable",
            "skip_host_skill_discovery",
            "-c",
            'web_search="disabled"',
            "-c",
            "mcp_servers={}",
            "--cd",
            directory,
            "--output-last-message",
            str(output),
        ]
        if model:
            command += ["--model", model]
        for image in images or []:
            command += ["--image", str(Path(image).resolve())]
        command += ["-"]
        prompt = (
            system + "\n\nDo not use tools. Return only the requested JSON.\n\n" + user
        )
        try:
            # Keep verbose CLI diagnostics out of memory and progress logs.
            with tempfile.TemporaryFile() as log:
                result = subprocess.run(
                    command,
                    input=prompt.encode(),
                    stdout=log,
                    stderr=log,
                    timeout=timeout,
                )
                if result.returncode:
                    log.seek(max(0, log.tell() - 8192))
                    detail = log.read().decode(errors="replace").lower()
                    category = (
                        "quota/authentication/rate limit"
                        if any(
                            word in detail
                            for word in (
                                "quota",
                                "usage limit",
                                "rate limit",
                                "not logged in",
                                "401",
                                "429",
                            )
                        )
                        else "execution failed (check CLI version and login)"
                    )
                    raise GenerationError("codex CLI " + category)
            if not output.is_file() or output.stat().st_size > 1024 * 1024:
                raise GenerationError("codex CLI returned missing/oversized output")
            return output.read_text(encoding="utf-8").strip()
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"codex CLI exceeded {timeout:g}s") from None
