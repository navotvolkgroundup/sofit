"""YouTube discovery via optional yt-dlp; media uses Sofit's bounded downloader.

Only individual public YouTube videos are resolved. No browser cookies, user
configuration, plugins, playlists or generic website extractors are loaded.
"""
from __future__ import annotations

from .footage_progress import measured

import importlib.util
import base64
import json
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

from .footage import Candidate, Cue, FootageError, canonical_url, channel_preference, fetch_bytes, relevance
from .footage_commons import parse_subtitles


def available() -> bool:
    return importlib.util.find_spec("yt_dlp") is not None


def youtube_url(url: str) -> str | None:
    """Canonical video identity; never accept lookalike hosts or playlists."""
    p = urlsplit(canonical_url(url))
    if p.port not in (None, 443):
        return None
    if p.hostname == "youtu.be":
        video_id = p.path.strip("/")
    elif p.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if p.path == "/watch":
            video_id = parse_qs(p.query).get("v", [""])[0]
        elif p.path.startswith(("/shorts/", "/embed/", "/live/")):
            video_id = p.path.split("/")[-1]
        else:
            return None
    else:
        return None
    return f"https://www.youtube.com/watch?v={video_id}" if re.fullmatch(r"[\w-]{11}", video_id, re.ASCII) else None


def _error_tail(errors) -> str:
    """Bound diagnostics and redact URLs/credentials before exposing stderr."""
    errors.seek(max(0, errors.tell() - 8192))
    detail = errors.read().decode(errors="replace")
    detail = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", detail)
    detail = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", detail)
    detail = re.sub(r"https?://\S+", "<url>", detail)
    detail = re.sub(r"(?i)\b(?:authorization|cookie|set-cookie)\s*[:=][^\n]*", "[redacted credentials]", detail)
    detail = re.sub(r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\s*[:=]\s*[^\s,;]+", "[redacted credential]", detail)
    lines = [line.strip()[:512] for line in detail.splitlines() if line.strip()]
    return " | ".join(lines[-2:])


@measured("metadata")
def _extract(target: str, flat: bool = False) -> dict:
    if not available():
        raise FootageError("YouTube footage needs the youtube extra: pip install 'sofit-cli[youtube]'")
    cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs",
           "--no-cache-dir", "--no-playlist", "--skip-download", "--dump-single-json",
           "--no-progress", "--no-warnings", "--socket-timeout", "15", "--retries", "1",
           "--extractor-retries", "1", "--proxy", "", "--js-runtimes", "node",
           "--no-remote-components", "--use-extractors", "youtube.*"]
    if flat:
        cmd += ["--flat-playlist", "--playlist-end", "8"]
    else:
        # Video-only is ideal: podcast audio replaces source audio. Avoid
        # manifests/fragments so ffmpeg never fetches untrusted remote URLs.
        # Like Commons, prefer a practical preview-size source for bounded
        # discovery downloads; 1080p remains a fallback when no smaller stream exists.
        cmd += ["--format", "bv*[protocol=https][height<=720][height>=240]/"
                "bv*[protocol=https][height<=1080][height>=240]",
                "--extractor-args", "youtube:skip=hls,dash"]
    cmd += ["--", target]
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            proc = subprocess.run(cmd, stdout=output, stderr=errors, timeout=90)
            if proc.returncode:
                detail = _error_tail(errors)
                raise FootageError("YouTube extraction failed; update the youtube extra and install Deno or Node 22+. The video may be unavailable or blocked" + (f": {detail}" if detail else ""))
            if output.tell() > 8 * 1024 * 1024:
                raise FootageError("YouTube metadata exceeds size limit")
            output.seek(0)
            data = json.load(output)
        if not isinstance(data, dict):
            raise ValueError("invalid metadata")
        return data
    except (subprocess.TimeoutExpired, ValueError) as e:
        raise FootageError("YouTube metadata timed out or was invalid") from e


def normalize_candidate(info: dict) -> Candidate | None:
    try:
        source = youtube_url(info["webpage_url"])
        if (not source or info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}
                or info.get("has_drm") or info.get("age_limit", 0) > 0
                or info.get("availability") not in {None, "public", "unlisted"}):
            return None
        media = canonical_url(info["url"])
        host = urlsplit(media).hostname or ""
        if not host.endswith(".googlevideo.com") or info.get("protocol") != "https":
            return None
        published = ""
        if info.get("upload_date"):
            published = datetime.strptime(info["upload_date"], "%Y%m%d").date().isoformat()
        subtitle_urls = []
        for tracks in [info.get("subtitles") or {}, info.get("automatic_captions") or {}]:
            for lang in sorted(tracks, key=lambda lang: (not lang.startswith("en"), lang)):
                for track in tracks[lang]:
                    url = track.get("url", "")
                    if track.get("ext") == "vtt" and urlsplit(url).hostname in {"www.youtube.com", "youtube.com"}:
                        subtitle_urls.append(canonical_url(url))
                        break
                if subtitle_urls:
                    break
            if subtitle_urls:
                break
        creator = info.get("channel") or info.get("uploader") or ""
        license_name = info.get("license") or "unknown"
        return Candidate(
            provider="youtube", source_url=source, media_url=media,
            title=info["title"], duration=float(info["duration"]),
            width=int(info["width"]), height=int(info["height"]),
            description=str(info.get("description") or "")[:20000],
            tags=tuple(info.get("tags") or []), creator=creator,
            license=license_name, license_url=info.get("license_url") or "",
            attribution=f"{creator}; {source}; {license_name}",
            original_media_url=media, size=info.get("filesize"),
            subtitle_urls=tuple(subtitle_urls[:1]), published_at=published,
            channel_url=info.get("channel_url") or "",
            cache_url=source + "&sofit_format=" + str(info.get("format_id") or "video"))
    except (KeyError, TypeError, ValueError, FootageError):
        return None


class YouTubeProvider:
    name = "youtube"

    def __init__(self, prefer_recent: bool = False, published_after: str = "",
                 preferred_channels: tuple[str, ...] = ()):
        self.prefer_recent = prefer_recent or bool(published_after)
        self.published_after = published_after
        self.preferred_channels = preferred_channels

    def resolve(self, url: str) -> Candidate | None:
        source = youtube_url(url)
        if not source:
            raise FootageError("expected an individual YouTube video URL")
        return normalize_candidate(_extract(source))

    def search(self, query: str, limit: int = 8) -> list[Candidate]:
        limit = min(max(limit, 1), 8)
        target = f"ytsearch{limit}:{query}"
        if self.prefer_recent:
            # YouTube removed sort-by-upload-date (ytsearchdate). Use an upload
            # window, then verify exact dates from individual video metadata.
            age = ((datetime.now(timezone.utc).date() - date.fromisoformat(self.published_after)).days
                   if self.published_after else 30)
            window = 3 if age <= 7 else 4 if age <= 30 else 5 if age <= 365 else None
            if window:
                params = base64.b64encode(bytes([18, 4, 8, window, 16, 1])).decode()
                target = "https://www.youtube.com/results?" + urlencode({"search_query": query, "sp": params})
        entries = _extract(target, flat=True).get("entries") or []
        # Full extraction is bounded; prefer plausible metadata before resolving.
        entries = sorted((e for e in entries[:limit] if isinstance(e, dict)),
                         key=lambda e: -(relevance(query, str(e.get("title", "")) + " " +
                                                   str(e.get("channel") or e.get("uploader") or "")) +
                             0.3 * channel_preference(e.get("channel") or e.get("uploader") or "",
                                                      e.get("channel_url") or "", self.preferred_channels)))
        candidates = []
        seen = set()
        failures = 0
        for entry in entries:
            try:
                if entry.get("duration") and not 2 <= float(entry["duration"]) <= 600:
                    continue
                url = youtube_url(entry.get("url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                candidate = self.resolve(url)
                if candidate and (not self.published_after or candidate.published_at >= self.published_after):
                    candidates.append(candidate)
            except (FootageError, ValueError, TypeError):
                failures += 1  # one deleted/blocked result must not hide others
            if len(seen) >= 4:
                break
        if failures:
            print(f"warning: {failures} YouTube result(s) unavailable; check/update the youtube extra if this persists", file=sys.stderr)
        return candidates

    def subtitles(self, candidate: Candidate) -> list[Cue]:
        for url in candidate.subtitle_urls:
            cues = parse_subtitles(fetch_bytes(url).decode("utf-8-sig"))
            if cues:
                return cues
        return []
