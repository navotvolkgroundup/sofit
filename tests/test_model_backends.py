import pytest

from sofit import generate, model_backends as models


def test_default_transports_and_versioned_cache_identity(monkeypatch):
    monkeypatch.delenv("SOFIT_TITLER_MODEL", raising=False)
    assert models.backend("api").transport is generate._call_api
    assert models.backend("claude-cli").transport is generate._call_claude_cli
    assert models.backend("api").cache_key == "claude-frames-v1:api"
    assert models.backend("claude-cli").cache_key == "claude-frames-v1:claude-cli"


def test_registered_transport_receives_text_images_and_model(monkeypatch, tmp_path):
    calls = []

    def transport(system, user, model, images=None):
        calls.append((system, user, model, images))
        return '{"ok": true}'

    monkeypatch.setattr(models, "_registered", {})
    models.register_backend("example", transport, cache_key="example-v2")
    image = tmp_path / "frame.jpg"
    for images in (None, [image]):
        assert generate.call_claude_json(
            "system",
            "user",
            lambda x: x,
            model="vision-v1",
            titler="example",
            images=images,
        ) == {"ok": True}
        assert calls[-1] == ("system", "user", "vision-v1", images)
    assert models.backend("example").cache_key == "example-v2"
    with pytest.raises(ValueError, match="unknown model backend"):
        models.backend("typo")


@pytest.mark.parametrize("name", ["", "api", "claude-cli"])
def test_builtin_names_cannot_be_overridden(name):
    with pytest.raises(ValueError):
        models.register_backend(name, lambda *a: "{}", cache_key="v1")


def test_invalid_json_and_validation_retry_but_transport_failure_does_not(monkeypatch):
    calls = []
    answers = iter(["not JSON", '{"ok": true}'])

    def transport(*args):
        calls.append(args)
        return next(answers)

    monkeypatch.setattr(generate, "_call_api", transport)
    assert generate.call_claude_json("s", "u", lambda x: x) == {"ok": True}
    assert len(calls) == 2
    calls.clear()

    def failed(*args):
        calls.append(args)
        raise generate.GenerationError("service unavailable")

    monkeypatch.setattr(generate, "_call_api", failed)
    with pytest.raises(generate.GenerationError, match="service unavailable"):
        generate.call_claude_json("s", "u", lambda x: x)
    assert len(calls) == 1
    calls.clear()

    def invalid(*args):
        calls.append(args)
        return "{}"

    def reject(obj):
        raise generate.GenerationError("missing field")

    monkeypatch.setattr(generate, "_call_api", invalid)
    with pytest.raises(generate.GenerationError, match="after retry.*missing field"):
        generate.call_claude_json("s", "u", reject)
    assert len(calls) == 2


def test_model_precedence_and_custom_default(monkeypatch):
    calls = []

    def transport(system, user, model, images=None):
        calls.append(model)
        return "{}"

    monkeypatch.setattr(models, "_registered", {})
    models.register_backend("custom", transport, cache_key="custom-v1")
    monkeypatch.setattr(generate, "_call_api", transport)
    monkeypatch.setenv("SOFIT_TITLER_MODEL", "environment")
    generate.call_claude_json("s", "u", lambda x: x, model="explicit")
    generate.call_claude_json("s", "u", lambda x: x)
    monkeypatch.delenv("SOFIT_TITLER_MODEL")
    generate.call_claude_json("s", "u", lambda x: x)
    generate.call_claude_json("s", "u", lambda x: x, titler="custom")
    assert calls == ["explicit", "environment", generate.CLAUDE_MODEL, ""]


def test_api_images_and_text_keep_distinct_payloads(monkeypatch, tmp_path):
    from types import SimpleNamespace

    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpeg")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")])

    monkeypatch.setattr(
        generate,
        "_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    monkeypatch.setenv("SOFIT_VISUAL_TIMEOUT", "17")
    generate._call_api("s", "u", "m", images=[image])
    generate._call_api("s", "u", "m")
    assert calls[0]["timeout"] == 17
    assert calls[0]["messages"][0]["content"][1]["source"]["data"] == "anBlZw=="
    assert calls[1]["messages"] == [{"role": "user", "content": "u"}]
    assert "timeout" not in calls[1]


def test_image_cli_timeout_respects_both_caps(monkeypatch, tmp_path):
    import shutil
    import subprocess

    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.setattr(shutil, "which", lambda _: "/bin/claude")
    monkeypatch.setattr(generate, "CLI_TIMEOUT", 10)
    monkeypatch.setenv("SOFIT_VISUAL_TIMEOUT", "17")

    def run(*args, **kwargs):
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired("claude", 10)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(TimeoutError, match="Raise SOFIT_CLI_TIMEOUT"):
        generate._call_claude_cli("s", "u", "m", images=[image])
