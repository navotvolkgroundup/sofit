"""Checks for the Graph-API Facebook uploader: body assembly and the schedule window.

Both are pure logic and neither touches the network, so the suite stays hermetic.
The multipart body is hand-rolled (sofit ships no HTTP dependency), which is
exactly the kind of thing that breaks silently - Graph would reject a malformed
body with an opaque error rather than pointing at the boundary.
"""
import importlib.util
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "publish" / "upload_facebook_api.py"
_spec = importlib.util.spec_from_file_location("upload_facebook_api", _SRC)
fb = importlib.util.module_from_spec(_spec)
sys.modules["upload_facebook_api"] = fb
_spec.loader.exec_module(fb)


def test_multipart_body_has_every_field_and_the_file():
    status_url = "https://example.invalid/x"
    captured = {}

    def fake_urlopen(req, timeout=None):        # noqa: ARG001
        captured["ctype"] = req.headers["Content-type"]
        captured["body"] = req.data
        raise RuntimeError("stop before the network")

    fb.urllib.request.urlopen = fake_urlopen
    with pytest.raises(RuntimeError):
        fb._post_multipart(status_url, {"description": "שלום", "published": "false"},
                           "clip.mp4", b"\x00\x01BINARY\xff")

    boundary = captured["ctype"].split("boundary=")[1]
    body = captured["body"]
    assert body.count(f"--{boundary}\r\n".encode()) == 3      # 2 fields + the file
    assert body.endswith(f"--{boundary}--\r\n".encode())      # closing delimiter
    assert 'name="description"' .encode() in body
    assert "שלום".encode() in body                            # utf-8, not mangled
    assert b'filename="clip.mp4"' in body
    assert b"Content-Type: video/mp4" in body
    assert b"\x00\x01BINARY\xff" in body                      # bytes survive intact


def test_schedule_window_is_enforced_before_the_call():
    # Meta rejects anything under 10 minutes or over 6 months; catching it here
    # keeps the failure readable instead of an opaque Graph error.
    past = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    with pytest.raises(SystemExit) as e:
        fb._scheduled_epoch(past, "13:00")
    assert "too_soon" in str(e.value)

    far = time.strftime("%Y-%m-%d", time.localtime(time.time() + 300 * 86400))
    with pytest.raises(SystemExit) as e:
        fb._scheduled_epoch(far, "13:00")
    assert "too_far" in str(e.value)

    ok = time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 86400))
    assert fb._scheduled_epoch(ok, "13:00") > int(time.time())
