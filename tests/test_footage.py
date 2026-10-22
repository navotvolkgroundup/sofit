"""Hermetic provider, rights, network-boundary and cache tests."""

import io
import json
import socket
import urllib.error
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from sofit import footage as f
from sofit import footage_commons as commons


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*a, **k):
        raise AssertionError("unit tests must not use the network")
    monkeypatch.setattr(f, "open_url", denied)


@pytest.fixture
def candidate():
    return f.Candidate("commons", "https://commons.wikimedia.org/wiki/File:Robot.webm",
                       "https://upload.wikimedia.org/robot.webm", "Unitree robot running",
                       90, 1280, 720, creator="Camera operator", license="CC BY 4.0",
                       license_url="https://creativecommons.org/licenses/by/4.0/",
                       attribution="Camera operator, CC BY 4.0", attribution_required=True)


def test_commons_normalization_and_transcode(monkeypatch):
    info = {"mime": "video/webm", "url": "https://upload.wikimedia.org/robot.webm?utm_content=original",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Robot.webm",
            "duration": 90, "width": 3840, "height": 2160, "size": 3_000_000_000,
            "extmetadata": {k: {"value": v} for k, v in {
                "Artist": '<a href="/user">Alice &amp; Bob</a>', "LicenseShortName": "CC BY 4.0",
                "LicenseUrl": "https://creativecommons.org/licenses/by/4.0/",
                "AttributionRequired": "true", "ImageDescription": "A <b>running robot</b>",
                "Categories": "Robots|Running", "UsageTerms": "Attribution"}.items()},
            "derivatives": [{"transcodekey": "720p.webm", "src": "https://upload.wikimedia.org/720.webm",
                             "type": "video/webm", "width": 1280, "height": 720}],
            "timedtext": [{"src": "https://commons.wikimedia.org/w/api.php?action=timedtext", "srclang": "en"}]}
    page = {"title": "File:Robot.webm", "videoinfo": [info]}
    c = commons.normalize_candidate(page)
    assert c.creator == "Alice & Bob"
    assert c.description == "A running robot"
    assert c.tags == ("Robots", "Running")
    assert c.width == 1280 and c.size is None
    assert c.original_media_url.endswith("/robot.webm")
    assert c.attribution_required is True and c.subtitle_urls
    calls = []
    def fetch(url):
        calls.append(url)
        return json.dumps({"query": {"pages": [page, {"missing": True}]}}).encode()
    monkeypatch.setattr(commons, "fetch_bytes", fetch)
    assert commons.CommonsProvider().search("Unitree robot") == [c]
    assert "filetype%3Avideo" in calls[0] and "gsrnamespace=6" in calls[0]
    assert "viextmetadatalanguage=en" in calls[0]
    assert commons.normalize_candidate({}) is None
    info["url"] = "file:///etc/passwd"
    assert commons.normalize_candidate(page) is None


def test_subtitles_and_provider_errors(monkeypatch, candidate):
    vtt = "WEBVTT\n\n00:01:12.000 --> 00:01:16.500 align:start\nA <b>robot runs</b>\n\n"
    assert commons.parse_subtitles(vtt) == [f.Cue(72, 76.5, "A robot runs")]
    assert commons.parse_subtitles("1\n01:02,000 --> 01:04,000\nHello\n") == [f.Cue(62, 64, "Hello")]
    monkeypatch.setattr(commons, "fetch_bytes", lambda *a: vtt.encode())
    assert commons.CommonsProvider().subtitles(replace(candidate, subtitle_urls=("https://example.org/sub",)))[0].start == 72
    assert commons.CommonsProvider().subtitles(candidate) == []
    monkeypatch.setattr(commons, "fetch_bytes", lambda *a: b'{"error":{"code":"maxlag"}}')
    with pytest.raises(f.FootageError, match="maxlag"):
        commons.CommonsProvider().search("robot")


@pytest.mark.parametrize("changes,allowed", [
    ({}, True), ({"license": "unknown", "license_url": ""}, False),
    ({"license": "CC BY-NC 4.0"}, False), ({"license": "CC BY-SA 4.0"}, False),
    ({"license": "CC BY-ND 4.0"}, False), ({"restrictions": "Personality rights"}, False),
    ({"creator": ""}, False), ({"license_url": "https://creativecommons.org.evil.test/licenses/by/4.0"}, False),
    ({"license": "Public domain", "attribution_required": False}, True),
    ({"license": "CC0", "attribution_required": False}, True),
    ({"license": "Public domain", "attribution_required": None}, False),
])
def test_license_policy(candidate, changes, allowed):
    c = replace(candidate, **changes)
    assert f.license_allowed(c)  # unrestricted default never invents a rights gate
    assert f.license_allowed(c, safe_only=True) is allowed


def test_ranking_requires_relevance_and_deduplicates(candidate):
    intent = f.VisualIntent("A Unitree robot running", "Unitree robot running", 4)
    unrelated = replace(candidate, title="Ocean sunset", media_url="https://example.org/ocean")
    tag_match = replace(candidate, title="A demonstration", tags=("Unitree", "robot", "running"),
                        media_url="https://example.org/tags")
    bad = [replace(candidate, duration=float("nan")), replace(candidate, duration=700),
           replace(candidate, size=300_000_000), replace(candidate, width=0)]
    assert f.rank_candidates([unrelated, tag_match, candidate, candidate, *bad], intent) == [candidate, tag_match]
    assert f.rank_candidates([replace(candidate, license="unknown")], intent, True) == []


def test_cache_default_is_external(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert f.cache_dir() == tmp_path / "sofit" / "footage"
    assert f.canonical_url("https://EXAMPLE.org:443/a?token=abc#time") == "https://example.org/a?token=abc"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://example.org/x", "https://user:secret@example.org/x"])
def test_rejects_unsafe_schemes_and_credentials(url):
    with pytest.raises(f.FootageError):
        f.canonical_url(url)


def test_rejects_private_addresses_and_redirects(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(f.FootageError, match="non-public"):
        f._public_url("https://example.org/a")
    with pytest.raises(f.FootageError, match="non-public"):
        f._PublicRedirect().redirect_request(None, None, 302, "", {}, "https://127.0.0.1/a")
    with pytest.raises(f.FootageError, match="non-public"):
        f._connect_public(("example.org", 443))


class Response(io.BytesIO):
    def __init__(self, data=b"video", length="5"):
        super().__init__(data)
        self.headers = {} if length is None else {"Content-Length": length}


def test_cache_reuses_verifies_and_recovers(monkeypatch, tmp_path, candidate):
    calls = []
    def download(*a, **k):
        calls.append(a)
        return Response()
    monkeypatch.setattr(f, "open_url", download)
    monkeypatch.setattr(f, "probe_video", lambda *a: f.VideoInfo(90, 1280, 720))
    cache = f.FootageCache(tmp_path)
    media, provenance = cache.retrieve(candidate)
    assert provenance["candidate"] == json.loads(json.dumps(asdict(candidate)))
    assert provenance["retrieved_at"].endswith("+00:00")
    assert cache.retrieve(replace(candidate, media_url=candidate.media_url + "#other"))[0] == media
    assert len(calls) == 1
    media.write_bytes(b"damage")
    assert cache.retrieve(candidate)[0].read_bytes() == b"video"
    assert len(calls) == 2
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("failure", ["partial", "size-header", "size-stream", "timeout", "http", "invalid", "deadline"])
def test_failed_downloads_never_become_cache_hits(monkeypatch, tmp_path, candidate, failure):
    def download(*a, **k):
        if failure == "timeout":
            raise TimeoutError()
        if failure == "http":
            raise urllib.error.HTTPError(candidate.media_url, 404, "Missing", {}, None)
        if failure == "partial":
            return Response(b"x", "5")
        if failure == "size-header":
            return Response(b"x", "999")
        if failure == "size-stream":
            return Response(b"x" * 20, None)
        return Response()
    def probe(*a):
        if failure == "invalid":
            raise f.FootageError("invalid video")
        return f.VideoInfo(90, 1280, 720)
    monkeypatch.setattr(f, "open_url", download)
    monkeypatch.setattr(f, "probe_video", probe)
    limits = f.Limits(max_bytes=10, download_seconds=-1 if failure == "deadline" else 180)
    with pytest.raises((OSError, f.FootageError)):
        f.FootageCache(tmp_path, limits).retrieve(candidate)
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("source.media"))
    assert not list(tmp_path.rglob("source.json"))


def test_invalid_probe_and_limits(monkeypatch, tmp_path, candidate):
    path = tmp_path / "bad.media"
    path.write_bytes(b"not video")
    monkeypatch.setattr(f.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="{}"))
    with pytest.raises(f.FootageError):
        f.probe_video(path)
    for c in [replace(candidate, duration=700), replace(candidate, size=300_000_000)]:
        with pytest.raises(f.FootageError):
            f.FootageCache(tmp_path).retrieve(c)


def test_atomic_spec_failure_preserves_previous(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text('{"previous":true}')
    with pytest.raises(ValueError):
        f.write_json(path, {"bad": float("nan")})
    assert json.loads(path.read_text()) == {"previous": True}
    assert not list(tmp_path.glob("*.part"))


def test_youtube_range_download_reassembles_exact_bytes(tmp_path, monkeypatch):
    payload = b'a' * (1024 * 1024 + 17)
    requests = []
    candidate = f.Candidate('youtube', 'https://www.youtube.com/watch?v=abcdefghijk',
                            'https://r1.googlevideo.com/video', 'robot', 10, 320, 240)

    def download(url, timeout, byte_range):
        start, end = byte_range
        requests.append(byte_range)
        stop = min(end, len(payload) - 1)
        response = Response(payload[start:stop + 1])
        response.status = 206
        response.headers = {'Content-Range': f'bytes {start}-{stop}/{len(payload)}',
                            'Content-Length': str(stop - start + 1)}
        return response

    monkeypatch.setattr(f, 'open_url', download)
    monkeypatch.setattr(f, 'probe_video', lambda *a: f.VideoInfo(10, 320, 240))
    media, metadata = f.FootageCache(tmp_path).retrieve(candidate)
    assert media.read_bytes() == payload
    assert requests == [(0, 1048575), (1048576, 1048592)]
    assert metadata['size'] == len(payload)
    assert not list(tmp_path.rglob('*.part'))


@pytest.mark.parametrize('content_range,body,length', [
    ('bytes 1-3/4', b'abc', '3'),
    ('bytes 0-3/4', b'ab', '4'),
    ('bytes 0-3/4', b'abcd', '3'),
    ('bytes 0-3/*', b'abcd', '4'),
    ('bytes 0-3/99999999999', b'abcd', '4'),
])
def test_invalid_youtube_ranges_never_publish(tmp_path, monkeypatch, content_range, body, length):
    candidate = f.Candidate('youtube', 'https://www.youtube.com/watch?v=abcdefghijk',
                            'https://r1.googlevideo.com/video', 'robot', 10, 320, 240)
    response = Response(body)
    response.status = 206
    response.headers = {'Content-Range': content_range, 'Content-Length': length}
    monkeypatch.setattr(f, 'open_url', lambda *a: response)
    with pytest.raises(f.FootageError):
        f.FootageCache(tmp_path).retrieve(candidate)
    assert not list(tmp_path.rglob('source.media'))
    assert not list(tmp_path.rglob('*.part'))
