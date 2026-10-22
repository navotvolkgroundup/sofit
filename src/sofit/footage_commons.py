"""Wikimedia Commons video discovery. Public MediaWiki API; no key or SDK."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from .footage import Candidate, Cue, FootageError, fetch_bytes

API = "https://commons.wikimedia.org/w/api.php"


def _plain(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(value))).split())


def _media_url(url: str) -> str:
    # Commons adds analytics parameters to identical originals in two API
    # fields. Remove ONLY its known tracking keys, never arbitrary signatures.
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname != "upload.wikimedia.org":
        raise FootageError("unexpected Commons media host")
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k not in {"utm_source", "utm_campaign", "utm_content"}]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))


def normalize_candidate(page: dict) -> Candidate | None:
    """Untrusted provider records -> the provider-independent contract."""
    try:
        info = page["videoinfo"][0]
        if not info.get("mime", "").startswith("video/"):
            return None
        metadata = info.get("extmetadata", {})

        def field(key):
            return _plain(metadata.get(key, {}).get("value", ""))

        original = _media_url(info["url"])
        width, height = int(info["width"]), int(info["height"])
        url, size = original, int(info["size"])
        derivatives = [d for d in info.get("derivatives", [])
                       if d.get("transcodekey") and d.get("type", "").startswith("video/")
                       and 360 <= int(d.get("height", 0)) <= 1080]
        if derivatives:
            chosen = min(derivatives, key=lambda d: abs(int(d["height"]) - 720))
            url = _media_url(chosen["src"])
            width, height = int(chosen["width"]), int(chosen["height"])
            size = None  # a transcode's Content-Length differs from the original
        required = field("AttributionRequired").lower()
        creator, license_name = field("Artist"), field("LicenseShortName") or "unknown"
        credit = field("Attribution") or field("Credit")
        attribution = "; ".join(v for v in (creator, credit, field("UsageTerms")) if v)
        subtitles = []
        for t in sorted(info.get("timedtext", []), key=lambda t: t.get("srclang") != "en"):
            p = urlsplit(t.get("src", ""))
            if (p.scheme == "https" and p.hostname == "commons.wikimedia.org"
                    and p.path == "/w/api.php"):
                subtitles.append(t["src"])
        return Candidate(
            provider="commons", source_url=info["descriptionurl"], media_url=url,
            original_media_url=original, title=field("ObjectName") or page["title"],
            description=field("ImageDescription"), tags=tuple(field("Categories").split("|")),
            duration=float(info["duration"]), width=width, height=height, size=size,
            creator=creator, license=license_name, license_url=field("LicenseUrl"),
            attribution=attribution,
            attribution_required={"true": True, "false": False}.get(required),
            restrictions=field("Restrictions"), subtitle_urls=tuple(subtitles[:2]))
    except (KeyError, IndexError, TypeError, ValueError, FootageError):
        return None


def parse_subtitles(text: str) -> list[Cue]:
    """The Commons timedtext API emits WebVTT; SRT timestamps also work."""
    cues = []

    def seconds(stamp):
        parts = stamp.replace(",", ".").split(":")
        return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))

    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        match = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s+-->\s+"
                          r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})[^\n]*\n(.+)", block, re.S)
        if match:
            start, end = seconds(match[1]), seconds(match[2])
            if 0 <= start < end:
                cues.append(Cue(start, end, _plain(match[3])))
    return cues


class CommonsProvider:
    name = "commons"

    def search(self, query: str, limit: int = 8) -> list[Candidate]:
        params = {"action": "query", "format": "json", "formatversion": 2,
                  "generator": "search", "gsrsearch": query + " filetype:video",
                  "gsrnamespace": 6, "gsrlimit": min(max(limit, 1), 20),
                  "prop": "videoinfo", "viprop": "url|size|mime|extmetadata|derivatives|timedtext",
                  "viextmetadatalanguage": "en"}
        data = json.loads(fetch_bytes(API + "?" + urlencode(params)))
        if "error" in data:
            raise FootageError("Commons search failed: " + str(data["error"].get("code", "unknown")))
        return [c for p in data.get("query", {}).get("pages", [])
                if (c := normalize_candidate(p)) is not None]

    def subtitles(self, candidate: Candidate) -> list[Cue]:
        if candidate.cues:
            return list(candidate.cues)
        for url in candidate.subtitle_urls:
            cues = parse_subtitles(fetch_bytes(url).decode("utf-8-sig"))
            if cues:
                return cues
        return []
