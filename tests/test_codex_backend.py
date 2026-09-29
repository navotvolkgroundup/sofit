import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from sofit import generate, model_backends as models


def test_codex_native_images_and_isolated_command(monkeypatch, tmp_path):
    image = tmp_path / "frame.jpg"
    observed = []
    monkeypatch.setattr(models.shutil, "which", lambda name: "/bin/codex")

    def run(command, **kwargs):
        observed.append(command)
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in command and "--ephemeral" in command
        assert command[command.index("--image") + 1] == str(image.resolve())
        assert command[command.index("--model") + 1] == "model-v1"
        assert b"Do not use tools" in kwargs["input"]
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"frames": []}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(models.subprocess, "run", run)
    result = models._call_codex_cli("judge", "intent", "model-v1", [image])
    assert json.loads(result) == {"frames": []}
    assert not Path(observed[0][observed[0].index("--cd") + 1]).exists()


def test_codex_quota_is_reported_without_leaking_diagnostics(monkeypatch):
    monkeypatch.setattr(models.shutil, "which", lambda name: "/bin/codex")

    def run(command, **kwargs):
        kwargs["stderr"].write(b"usage limit reached private diagnostic")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(models.subprocess, "run", run)
    with pytest.raises(generate.GenerationError, match="quota/authentication") as error:
        models._call_codex_cli("judge", "intent", "")
    assert "private diagnostic" not in str(error.value)


@pytest.mark.parametrize("images", [False, True])
def test_codex_timeout_is_bounded(monkeypatch, tmp_path, images):
    monkeypatch.setattr(models.shutil, "which", lambda _: "/bin/codex")
    monkeypatch.setattr(generate, "CLI_TIMEOUT", 40)
    monkeypatch.setenv("SOFIT_VISUAL_TIMEOUT", "15")

    def run(*args, **kwargs):
        assert kwargs["timeout"] == (15 if images else 40)
        raise models.subprocess.TimeoutExpired("codex", kwargs["timeout"])

    monkeypatch.setattr(models.subprocess, "run", run)
    with pytest.raises(TimeoutError, match="codex CLI exceeded"):
        models._call_codex_cli(
            "s", "u", "", [tmp_path / "frame.jpg"] if images else None
        )


def test_codex_missing_binary(monkeypatch):
    monkeypatch.setattr(models.shutil, "which", lambda _: None)
    with pytest.raises(generate.GenerationError, match="install and authenticate"):
        models._call_codex_cli("s", "u", "")


@pytest.mark.parametrize("output", [None, "x" * (1024 * 1024 + 1)])
def test_codex_rejects_missing_or_oversized_output(monkeypatch, output):
    monkeypatch.setattr(models.shutil, "which", lambda _: "/bin/codex")

    def run(command, **kwargs):
        if output is not None:
            Path(command[command.index("--output-last-message") + 1]).write_text(output)
        assert "--image" not in command and "--model" not in command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(models.subprocess, "run", run)
    with pytest.raises(generate.GenerationError, match="missing/oversized"):
        models._call_codex_cli("s", "u", "")


def test_codex_is_registered_separately_from_claude():
    assert models.backend("codex-cli").transport is models._call_codex_cli
    assert (
        models.backend("codex-cli").cache_key != models.backend("claude-cli").cache_key
    )
    with pytest.raises(ValueError):
        models.register_backend("codex-cli", lambda *a: "{}", cache_key="override")
