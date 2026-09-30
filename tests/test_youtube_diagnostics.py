import subprocess
from types import SimpleNamespace

import pytest

from sofit import footage_youtube as yt
from sofit.footage import FootageError


@pytest.mark.parametrize(
    "detail,expected",
    [
        (
            b"initial startup\nWARNING: no supported JavaScript runtime\nERROR: Video unavailable\n",
            "Video unavailable",
        ),
        (b"ERROR: Sign in to confirm your age\n", "Sign in to confirm your age"),
        (b"", "may be unavailable or blocked"),
    ],
)
def test_failed_extraction_includes_bounded_useful_diagnostics(
    monkeypatch, detail, expected
):
    monkeypatch.setattr(yt, "available", lambda: True)

    def run(cmd, **kwargs):
        kwargs["stderr"].write(b"old noise\n" * 10000 + detail)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(FootageError) as error:
        yt._extract("https://www.youtube.com/watch?v=abcdefghijk")
    message = str(error.value)
    assert expected in message
    assert "Deno or Node 22+" in message
    assert len(message) < 1300
    if detail:
        assert "old noise" not in message or detail.count(b"\n") == 1


def test_diagnostics_do_not_expose_urls_credentials_or_terminal_controls(monkeypatch):
    monkeypatch.setattr(yt, "available", lambda: True)

    def run(cmd, **kwargs):
        kwargs["stderr"].write(
            b"Authorization: Bearer private-auth\nCookie: session=private-cookie\n"
            b"\x1b[31mERROR: blocked https://media.example/video?sig=private-url\x1b[0m\n"
            b"ERROR: token=private-token password=private-password api_key=private-key\x00\n"
        )
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(FootageError) as error:
        yt._extract("https://www.youtube.com/watch?v=abcdefghijk")
    message = str(error.value)
    assert "blocked" in message
    assert all(
        word not in message for word in ["private-", "\x1b", "\x00", "media.example"]
    )
