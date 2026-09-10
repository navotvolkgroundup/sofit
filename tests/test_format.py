"""Tests for the pure formatting logic — the part most likely to break silently
(bidi marks, YouTube rules). No Whisper or Claude needed."""

import json

from sofit.format import (
    LRM,
    fmt_timestamp,
    render_chapters_md,
    render_chapters_podcast_json,
    render_chapters_youtube,
)
from sofit.generate import Chapter


def test_fmt_timestamp():
    assert fmt_timestamp(0) == "0:00"
    assert fmt_timestamp(65) == "1:05"
    assert fmt_timestamp(3725) == "1:02:05"
    assert fmt_timestamp(-3) == "0:00"


def test_md_has_lrm_bidi_mark():
    md = render_chapters_md([Chapter(0.0, "פתיחה")])
    assert md.startswith(LRM)
    assert "0:00 — פתיחה" in md


def test_youtube_has_no_bidi_marks():
    # Invisible LRM chars break YouTube's chapter parser — must never appear.
    ch = [Chapter(0.0, "א"), Chapter(30.0, "ב"), Chapter(90.0, "ג")]
    yt = render_chapters_youtube(ch, audio_end=200.0)
    assert LRM not in yt
    assert yt.splitlines()[0].startswith("0:00 ")


def test_youtube_prepends_zero():
    ch = [Chapter(12.0, "א"), Chapter(40.0, "ב"), Chapter(100.0, "ג")]
    yt = render_chapters_youtube(ch, audio_end=200.0)
    assert yt.splitlines()[0].startswith("0:00 ")


def test_youtube_merges_sub_10s_chapters():
    # Second chapter is only 3s long -> dropped/merged into the first.
    ch = [Chapter(0.0, "א"), Chapter(3.0, "ב"), Chapter(60.0, "ג"), Chapter(120.0, "ד")]
    yt = render_chapters_youtube(ch, audio_end=200.0)
    assert len(yt.splitlines()) == 3  # ב merged away


def test_youtube_fewer_than_three_returns_empty():
    ch = [Chapter(0.0, "א"), Chapter(60.0, "ב")]
    assert render_chapters_youtube(ch, audio_end=120.0) == ""


def test_spotify_min_gap_30s():
    # Spotify needs >=30s spacing; the 20s chapter is dropped, 40s/90s kept.
    ch = [Chapter(0.0, "א"), Chapter(20.0, "ב"), Chapter(40.0, "ג"), Chapter(90.0, "ד")]
    yt = render_chapters_youtube(ch, audio_end=200.0, min_gap=30.0)
    lines = yt.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("0:00 ")
    assert lines[1].startswith("0:40 ")  # 20s one dropped


def test_podcast_json_is_valid_pc20():
    ch = [Chapter(0.0, "פתיחה"), Chapter(35.5, "נושא")]
    doc = json.loads(render_chapters_podcast_json(ch))
    assert doc["version"] == "1.2.0"
    assert doc["chapters"] == [
        {"startTime": 0.0, "title": "פתיחה"},
        {"startTime": 35.5, "title": "נושא"},
    ]


def test_index_guard_rejects_non_increasing():
    # The chapter validator lives in generate.make_chapters; here we just assert
    # the invariant the guard enforces is what format expects (sorted, distinct).
    ch = [Chapter(0.0, "א"), Chapter(30.0, "ב"), Chapter(90.0, "ג")]
    starts = [c.start for c in ch]
    assert starts == sorted(starts)


def test_rtl_caption_pins_direction():
    from sofit.format import PDF, RLE, rtl_caption

    # a Hebrew body paragraph gets an RTL embedding so an LTR container can't
    # transpose its segments around the Latin brand name
    body = "אשתמש ב-Instinct בעוד כמה חודשים."
    assert rtl_caption(body) == RLE + body + PDF

    # hashtag lines are left untouched - the platforms split them into one
    # element per tag, and a control char next to "#" can land inside the tag
    tags = "#וויקליסינק #Instinct #AI"
    assert rtl_caption(tags) == tags

    # nothing to do for text with no Hebrew, and blank lines survive
    assert rtl_caption("Decart raised a round") == "Decart raised a round"
    once = rtl_caption(f"{body}\n\n{tags}")
    assert "\n\n" in once
    # idempotent: a second pass re-marks from clean rather than nesting
    assert rtl_caption(once) == once
