"""Feed/URL resolution tests. Network is stubbed — no real fetching."""

import pytest

from sofit import feed

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>WeeklySync</title>
  <item><title>Ep 3 (latest)</title><enclosure url="https://x/ep3.mp3" type="audio/mpeg"/></item>
  <item><title>Ep 2</title><enclosure url="https://x/ep2.mp3" type="audio/mpeg"/></item>
  <item><title>Ep 1 (no audio)</title></item>
</channel></rss>"""


def test_is_url():
    assert feed.is_url("https://x/feed.xml")
    assert feed.is_url("http://x/feed.xml")
    assert not feed.is_url("/Users/me/ep.mp3")
    assert not feed.is_url("ep.mp4")


def test_list_episodes_parses_enclosures(monkeypatch):
    monkeypatch.setattr(feed, "_fetch", lambda url, timeout=60: RSS)
    eps = feed.list_episodes("https://x/feed.xml")
    assert [e.title for e in eps] == ["Ep 3 (latest)", "Ep 2"]  # item with no enclosure dropped
    assert eps[0].url == "https://x/ep3.mp3"


def test_local_path_passes_through():
    assert feed.resolve("/Users/me/ep.mp3") == "/Users/me/ep.mp3"


def test_resolve_feed_picks_episode(monkeypatch):
    monkeypatch.setattr(feed, "_fetch", lambda url, timeout=60: RSS)
    monkeypatch.setattr(feed, "download_audio", lambda url: f"/cache/{url.rsplit('/', 1)[1]}")
    assert feed.resolve("https://x/feed.xml", episode=1) == "/cache/ep3.mp3"  # latest
    assert feed.resolve("https://x/feed.xml", episode=2) == "/cache/ep2.mp3"


def test_resolve_episode_out_of_range(monkeypatch):
    monkeypatch.setattr(feed, "_fetch", lambda url, timeout=60: RSS)
    with pytest.raises(feed.FeedError):
        feed.resolve("https://x/feed.xml", episode=9)


def test_is_youtube():
    assert feed.is_youtube("https://www.youtube.com/watch?v=abc123")
    assert feed.is_youtube("https://youtu.be/abc123")
    assert feed.is_youtube("https://music.youtube.com/watch?v=abc")
    assert not feed.is_youtube("https://feeds.example.com/show.xml")


def test_resolve_youtube_routes_to_downloader(monkeypatch):
    # A YouTube URL must go to download_youtube, not the feed parser or audio downloader.
    monkeypatch.setattr(feed, "_fetch", lambda *a, **k: pytest.fail("should not parse feed"))
    monkeypatch.setattr(feed, "download_youtube", lambda url, video=False: "/cache/yt.m4a")
    assert feed.resolve("https://www.youtube.com/watch?v=abc123") == "/cache/yt.m4a"


def test_youtube_without_ytdlp_raises(monkeypatch):
    # Simulate yt-dlp not installed → clear, actionable error.
    import builtins
    real_import = builtins.__import__

    def no_ytdlp(name, *a, **k):
        if name == "yt_dlp":
            raise ImportError("no yt_dlp")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_ytdlp)
    with pytest.raises(feed.FeedError, match="yt-dlp"):
        feed.download_youtube("https://youtu.be/abc")


def test_resolve_direct_audio_url_skips_feed_parse(monkeypatch):
    # A direct audio URL must download without fetching/parsing a feed.
    monkeypatch.setattr(feed, "_fetch", lambda *a, **k: pytest.fail("should not parse feed"))
    monkeypatch.setattr(feed, "download_audio", lambda url: "/cache/direct.mp3")
    assert feed.resolve("https://x/show/episode.mp3") == "/cache/direct.mp3"


def test_youtube_video_download_asks_for_video_and_caches_separately(monkeypatch, tmp_path):
    import sys, types
    seen = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def extract_info(self, url, download):
            seen.append(self.opts)
            ext = "mp4" if "merge_output_format" in self.opts else "webm"
            open(self.opts["outtmpl"].replace("%(ext)s", ext), "wb").write(b"x")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    monkeypatch.setattr(feed, "_cache_dir", lambda: tmp_path)
    url = "https://www.youtube.com/watch?v=abc"
    audio = feed.download_youtube(url)
    video = feed.download_youtube(url, video=True)
    assert audio != video and video.endswith(".mp4")
    assert seen[0]["format"] == "bestaudio/best"
    assert "height<=1080" in seen[1]["format"] and seen[1]["merge_output_format"] == "mp4"
