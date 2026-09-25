"""Resolve a media source to a local file path.

A source can be a local path (returned as-is), a direct audio URL, or an RSS feed
URL. For a feed, an episode is selected by 1-based index (1 = the first/latest
item) and its enclosure audio is downloaded. Stdlib only — RSS is parsed with
xml.etree, downloads stream via urllib, and downloaded audio is cached by URL so
re-runs don't re-fetch.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXT = (".mp3", ".mp4", ".m4a", ".wav", ".aac", ".ogg", ".opus")
_UA = {"User-Agent": "sofit"}


class FeedError(RuntimeError):
    pass


@dataclass
class Episode:
    title: str
    url: str


def is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    d = Path(base) / "sofit" / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read()


def list_episodes(feed_url: str) -> list[Episode]:
    """Parse an RSS feed and return its episodes (items with an audio enclosure),
    in feed order — item 1 is whatever the feed lists first (usually the newest)."""
    try:
        root = ET.fromstring(_fetch(feed_url))
    except ET.ParseError as e:
        raise FeedError(f"could not parse RSS feed: {e}") from e
    episodes: list[Episode] = []
    for item in root.iter("item"):
        enc = item.find("enclosure")
        url = enc.get("url") if enc is not None else None
        if url:
            episodes.append(Episode(title=(item.findtext("title") or "untitled").strip(), url=url))
    return episodes


def download_audio(url: str) -> str:
    """Stream a media URL to the cache, keyed by URL. Returns the local path."""
    ext = os.path.splitext(url.split("?")[0])[1] or ".mp3"
    dest = _cache_dir() / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}{ext}"
    if dest.exists():
        return str(dest)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=120) as r, open(tmp, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            f.write(chunk)
    tmp.rename(dest)
    return str(dest)


def download_youtube(url: str, video: bool = False) -> str:
    """Download a YouTube video via yt-dlp (optional `[youtube]` extra), cached by
    URL. Audio only by default: faster-whisper decodes the m4a/webm directly.
    `video=True` fetches an mp4 of at most 1080p with sound, which clip rendering
    and `sofit clean` need (an audio-only source renders as a black audiogram)."""
    try:
        import yt_dlp
    except ImportError as e:
        raise FeedError("YouTube support needs yt-dlp — install: pip install 'sofit-cli[youtube]'") from e

    key = hashlib.sha256((url + ("|video" if video else "")).encode()).hexdigest()[:32]
    d = _cache_dir()
    cached = [p for p in d.glob(f"{key}.*") if not p.name.endswith(".part")]
    if cached:
        return str(cached[0])
    opts = {
        "format": ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b"
                   if video else "bestaudio/best"),
        "outtmpl": str(d / f"{key}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if video:
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    # Glob after download — the real extension isn't known until yt-dlp picks a stream.
    files = [p for p in d.glob(f"{key}.*") if not p.name.endswith(".part")]
    if not files:
        raise FeedError("yt-dlp produced no audio file")
    return str(files[0])


def resolve(source: str, episode: int = 1, video: bool = False) -> str:
    """Return a local media path. A local path is returned unchanged; a YouTube URL
    or direct audio URL is downloaded; an RSS feed URL has `episode` (1-based)
    downloaded."""
    if not is_url(source):
        return source
    if is_youtube(source):
        return download_youtube(source, video=video)
    if source.split("?")[0].lower().endswith(AUDIO_EXT):
        return download_audio(source)
    episodes = list_episodes(source)
    if not episodes:
        raise FeedError("no episodes with an audio enclosure found in this feed")
    if episode < 1 or episode > len(episodes):
        raise FeedError(f"--episode {episode} out of range (feed has {len(episodes)} episodes)")
    return download_audio(episodes[episode - 1].url)
