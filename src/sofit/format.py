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


RLM = "‏"  # Right-to-Left Mark
_HEBREW = re.compile(r"[֐-׿]")
_LATIN = re.compile(r"[A-Za-z]")


def rtl_caption(text: str) -> str:
    """Pin a Hebrew social caption to RTL so the platforms don't reorder it.

    TikTok/IG/YouTube render each paragraph with dir=auto, which takes its
    direction from the FIRST strong character. Two things break:

    - A paragraph opening on a Latin brand name ("Decart ספרה...") resolves the
      whole paragraph to LTR and scrambles the Hebrew segment order.
    - A Latin hashtag among Hebrew ones ("#וויקליסינק #Instinct #AI") comes out
      in reversed order with its "#" reading on the far side of the word.

    Both are fixed by making the direction explicit: an RLM at the head of any
    paragraph that starts Latin, and one closing each Latin-script hashtag.
    """
    out = []
    for para in text.replace(RLM, "").split("\n"):  # idempotent: re-mark from clean
        if not para.strip():
            out.append(para)
            continue
        if _HEBREW.search(para):
            para = re.sub(r"(#[A-Za-z][^\s#]*)", r"\1" + RLM, para)
            strong = _HEBREW.search(para), _LATIN.search(para)
            if strong[1] and (not strong[0] or strong[1].start() < strong[0].start()):
                para = RLM + para
        out.append(para)
    return "\n".join(out)
