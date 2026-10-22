"""Web footage contracts, inexpensive search ranking, and bounded local caching.

Providers return metadata, never renderer instructions. Only downloaded, probed
local video reaches ffmpeg; the final asset uses the existing cutaway contract.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol


class FootageError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisualIntent:
    intent: str
    query: str
    duration: float
    context: str = ""
    prefer_recent: bool = False
    published_after: str = ""
    source_urls: tuple[str, ...] = ()
    required_terms: tuple[str, ...] = ()
    preferred_channels: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.intent.strip() or not self.query.strip():
            raise ValueError("footage needs a visual intent and search query")
        if not math.isfinite(self.duration) or not 2 <= self.duration <= 30:
            raise ValueError("footage duration must be between 2 and 30 seconds")
        if self.published_after:
            date.fromisoformat(self.published_after)
        if len(self.source_urls) > 8:
            raise ValueError("at most eight footage URLs per beat")
        for url in self.source_urls:
            canonical_url(url)
        for items in (self.required_terms, self.preferred_channels):
            if (not isinstance(items, (list, tuple)) or len(items) > 8
                    or any(not isinstance(t, str) or not t.strip() for t in items)):
                raise ValueError("subject terms and publisher names must be lists of up to eight strings")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Candidate:
    provider: str
    source_url: str
    media_url: str
    title: str
    duration: float
    width: int
    height: int
    description: str = ""
    tags: tuple[str, ...] = ()
    creator: str = ""
    license: str = "unknown"
    license_url: str = ""
    attribution: str = ""
    attribution_required: bool | None = None
    restrictions: str = ""
    original_media_url: str = ""
    size: int | None = None
    subtitle_urls: tuple[str, ...] = ()
    cues: tuple[Cue, ...] = ()
    published_at: str = ""
    channel_url: str = ""
    # Stable public identity for hosts whose media URLs expire (e.g. YouTube).
    cache_url: str = ""


def candidate_from_dict(data: dict) -> Candidate:
    """Restore nested timed text from a JSON cache manifest."""
    data = dict(data)
    data["cues"] = tuple(c if isinstance(c, Cue) else Cue(**c) for c in data.get("cues", ()))
    data["tags"] = tuple(data.get("tags", ()))
    data["subtitle_urls"] = tuple(data.get("subtitle_urls", ()))
    return Candidate(**data)


class FootageProvider(Protocol):
    """A provider owns discovery and optional timed text, not downloading/rendering."""

    name: str

    def search(self, query: str, limit: int = 8) -> list[Candidate]: ...

    def subtitles(self, candidate: Candidate) -> list[Cue]: ...


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 256 * 1024 * 1024
    max_duration: float = 600
    timeout: float = 30
    download_seconds: float = 180


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "sofit" / "footage"


def canonical_url(url: str) -> str:
    """Keep query parameters (including signatures); fragments never identify bytes."""
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() != "https" or not p.hostname or p.username or p.password:
        raise FootageError("footage requires an HTTPS URL without credentials")
    host = p.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = p.port
    return urllib.parse.urlunsplit(("https", host + (f":{port}" if port and port != 443 else ""),
                                    p.path or "/", p.query, ""))


def _public_url(url: str) -> str:
    url = canonical_url(url)
    p = urllib.parse.urlsplit(url)
    addresses = socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise FootageError("footage URL resolves to a non-public address")
    return url


def _connect_public(address, timeout=30, source_address=None, **kwargs):
    """Connect to the validated numeric address, closing the DNS-rebinding gap."""
    host, port = address
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise FootageError("footage URL resolves to a non-public address")
    last = None
    for family, kind, proto, _, destination in addresses:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(destination)
            return sock
        except OSError as e:
            last = e
            sock.close()
    raise last or FootageError("no public address available")


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _public_url(newurl))


def open_url(url: str, timeout: float = 30, byte_range: tuple[int, int] | None = None):
    """Validate initial and redirected destinations; never accept local/file URLs."""
    req = urllib.request.Request(_public_url(url), headers={
        "User-Agent": "Sofit/0.7 (https://github.com/navotvolkgroundup/sofit)",
        "Accept-Encoding": "identity",
    })
    if byte_range is not None:
        req.add_header("Range", f"bytes={byte_range[0]}-{byte_range[1]}")
    # Direct HTTPS preserves the validated destination. Environment proxies
    # could resolve/connect elsewhere; this downloader intentionally ignores them.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _PublicRedirect(),
                                       _PublicHTTPSHandler()).open(req, timeout=timeout)


def fetch_bytes(url: str, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    deadline = time.monotonic() + 60
    data = bytearray()
    with open_url(url) as response:
        for chunk in iter(lambda: response.read1(64 * 1024), b""):
            data.extend(chunk)
            if len(data) > max_bytes or time.monotonic() > deadline:
                raise FootageError("metadata response exceeded size/time limit")
    return bytes(data)


def _tokens(text: str) -> set[str]:
    ignored = {"a", "an", "the", "of", "on", "in", "at", "and", "with", "to", "for",
               "real", "footage", "video", "show", "showing", "file", "webm", "mp4"}
    return {t for t in re.findall(r"\w+", text.casefold()) if len(t) > 1 and t not in ignored}


def relevance(query: str, text: str) -> float:
    terms = _tokens(query)
    return len(terms & _tokens(text)) / len(terms) if terms else 0.0


def _phrase(text: str) -> str:
    text = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text.casefold())
    return " ".join(re.findall(r"[^\W_]+", text))


def matches_subject(candidate: Candidate, terms: tuple[str, ...]) -> bool:
    """Require subject words and intact version numbers across publisher metadata."""
    evidence = " " + _phrase(" ".join((candidate.title, candidate.description,
                                      candidate.creator, *candidate.tags))) + " "
    for term in terms:
        if not set(_phrase(term).split()) <= set(evidence.split()):
            return False
        # A title may omit the company when its publisher names it. But Helix
        # 2.0 plus a stray '5' elsewhere must never satisfy Helix 2.5.
        for version in re.findall(r"\d+(?:\.\d+)+", term):
            if " " + _phrase(version) + " " not in evidence:
                return False
    return True


def channel_preference(creator: str, channel_url: str, channels: tuple[str, ...]) -> bool:
    """Prefer an exact publisher name/handle; this is not verification of ownership."""
    names = {_phrase(creator), _phrase(urllib.parse.urlsplit(channel_url).path.rsplit("/", 1)[-1])}
    return any(_phrase(name) in names for name in channels if _phrase(name))


def license_allowed(candidate: Candidate, safe_only: bool = False) -> bool:
    """Default records supplied rights without gating. Safe-only is an allowlist.

    This is a metadata filter, not legal clearance. Share-alike/NC/ND/custom
    licenses deliberately require the unrestricted mode and editorial review.
    """
    if not safe_only:
        return True
    if candidate.restrictions.strip():
        return False
    name = candidate.license.strip().lower()
    if name in {"public domain", "pd", "cc0", "cc0 1.0"}:
        return candidate.attribution_required is False
    p = urllib.parse.urlsplit(candidate.license_url)
    return (p.scheme == "https" and p.hostname in {"creativecommons.org", "www.creativecommons.org"}
            and bool(re.fullmatch(r"/licenses/by/(1\.0|2\.0|2\.5|3\.0|4\.0)/?", p.path))
            and bool(re.fullmatch(r"cc by (1\.0|2\.0|2\.5|3\.0|4\.0)", name))
            and candidate.attribution_required is True
            and bool(candidate.creator.strip()) and bool(candidate.attribution.strip()))


def rank_candidates(candidates: list[Candidate], intent: VisualIntent,
                    safe_only: bool = False, limits: Limits = Limits()) -> list[Candidate]:
    """Semantic text overlap first; resolution/length only break plausible ties."""
    ranked = []
    seen = set()
    for c in candidates:
        try:
            url = canonical_url(c.cache_url or c.media_url)
        except (FootageError, ValueError):
            continue
        if (url in seen or not math.isfinite(c.duration)
                or not intent.duration <= c.duration <= limits.max_duration
                or min(c.width, c.height) < 240
                or (c.size is not None and not 0 < c.size <= limits.max_bytes)
                or not license_allowed(c, safe_only)):
            continue
        if not (c.provider == "direct" and c.source_url in intent.source_urls) and not matches_subject(c, intent.required_terms):
            from .footage_progress import count, event
            count("subject_rejections")
            event("candidate_rejected", reason="required_subject_not_in_metadata", source=hashlib.sha256(c.source_url.encode()).hexdigest()[:12])
            continue
        try:
            published = date.fromisoformat(c.published_at) if c.published_at else None
        except ValueError:
            published = None
        if intent.published_after and (published is None or published < date.fromisoformat(intent.published_after)):
            continue
        title = relevance(intent.query, c.creator + " " + c.title)
        body = relevance(intent.query, c.description + " " + " ".join(c.tags))
        cues = relevance(intent.query, " ".join(cue.text for cue in c.cues))
        semantic = max(title, 0.85 * body, 0.9 * cues)
        if intent.required_terms:
            # A release demonstration need not list every action in its title;
            # exact subject identity is gated above, visible action below in the VLM.
            semantic = max(semantic, 0.75 * relevance(" ".join(intent.required_terms),
                           c.creator + " " + c.title + " " + c.description))
        # Explicit links are editorial candidates, not evidence of visual relevance.
        # They still require the same frame confidence gate as search results.
        if c.source_url in intent.source_urls:
            semantic = max(semantic, 0.5)
        if semantic < 0.35:
            continue
        seen.add(url)
        quality = min(c.width * c.height / (1920 * 1080), 1)
        score = semantic + 0.05 * quality + 0.03 * min(intent.duration / c.duration, 1)
        if channel_preference(c.creator, c.channel_url, intent.preferred_channels):
            score += 0.3
        if intent.prefer_recent and published is not None:
            age = (datetime.now(timezone.utc).date() - published).days
            if age >= 0:
                score += 0.15 / (1 + age / 30)
        ranked.append((score, c))
    return [c for _, c in sorted(ranked, key=lambda pair: (-pair[0], pair[1].source_url))]


def probe_video(path: Path, limits: Limits = Limits()) -> VideoInfo:
    """ffprobe cannot follow network references hidden inside downloaded media."""
    if not path.is_file() or not 0 < path.stat().st_size <= limits.max_bytes:
        raise FootageError("missing, empty, or oversized footage")
    try:
        p = subprocess.run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov,matroska,webm,ogg,avi",
            "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,width,height,duration",
            "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        data = json.loads(p.stdout)
        stream = next(s for s in data.get("streams", []) if s.get("codec_type") == "video")
        duration = float(stream.get("duration") or data["format"]["duration"])
        width, height = int(stream["width"]), int(stream["height"])
        formats = set(data["format"]["format_name"].split(","))
        if (p.returncode or not stream.get("codec_name") or not math.isfinite(duration)
                or not 0 < duration <= limits.max_duration
                or not 0 < width <= 7680 or not 0 < height <= 7680
                or not formats & {"mov", "mp4", "matroska", "webm", "ogg", "avi"}):
            raise ValueError("unsupported or out-of-bounds video")
        return VideoInfo(duration, width, height)
    except (KeyError, ValueError, StopIteration, subprocess.SubprocessError) as e:
        raise FootageError("invalid or unsupported video") from e


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, data: dict) -> None:
    """Replace atomically; failed writes never destroy the previous spec/metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _download_into(url, output, limits, provider, cancelled=None):
    """Bounded HTTP ranges avoid throttled whole-file YouTube transfers.

    Partial responses must match the exact requested byte interval and stable
    total length. Other providers keep the ordinary single-response path.
    """
    from .footage_progress import count
    ranged = provider == "youtube" and (urllib.parse.urlsplit(url).hostname or "").endswith(".googlevideo.com")
    deadline = time.monotonic() + limits.download_seconds
    size, total = 0, None
    while True:
        if cancelled and cancelled():
            count("downloads_cancelled")
            raise FootageError("footage acquisition cancelled: visual service unavailable")
        if time.monotonic() > deadline:
            raise FootageError("footage download exceeded size/time limit")
        end = min(size + 1024 * 1024 - 1, (total or limits.max_bytes) - 1)
        count("download_requests")
        response = (open_url(url, limits.timeout, (size, end)) if ranged
                    else open_url(url, limits.timeout))
        with response:
            status = getattr(response, "status", 200)
            length = response.headers.get("Content-Length")
            expected = int(length) if length is not None else None
            if expected is not None and not 0 < expected <= limits.max_bytes:
                raise FootageError("source exceeds file size limit")
            if status == 206 and ranged:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match:
                    raise FootageError("missing or invalid footage byte range")
                start, stop, announced = map(int, match.groups())
                if not (start == size and start <= stop <= end
                        and stop < announced <= limits.max_bytes
                        and (total is None or total == announced)):
                    raise FootageError("unexpected footage byte range")
                total = announced
                if expected is not None and expected != stop - start + 1:
                    raise FootageError("inconsistent footage range length")
                expected = stop - start + 1
            elif status != 200 or size:
                raise FootageError("unexpected partial footage response")
            offset = size
            for chunk in iter(lambda: response.read1(64 * 1024), b""):
                size += len(chunk)
                count("bytes_downloaded", len(chunk))
                if cancelled and cancelled():
                    count("downloads_cancelled")
                    raise FootageError("footage acquisition cancelled: visual service unavailable")
                if size > limits.max_bytes or time.monotonic() > deadline:
                    raise FootageError("footage download exceeded size/time limit")
                if expected is not None and size - offset > expected:
                    raise FootageError("footage response exceeds declared length")
                output.write(chunk)
            if expected is not None and size - offset != expected:
                raise FootageError("partial footage download")
            if status == 200 or size == total:
                return size


class FootageCache:
    def __init__(self, root: Path | None = None, limits: Limits = Limits()):
        self.root = Path(root) if root is not None else cache_dir()
        self.limits = limits

    def retrieve(self, candidate: Candidate, *, cancelled=None) -> tuple[Path, dict]:
        from .footage_progress import count, stage
        url = canonical_url(candidate.media_url)
        identity = canonical_url(candidate.cache_url or url)
        key = hashlib.sha256(identity.encode()).hexdigest()
        directory = self.root / key
        directory.mkdir(parents=True, exist_ok=True)
        media, meta = directory / "source.media", directory / "source.json"
        if media.exists() and meta.exists():
            try:
                saved = json.loads(meta.read_text(encoding="utf-8"))
                if (saved.get("identity", saved["url"]) == identity and saved["size"] == media.stat().st_size
                        and saved["sha256"] == _hash(media)):
                    probe_video(media, self.limits)
                    count("cache_hit")
                    return media, saved
            except (OSError, ValueError, KeyError, FootageError):
                pass  # recover an interrupted/corrupt cache, without reusing its bytes
        if candidate.duration > self.limits.max_duration:
            raise FootageError("source exceeds duration limit")
        if candidate.size is not None and candidate.size > self.limits.max_bytes:
            raise FootageError("source exceeds file size limit")
        if cancelled and cancelled():
            raise FootageError("footage acquisition cancelled: visual service unavailable")
        count("cache_miss")
        fd, temp_name = tempfile.mkstemp(dir=directory, suffix=".part")
        temp = Path(temp_name)
        try:
            with stage("download"):
                with os.fdopen(fd, "wb") as output:
                    size = _download_into(url, output, self.limits, candidate.provider, cancelled)
                info = probe_video(temp, self.limits)
                saved = {"url": url, "identity": identity, "size": size, "sha256": _hash(temp),
                         "retrieved_at": datetime.now(timezone.utc).isoformat(),
                         "candidate": json.loads(json.dumps(asdict(candidate))), "video": asdict(info)}
                os.replace(temp, media)
                write_json(meta, saved)
                count("downloads_done")
                return media, saved
        finally:
            temp.unlink(missing_ok=True)
