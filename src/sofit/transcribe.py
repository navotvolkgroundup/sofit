"""Local transcription with faster-whisper + a content-addressed cache.

Transcription is the expensive step, so we cache the result keyed by the file
content plus the settings that affect output. Every downstream generator
(chapters / show notes / quotes) reuses one cached transcript, so re-runs and
prompt-iteration are effectively free.

faster-whisper decodes mp3 and mp4 directly (via PyAV/ffmpeg), so there is no
separate audio-extraction step for the common case.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    text: str
    # faster-whisper's per-word probability, when the transcript was produced
    # by a version that stores it. Older caches omit it; defiller then relies
    # on pauses alone for the ambiguous short tokens (אה / אם).
    probability: float | None = None


@dataclass
class Segment:
    index: int
    start: float
    end: float
    text: str
    words: list[Word]


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    d = Path(base) / "sofit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_key(path: str, model: str, lang: str, compute_type: str) -> str:
    """Content-addressed key. Includes the faster-whisper version and compute
    settings so a library upgrade or a settings change invalidates stale
    transcripts instead of silently reusing them."""
    try:
        import faster_whisper

        fw_version = getattr(faster_whisper, "__version__", "unknown")
    except Exception:  # pragma: no cover - only when dep missing
        fw_version = "unknown"
    raw = "|".join([_file_sha256(path), model, lang, fw_version, compute_type])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _load_cache(key: str) -> list[Segment] | None:
    p = _cache_dir() / f"{key}.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return [
        Segment(
            index=s["index"], start=s["start"], end=s["end"], text=s["text"],
            words=[Word(**w) for w in s["words"]],
        )
        for s in data
    ]


def _save_cache(key: str, segments: list[Segment]) -> None:
    p = _cache_dir() / f"{key}.json"
    p.write_text(json.dumps([asdict(s) for s in segments], ensure_ascii=False))


# ivrit-ai fine-tunes Whisper on Hebrew and ships CTranslate2 builds that
# faster-whisper loads directly by repo id. It vastly out-transcribes stock
# Whisper on Hebrew (and segments better). `--model medium` etc. still work.
DEFAULT_MODEL = "ivrit-ai/whisper-large-v3-turbo-ct2"


def cached_segments(
    media_path: str,
    model: str = DEFAULT_MODEL,
    lang: str = "he",
    compute_type: str = "int8",
) -> list[Segment] | None:
    """Return the cached transcript for this file+settings, or None if not yet
    transcribed. Never runs the model — safe for a quick "is it ready?" check."""
    if not os.path.exists(media_path):
        return None
    return _load_cache(cache_key(media_path, model, lang, compute_type))


def transcribe(
    media_path: str,
    model: str = DEFAULT_MODEL,
    lang: str = "he",
    compute_type: str = "int8",
    use_cache: bool = True,
) -> list[Segment]:
    """Transcribe a media file to word-timestamped segments.

    Returns an empty list when no speech is detected. Raises FileNotFoundError
    if the path is missing.
    """
    if not os.path.exists(media_path):
        raise FileNotFoundError(media_path)

    key = cache_key(media_path, model, lang, compute_type)
    if use_cache:
        cached = _load_cache(key)
        if cached is not None:
            return cached

    # Imported lazily so `--help`, cache hits, and tests don't pay the import cost.
    from faster_whisper import WhisperModel

    wm = WhisperModel(model, compute_type=compute_type)
    raw_segments, _info = wm.transcribe(media_path, language=lang, word_timestamps=True)

    segments: list[Segment] = []
    for i, seg in enumerate(raw_segments):
        words = [
            Word(start=w.start, end=w.end, text=w.word,
                 probability=getattr(w, "probability", None))
            for w in (seg.words or [])
        ]
        segments.append(
            Segment(index=i, start=seg.start, end=seg.end, text=seg.text.strip(), words=words)
        )

    if use_cache and segments:
        _save_cache(key, segments)
    return segments
