"""Output formatting. Pure functions — no I/O, no models — so they're fully
testable without Whisper or the Claude API.

Bidi note: md/txt output wraps the LTR "H:MM:SS —" prefix in a Left-to-Right
Mark so it doesn't visually reorder the RTL Hebrew title in terminals and
markdown renderers. The YouTube format is EXEMPT: YouTube's automatic chapter
detector parses the timestamp line, and invisible bidi control characters make
it silently fail to detect chapters.
"""

from __future__ import annotations

import json
import re

from .generate import Chapter, Quote

LRM = "‎"  # Left-to-Right Mark


def fmt_timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def render_chapters_md(chapters: list[Chapter]) -> str:
    # LRM before the timestamp keeps the line visually LTR-then-RTL.
    return "\n".join(f"{LRM}{fmt_timestamp(c.start)} — {c.title}" for c in chapters)


def render_chapters_youtube(chapters: list[Chapter], audio_end: float, min_gap: float = 10.0) -> str:
    """Plain-text timestamp block for description-based chapters. First at 0:00,
    ascending, >=3 chapters, each at least `min_gap` seconds apart. Returns "" if
    fewer than 3 survive (caller should fall back to md and warn). Never emits bidi
    marks — invisible control chars break both YouTube's and Spotify's parsers.

    min_gap: YouTube requires >=10s between chapter starts; Spotify requires >=30s.
    """
    if not chapters:
        return ""
    ch = sorted(chapters, key=lambda c: c.start)
    if ch[0].start > 0:
        ch = [Chapter(start=0.0, title=ch[0].title)] + ch[1:]

    # Keep a chapter only if it starts >= min_gap after the last kept one. Folds
    # any too-close chapter into its predecessor, including one right after 0:00.
    merged: list[Chapter] = []
    for c in ch:
        if merged and c.start - merged[-1].start < min_gap:
            continue
        merged.append(c)

    if len(merged) < 3:
        return ""  # both platforms need >=3; signal fallback
    return "\n".join(f"{fmt_timestamp(c.start)} {c.title}" for c in merged)


def render_chapters_podcast_json(chapters: list[Chapter]) -> str:
    """Podcasting 2.0 chapters JSON. Host this file and point your RSS item at it
    with <podcast:chapters url="..." type="application/json+chapters" />. Read by
    Overcast, Fountain, Podcast Addict, and other modern podcast apps."""
    doc = {
        "version": "1.2.0",
        "chapters": [{"startTime": round(c.start, 3), "title": c.title} for c in chapters],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def render_shownotes_md(notes: dict) -> str:
    lines = [notes.get("summary", "").strip(), ""]
    lines += [f"- {b}" for b in notes.get("bullets", [])]
    return "\n".join(lines).strip()


def render_quotes_md(quotes: list[Quote]) -> str:
    return "\n".join(
        f"{LRM}{fmt_timestamp(q.start)}–{fmt_timestamp(q.end)} — {q.text}" for q in quotes
    )


RLE = "‫"  # Right-to-Left Embedding
PDF = "‬"  # Pop Directional Formatting
_HEBREW = re.compile(r"[֐-׿]")


def rtl_caption(text: str) -> str:
    """Force RTL paragraph order on a Hebrew social caption.

    Measured 2026-09-10 against live TikTok and Instagram posts: neither
    renders captions with dir=auto. TikTok's caption node computes
    `direction: ltr; unicode-bidi: isolate`, and Instagram's dir=auto sits on a
    wrapper that opens with the Latin account handle, so it always resolves
    LTR. In an LTR paragraph every Hebrew run is placed left-to-right in
    logical order, so any sentence carrying a Latin brand name comes out with
    its segments transposed — "אשתמש ב-Instinct בעוד כמה חודשים" reads as
    "בעוד כמה חודשים ב-Instinct אשתמש", and trailing punctuation jumps to the
    wrong edge.

    A bare RLM cannot fix that: the container's direction is explicit, not
    auto, so the paragraph needs an embedding to override it. RLE...PDF around
    each paragraph does, verified in the same container the platforms use.

    Hashtag-only paragraphs are left ALONE. Both platforms re-parse hashtags
    into one element per tag, which isolates them anyway, and an invisible
    control character next to a "#" risks ending up inside the tag itself.
    Order Hebrew tags before Latin ones instead.
    """
    out = []
    for para in text.replace(RLE, "").replace(PDF, "").split("\n"):  # re-mark from clean
        stripped = para.strip()
        if not stripped or stripped.startswith("#") or not _HEBREW.search(para):
            out.append(para)
        else:
            out.append(RLE + para + PDF)
    return "\n".join(out)
