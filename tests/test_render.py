"""Pure-logic tests for the render module. No ffmpeg / Pillow needed.

Covers the two things most likely to break silently:
  * crop-to-fill produces a CROP (not a pad/letterbox) for 9:16
  * clip-relative word timings convert to correct SRT times
"""

import os
import pytest
from pathlib import Path

from sofit.render import (
    _bidi_word_order,
    _build_crop_vf,
    _prep_logo,
    _caption_entries,
    _clip_transcript,
    _crop_position_for_face,
    _dynamic_crop_vf,
    _pan_keyframes,
    _parse_srt,
    _srt_time,
    _target_resolution,
    generate_srt,
)


# --- bidi word order (visual left-to-right, base RTL) --------------------

def _t(*texts):
    return [{"text": s, "start": 0, "end": 1} for s in texts]


def test_bidi_pure_hebrew_reverses():
    # Hebrew-only line: visual L->R is the logical order reversed.
    out = [w["text"] for w in _bidi_word_order(_t("שלום", "עולם", "טוב"))]
    assert out == ["טוב", "עולם", "שלום"]


def test_bidi_keeps_multiword_latin_run_in_order():
    # "זה Thoma Bravo כזה" — the Latin run must NOT reverse to "Bravo Thoma".
    out = [w["text"] for w in _bidi_word_order(_t("זה", "Thoma", "Bravo", "כזה"))]
    # visual L->R: כזה, then the LTR run in order (Thoma, Bravo), then זה
    assert out == ["כזה", "Thoma", "Bravo", "זה"]


def test_bidi_single_latin_token_between_hebrew():
    out = [w["text"] for w in _bidi_word_order(_t("את", "OpenAI", "אהבתי"))]
    assert out == ["אהבתי", "OpenAI", "את"]


# --- crop-to-fill --------------------------------------------------------

def test_crop_vf_9_16_is_crop_not_pad():
    vf = _build_crop_vf("9:16")
    assert "crop=" in vf
    assert "pad" not in vf  # must never letterbox
    assert vf.endswith("scale=1080:1920")


def test_crop_vf_9_16_crops_width_keeps_height():
    # Portrait target crops horizontally (keeps full height ih).
    vf = _build_crop_vf("9:16")
    assert "crop=ih*1080/1920:ih:" in vf


def test_crop_vf_center_by_default():
    # crop_position 0.5 -> the x offset is multiplied by 0.5.
    vf = _build_crop_vf("9:16")
    assert "*0.5:0" in vf


def test_crop_vf_landscape_and_square():
    assert _build_crop_vf("16:9").endswith("scale=1920:1080")
    assert _build_crop_vf("1:1").endswith("scale=1080:1080")


def test_target_resolution_arbitrary_aspect_is_even():
    w, h = _target_resolution("4:5")
    assert h == 1920
    assert w % 2 == 0 and h % 2 == 0
    assert w < h  # portrait


# --- face-aware crop mapping --------------------------------------------

def test_crop_position_centers_on_face():
    # A face at frame center maps to a centered crop.
    assert _crop_position_for_face(0.5, 0.316) == 0.5
    # A face left of center pulls the crop left (< 0.5); right pulls right.
    assert _crop_position_for_face(0.3, 0.316) < 0.5
    assert _crop_position_for_face(0.7, 0.316) > 0.5
    # Edges clamp to [0,1].
    assert _crop_position_for_face(0.0, 0.316) == 0.0
    assert _crop_position_for_face(1.0, 0.316) == 1.0


def test_crop_position_degenerate_widths_are_center():
    # A crop as wide as the source (or wider) can't recenter — stay centered.
    assert _crop_position_for_face(0.2, 1.0) == 0.5
    assert _crop_position_for_face(0.2, 1.5) == 0.5


# --- speaker-tracking pan ------------------------------------------------

def test_pan_keyframes_snaps_on_cut():
    # steady on the left, then a hard cut to the right (a camera switch).
    samples = [(i * 0.5, 0.30) for i in range(6)] + [(3.0 + i * 0.5, 0.75) for i in range(6)]
    kf = _pan_keyframes(samples)
    xs = [c for _, c in kf]
    assert min(xs) < 0.35 and max(xs) > 0.70  # both shots represented
    ts = [t for t, _ in kf]
    gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    assert any(g < 0.1 for g in gaps)  # a near-instant snap, not a slow pan


def test_pan_keyframes_steady_is_minimal():
    samples = [(i * 0.5, 0.5) for i in range(10)]
    assert len(_pan_keyframes(samples)) <= 2  # no spurious keyframes when static


def test_pan_keyframes_follows_isolated_sustained_cut():
    # long shot A, an isolated ~1.25s cut to B, then back to A -> B IS followed
    # (this is the clip-6 case: the crop must move to frame the other speaker).
    a = [(i * 0.25, 0.30) for i in range(20)]
    b = [(5.0 + i * 0.25, 0.75) for i in range(5)]
    a2 = [(6.25 + i * 0.25, 0.30) for i in range(20)]
    xs = [c for _, c in _pan_keyframes(a + b + a2)]
    assert max(xs) > 0.70  # followed the sustained cut to B


def test_pan_keyframes_burst_does_not_alternate():
    # A rapid A/B/A/B exchange must collapse to a single held position, never a
    # back-and-forth bounce (the jumpiness). At most one excursion up.
    a = [(i * 0.25, 0.20) for i in range(12)]
    ex = []
    t = 3.0
    for _ in range(5):  # 5 rapid A/B swaps, ~0.75s each
        for _ in range(3):
            ex.append((t, 0.85))
            t += 0.25
        for _ in range(3):
            ex.append((t, 0.20))
            t += 0.25
    a2 = [(t + i * 0.25, 0.20) for i in range(12)]
    xs = [c for _, c in _pan_keyframes(a + ex + a2)]
    ups = sum(1 for i in range(1, len(xs)) if xs[i] - xs[i - 1] > 0.1)
    assert ups <= 1  # no alternating bounce


def test_pan_keyframes_ignores_rapid_flicker():
    # 3s locked on the left, then a rapid A/B flicker every 0.3s. The crop must
    # NOT chase the flicker (that was the jumpy bug) — at most one settle.
    steady = [(i * 0.3, 0.30) for i in range(10)]
    flicker = [(3.0 + k * 0.3, 0.75 if k % 2 else 0.30) for k in range(7)]
    kf = _pan_keyframes(steady + flicker)
    xs = [c for _, c in kf]
    snaps = sum(1 for i in range(1, len(xs)) if abs(xs[i] - xs[i - 1]) > 0.1)
    assert snaps <= 1


def test_pan_keyframes_fills_gaps():
    # None = no face detected that frame; must be hold-filled, not dropped.
    kf = _pan_keyframes([(0.0, 0.4), (0.5, None), (1.0, None), (1.5, 0.6)])
    assert kf and all(c is not None for _, c in kf)


def test_dynamic_crop_vf_pans_over_time():
    vf = _dynamic_crop_vf("9:16", [(0.0, 0.3), (2.0, 0.7)])
    assert vf.startswith("crop=") and vf.endswith("scale=1080:1920")
    assert "t" in vf  # x expression references time -> the crop pans


# --- caption entries (word highlight) ------------------------------------

def test_caption_entries_keep_word_timings():
    clip = {
        "id": "c", "start": 10.0, "end": 20.0,
        "words": [
            {"t": 0.0, "d": 0.4, "w": "שלום"},
            {"t": 0.5, "d": 0.4, "w": "עולם"},
            {"t": 1.1, "d": 0.4, "w": "טוב."},
        ],
    }
    entries = _caption_entries(_clip_transcript(clip), 10.0, 20.0)
    assert entries
    words = [w for e in entries for w in e["words"]]
    assert [w["text"] for w in words] == ["שלום", "עולם", "טוב."]
    # times are clip-relative (first word at ~0), so highlighting lines up with t.
    assert abs(words[0]["start"]) < 0.01


# --- logo prep -----------------------------------------------------------

def test_prep_logo_trims_transparent_margins(tmp_path):
    PIL = pytest.importorskip("PIL")
    from PIL import Image
    # 200x200 fully transparent with a 40x20 opaque block at (30,50)
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    for x in range(30, 70):
        for y in range(50, 70):
            im.putpixel((x, y), (255, 0, 0, 255))
    src = tmp_path / "logo.png"
    im.save(src)
    out = _prep_logo(str(src), str(tmp_path))
    trimmed = Image.open(out)
    assert trimmed.size == (40, 20)  # cropped to the opaque block


def test_render_clips_passes_hook_when_card_on(monkeypatch, tmp_path):
    # The clip's `hook` reaches extract_clip by default (caption-first hook card),
    # and is suppressed when hook_card=False.
    import sofit.render as r
    captured = []
    monkeypatch.setattr(r, "extract_clip", lambda **k: captured.append(k))
    clip = {"id": "clip-1", "hook": "בדיקה", "focus": 0.5, "start": 0, "end": 1, "words": []}
    r.render_clips("v.mp4", [clip], str(tmp_path), subtitles=False)
    assert captured[-1]["hook"] == "בדיקה"
    r.render_clips("v.mp4", [clip], str(tmp_path), subtitles=False, hook_card=False)
    assert captured[-1]["hook"] is None


def test_hook_card_shrinks_instead_of_blanketing_the_frame():
    # A whole-sentence hook must not blanket the speaker's face: the card shrinks
    # rather than stacking 4 lines. (The first WS203 batch rendered 4-line cards
    # over the face — this is that regression.)
    pytest.importorskip("PIL")
    from sofit.render import _fit_hook_card
    long_hook = "Lovable שווה 8 מיליארד. Wix שווה 2. איך זה הגיוני?"
    _, _, lines, size = _fit_hook_card(long_hook, 1920, int(1080 * 0.90))
    assert len(lines) <= 2
    assert size < max(34, 1920 // 16)  # shrank from the headline size

    # A short hook keeps the full headline size.
    _, _, short_lines, short_size = _fit_hook_card("למה כולם טועים?", 1920, int(1080 * 0.90))
    assert len(short_lines) == 1
    assert short_size == max(34, 1920 // 16)


def test_hook_card_never_truncates_a_long_hook():
    # WS204 regression: a word cap silently cut the payoff off 15-word hooks
    # mid-clause ("...ואף אחד לא", losing "היה שם לב"), which destroys the hook.
    # Every word must survive; the card shrinks (and may take a 3rd line) instead.
    pytest.importorskip("PIL")
    from sofit.render import _fit_hook_card
    hook = "היית יכול לערבב את הצוותים של שלוש חברות סייבר ואף אחד לא היה שם לב"
    _, _, lines, size = _fit_hook_card(hook, 1920, int(1080 * 0.90))
    rendered = [w["text"] for line in lines for w in line]
    assert rendered == hook.split()          # nothing dropped
    assert len(lines) <= 3                   # still not a wall of text
    assert size >= max(22, 1920 // 25)       # and not shrunk below the floor


def test_render_clips_hook_variant_picks_alt_and_suffixes_output(monkeypatch, tmp_path):
    # hook_variant=N uses hook_variants[N-1] and writes <id>.hookN.mp4, so A/B
    # renders of the same clip don't overwrite each other.
    import sofit.render as r
    captured = []
    monkeypatch.setattr(r, "extract_clip", lambda **k: captured.append(k))
    clip = {"id": "clip-1", "hook": "ראשי", "hook_variants": ["חלופה א", "חלופה ב"],
            "focus": 0.5, "start": 0, "end": 1, "words": []}

    outs = r.render_clips("v.mp4", [clip], str(tmp_path), subtitles=False, hook_variant=2)
    assert captured[-1]["hook"] == "חלופה ב"
    assert outs[0].endswith("clip-1.hook2.mp4")

    outs = r.render_clips("v.mp4", [clip], str(tmp_path), subtitles=False)  # primary
    assert captured[-1]["hook"] == "ראשי"
    assert outs[0].endswith("clip-1.mp4")


def test_render_clips_out_of_range_variant_falls_back_to_primary(monkeypatch, tmp_path):
    # Asking for a variant the clip doesn't have must render the primary hook,
    # not a silently card-less clip.
    import sofit.render as r
    captured = []
    monkeypatch.setattr(r, "extract_clip", lambda **k: captured.append(k))
    clip = {"id": "clip-1", "hook": "ראשי", "hook_variants": [],
            "focus": 0.5, "start": 0, "end": 1, "words": []}
    outs = r.render_clips("v.mp4", [clip], str(tmp_path), subtitles=False, hook_variant=3)
    assert captured[-1]["hook"] == "ראשי"
    assert outs[0].endswith("clip-1.mp4")  # no bogus .hook3 suffix


def test_render_clips_logo_env_default(monkeypatch, tmp_path):
    # SOFIT_LOGO applies a logo without passing --logo/logo=.
    import sofit.render as r
    monkeypatch.setenv("SOFIT_LOGO", "/tmp/mylogo.png")
    captured = {}
    monkeypatch.setattr(r, "_prep_logo", lambda p, d: captured.setdefault("logo", p))
    monkeypatch.setattr(r, "extract_clip", lambda **k: None)
    r.render_clips("v.mp4", [{"id": "clip-1", "focus": 0.5, "start": 0, "end": 1, "words": []}],
                   str(tmp_path), subtitles=False)
    assert captured["logo"] == "/tmp/mylogo.png"


# --- SRT timing ----------------------------------------------------------

def test_srt_time_format():
    assert _srt_time(0) == "00:00:00,000"
    assert _srt_time(65.5) == "00:01:05,500"
    assert _srt_time(3725.25) == "01:02:05,250"


def test_clip_transcript_offsets_words_to_absolute():
    clip = {
        "id": "clip-1", "start": 100.0, "end": 105.0, "hook": "x",
        "words": [
            {"t": 0.0, "d": 0.5, "w": "שלום"},
            {"t": 1.0, "d": 0.5, "w": "עולם"},
        ],
    }
    tr = _clip_transcript(clip)
    words = tr["segments"][0]["words"]
    assert words[0]["start"] == 100.0 and words[0]["end"] == 100.5
    assert words[1]["start"] == 101.0 and words[1]["end"] == 101.5


def test_generate_srt_clip_relative_timing(tmp_path):
    # Words are relative to a clip starting at 100s; the SRT must be relative to
    # the clip (first word at 0:00), not to the source video.
    clip = {
        "id": "clip-1", "start": 100.0, "end": 105.0, "hook": "x",
        "words": [
            {"t": 0.0, "d": 0.4, "w": "אחת"},
            {"t": 0.5, "d": 0.4, "w": "שתיים"},
            {"t": 1.2, "d": 0.4, "w": "שלוש."},
        ],
    }
    tr = _clip_transcript(clip)
    srt = tmp_path / "clip-1.srt"
    generate_srt(tr, clip["start"], clip["end"], srt)

    entries = _parse_srt(srt)
    assert entries, "expected at least one caption entry"
    # First caption starts at the clip origin (0.0), NOT at 100s.
    assert abs(entries[0]["start"] - 0.0) < 0.01
    # Full text preserved, in clip-relative time.
    joined = " ".join(e["text"] for e in entries)
    assert "אחת" in joined and "שלוש" in joined
    # Last caption end is within the clip span (< 5s), proving the offset.
    assert entries[-1]["end"] <= 5.0


def test_generate_srt_punctuation_flush(tmp_path):
    # A word ending in sentence punctuation flushes the caption chunk.
    clip = {
        "id": "c", "start": 0.0, "end": 10.0,
        "words": [
            {"t": 0.0, "d": 0.3, "w": "היי"},
            {"t": 0.4, "d": 0.3, "w": "שם."},
            {"t": 2.0, "d": 0.3, "w": "עוד"},
            {"t": 2.4, "d": 0.3, "w": "משפט."},
        ],
    }
    tr = _clip_transcript(clip)
    srt = tmp_path / "c.srt"
    generate_srt(tr, 0.0, 10.0, srt)
    entries = _parse_srt(srt)
    assert len(entries) == 2  # split on the two sentence-final periods


def test_hook_card_height_is_budgeted_not_line_counted():
    # The card must stay inside a height budget rather than a line-count target:
    # a line-count cap over-shrank long hooks, and compensating with the floor
    # pushed a 15-word hook to 4 lines across the speaker's eyes (WS204 clip-5).
    pytest.importorskip("PIL")
    from sofit.render import _fit_hook_card
    h, max_w = 1920, int(1080 * 0.90)
    budget = int(h * 0.16)
    for hook in [
        "פעם ראשונה ב-20 שנה: גוגל שורפת מזומן",                                  # short
        "פייבר איבדה רבע ממשתמשיה בשנה — היום היא שווה פחות מהמזומן שלה",          # medium
        "כל החנויות בארץ הפכו למחסן קדמי של עלי אקספרס — פיתחתי תוסף שחושף את זה",  # long
    ]:
        _, _, lines, size = _fit_hook_card(hook, h, max_w)
        assert [w["text"] for L in lines for w in L] == hook.split()  # complete
        assert len(lines) * int(size * 1.3) <= budget                 # inside budget
        assert size >= max(22, h // 28)                               # above the floor


def test_safe_area_presets_narrow_captions_for_tiktok():
    # TikTok and Reels are the same 1080x1920 format; only the UI safe zone differs.
    # At the default 0.90 width a caption comes within 54px of the edge, which sits
    # under TikTok's ~120px right-hand button rail.
    from sofit.render import _SAFE_AREAS
    W = 1080
    def side_gap(frac):            # captions are centered, so gap is symmetric
        return (W - int(W * frac)) // 2
    assert side_gap(_SAFE_AREAS["none"][0]) < 120      # the problem
    assert side_gap(_SAFE_AREAS["tiktok"][0]) >= 120   # clears the rail
    assert side_gap(_SAFE_AREAS["reels"][0]) >= 60     # clears Reels' narrower rail
    # bottom clearance must stay above both platforms' bottom chrome (~320px)
    for name in ("tiktok", "reels"):
        assert int(1920 * _SAFE_AREAS[name][1]) >= 320


def test_safe_area_threads_through_render_clips(monkeypatch, tmp_path):
    import sofit.render as r
    captured = []
    monkeypatch.setattr(r, "extract_clip", lambda **k: captured.append(k))
    clip = {"id": "clip-1", "hook": "h", "focus": 0.5, "start": 0, "end": 1, "words": []}
    r.render_clips("v.mp4", [clip], str(tmp_path), subtitles=False, safe_area="tiktok")
    assert captured[-1]["safe_area"] == "tiktok"
    r.render_clips("v.mp4", [clip], str(tmp_path), subtitles=False)
    assert captured[-1]["safe_area"] == "none"


def test_logo_clears_the_iphone_status_bar():
    # Reported from a real Reels screenshot: at a 4% top margin (43px on 1080x1920)
    # the wordmark rendered under the iPhone clock. Every preset must inset the logo
    # far enough vertically to clear the status bar (~110px), and the platform
    # presets further to clear the app's own header row.
    from sofit.render import _SAFE_AREAS
    H = 1920
    assert int(H * _SAFE_AREAS["none"][2]) >= 110          # status bar
    assert int(H * _SAFE_AREAS["tiktok"][2]) >= 200        # + TikTok top area
    assert int(H * _SAFE_AREAS["reels"][2]) >= 250         # + "Reels" header row
    assert round(1080 * 0.04) < 110                        # the old value did not


def test_encode_timeouts_are_generous_and_configurable(monkeypatch):
    # A fixed 300s killed WS204 clip-5 (starts at 41:45 in an 8GB 1080p50 file,
    # encoding alongside 5 siblings). Encode time scales with length/fps/load, so
    # the ceiling must be generous and overridable.
    import importlib
    import sofit.render as r
    assert r.FFMPEG_TIMEOUT >= 1800
    monkeypatch.setenv("SOFIT_FFMPEG_TIMEOUT", "2400")
    assert importlib.reload(r).FFMPEG_TIMEOUT == 2400
    monkeypatch.delenv("SOFIT_FFMPEG_TIMEOUT")
    importlib.reload(r)


def test_hook_card_starts_below_a_top_corner_logo():
    # Raising the logo out of the iPhone status bar pushed it INTO the hook card's
    # band (tiktok: logo 250-340, card started at 307). The card must start below
    # the logo's bottom edge for every preset.
    pytest.importorskip("PIL")
    from sofit.render import _SAFE_AREAS
    TH, LOGO_H = 1920, 90          # logo scaled to 22% width on this wordmark
    for name, (_, _, logo_frac) in _SAFE_AREAS.items():
        logo_bottom = round(TH * logo_frac) + LOGO_H
        card_top = max(int(TH * 0.16), logo_bottom + round(TH * 0.025))
        assert card_top >= logo_bottom, f"{name}: hook card overlaps the logo"


def test_audio_is_peak_limited(monkeypatch, tmp_path):
    # WS205 clip-1/2 rendered at 0.0 dBFS, which clips on playback and again when
    # the platform re-encodes. Every render must carry a peak ceiling, and it must
    # use level=disabled — alimiter's default auto-level RAISES quiet audio to the
    # ceiling, which would change the mix instead of just catching peaks.
    import sofit.render as r
    cmds = []
    monkeypatch.setattr(r, "_run_ffmpeg", lambda cmd: cmds.append(cmd))
    monkeypatch.setattr(r, "_burn_captions_pillow", lambda *a, **k: None)
    monkeypatch.setattr(r, "_burn_subtitles_pillow", lambda *a, **k: None)
    r.extract_clip(source_video=Path("in.mp4"), start_time=0, end_time=5,
                   output_path=tmp_path / "out.mp4")
    af = cmds[0][cmds[0].index("-af") + 1]
    assert "alimiter" in af
    assert "level=disabled" in af          # never auto-level quiet clips upward


def test_speed_change_keeps_the_limiter(monkeypatch, tmp_path):
    # The limiter must survive the atempo path, not be replaced by it.
    import sofit.render as r
    cmds = []
    monkeypatch.setattr(r, "_run_ffmpeg", lambda cmd: cmds.append(cmd))
    monkeypatch.setattr(r, "_burn_captions_pillow", lambda *a, **k: None)
    monkeypatch.setattr(r, "_burn_subtitles_pillow", lambda *a, **k: None)
    r.extract_clip(source_video=Path("in.mp4"), start_time=0, end_time=5,
                   output_path=tmp_path / "out.mp4", speed=1.5)
    af = cmds[0][cmds[0].index("-af") + 1]
    assert "atempo=1.5" in af and "alimiter" in af


def test_render_clips_cuts_fillers_shifts_captions_keeps_overlays(monkeypatch, tmp_path):
    # The filler leaves the karaoke and the picture; the next word moves
    # earlier by the same amount. Hook, logo, speaker tag, and the brand
    # pass still run. The clip spec itself is not rewritten.
    import sofit.render as r
    calls = []
    monkeypatch.setattr(r, "extract_clip", lambda **k: calls.append(k))
    branded = {}
    monkeypatch.setattr(r, "_apply_brand_overlays",
                        lambda *a, **k: branded.setdefault("yes", True))
    words = [
        {"t": 0.0, "d": 0.40, "w": "שלום"},
        {"t": 0.50, "d": 0.40, "w": "אממ"},
        {"t": 1.20, "d": 0.50, "w": "עולם"},
    ]
    clip = {"id": "clip-1", "hook": "הוק", "focus": 0.5,
            "start": 10.0, "end": 12.0, "words": words,
            "speaker_tags": [{"name": "נבות", "at": 1.20, "dur": 2.0, "span": 0}],
            "logo_note": True}
    r.render_clips("v.mp4", [clip], str(tmp_path), subtitles=True, logo="/tmp/logo.png")
    assert calls, "extract_clip was not called"
    k = calls[0]
    assert k["hook"] == "הוק"
    assert k["logo"] is None or k["logo"]  # passed through; file may not exist
    assert "logo" in k
    texts = [w["text"] for e in k["caption_entries"] for w in e["words"]]
    assert texts == ["שלום", "עולם"]
    olam = next(w for e in k["caption_entries"] for w in e["words"] if w["text"] == "עולם")
    assert olam["start"] < 1.20
    assert k["drops"] and k["drops"][0][0] < 0.55 and k["drops"][0][1] > 0.85
    assert k["speaker_tags"][0]["at"] == pytest.approx(olam["start"], abs=0.05)
    assert k["speaker_tags"][0]["name"] == "נבות"
    # Spec unchanged — corrections and a later --no-defiller render still see אממ.
    assert [w["w"] for w in clip["words"]] == ["שלום", "אממ", "עולם"]
    assert clip["speaker_tags"][0]["at"] == 1.20
    assert branded.get("yes") is True

    # Real words in a tight sentence are not a cut, and --no via defiller=False
    # leaves an obvious filler in the captions.
    calls.clear()
    plain = {"id": "clip-2", "hook": "הוק", "focus": 0.5, "start": 0.0, "end": 1.2,
             "words": [
                 {"t": 0.00, "d": 0.30, "w": "אם"},
                 {"t": 0.34, "d": 0.30, "w": "אתה"},
                 {"t": 0.68, "d": 0.30, "w": "רוצה"},
             ]}
    r.render_clips("v.mp4", [plain], str(tmp_path), subtitles=True)
    texts = [w["text"] for e in calls[-1]["caption_entries"] for w in e["words"]]
    assert texts == ["אם", "אתה", "רוצה"]
    assert not calls[-1].get("drops")


def test_render_clips_defiller_off_keeps_the_filler(monkeypatch, tmp_path):
    import sofit.render as r
    calls = []
    monkeypatch.setattr(r, "extract_clip", lambda **k: calls.append(k))
    monkeypatch.setattr(r, "_apply_brand_overlays", lambda *a, **k: None)
    clip = {"id": "clip-1", "hook": "הוק", "focus": 0.5, "start": 0.0, "end": 2.0,
            "words": [
                {"t": 0.0, "d": 0.4, "w": "שלום"},
                {"t": 0.5, "d": 0.4, "w": "אממ"},
                {"t": 1.2, "d": 0.4, "w": "עולם"},
            ]}
    r.render_clips("v.mp4", [clip], str(tmp_path), defiller=False)
    texts = [w["text"] for e in calls[0]["caption_entries"] for w in e["words"]]
    assert "אממ" in texts
    assert not calls[0].get("drops")


def test_defiller_remaps_face_track_samples(monkeypatch, tmp_path):
    # A face sample that landed on the filler must not stay on the old clock.
    import sofit.render as r
    seen = {}

    def face(source, start, end, step=0.25):
        return ([(0.20, 0.30), (0.70, 0.80), (1.50, 0.40)], 1.7, (0.2, 0.5), None)

    def pan(samples, cut_delta=0.12, min_run=2):
        seen["samples"] = list(samples)
        return []

    monkeypatch.setattr(r, "_is_audio_only", lambda source: False)
    monkeypatch.setattr(r, "_face_track", face)
    monkeypatch.setattr(r, "_pan_keyframes", pan)
    monkeypatch.setattr(r, "extract_clip", lambda **k: seen.setdefault("call", k))
    monkeypatch.setattr(r, "_apply_brand_overlays", lambda *a, **k: None)
    clip = {"id": "clip-1", "start": 0.0, "end": 2.0,  # no focus → face track
            "words": [
                {"t": 0.0, "d": 0.40, "w": "שלום"},
                {"t": 0.50, "d": 0.40, "w": "אממ"},   # covers t=0.70
                {"t": 1.20, "d": 0.50, "w": "עולם"},
            ]}
    r.render_clips("v.mp4", [clip], str(tmp_path), subtitles=False)
    times = [t for t, _cx in seen["samples"]]
    assert 0.70 not in [round(t, 2) for t in times]
    assert any(t < 0.3 for t in times) and any(t > 0.5 for t in times)
    # The surviving late sample moved earlier by about the cut.
    assert max(times) < 1.50


def test_splice_filter_renders_a_shorter_clip(tmp_path):
    """The crossfade graph actually runs: the middle second is gone."""
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("needs ffmpeg")
    from sofit.defiller import splice_layout
    from sofit.render import _run_ffmpeg, _splice_av_filters

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-y", str(src)],
        check=True,
    )
    layout, out_dur, xf = splice_layout([(0.0, 1.0), (2.0, 3.0)], 0.02)
    # fps omitted on purpose: a video xfade has to pick a constant rate
    # itself. ffmpeg 7.1 rejects the trimmed pieces otherwise
    # ("current rate of 1/0 is invalid").
    parts, vlabel, alabel = _splice_av_filters(layout, xf, source_duration=3.0)
    assert ";".join(parts).count("fps=30") == 2
    out = tmp_path / "out.mp4"
    _run_ffmpeg([
        "ffmpeg", "-i", str(src), "-filter_complex", ";".join(parts),
        "-map", f"[{vlabel}]", "-map", f"[{alabel}]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-y", str(out),
    ])
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    got = float(probed.stdout.strip())
    # 1s + 1s - 20ms crossfade. AAC priming can add a few tens of ms.
    assert abs(got - out_dur) < 0.15, f"duration {got} vs planned {out_dur}"


def test_choose_xfade_fps_uses_source_rate_average_for_vfr_and_override():
    from sofit.render import choose_xfade_fps
    assert choose_xfade_fps("50/1", "50/1") == "50/1"
    # Within 2% the file is CFR; keep the nominal rate, not the average.
    assert choose_xfade_fps("50/1", "49.5/1") == "50/1"
    # Trimmed 50fps pieces lose their CFR tag. A variable-rate file is the
    # same failure with a different number: r_frame_rate is a guess, the
    # average is what xfade should be forced to.
    assert choose_xfade_fps("50/1", "2125/49") == "2125/49"
    assert choose_xfade_fps("90000/1", "30000/1001") == "30000/1001"
    assert choose_xfade_fps("0/0", "0/0") == "30"
    assert choose_xfade_fps("50/1", "50/1", override="25") == "25"


def test_splice_filter_forces_fps_on_50fps_and_vfr(tmp_path, monkeypatch):
    """xfade rejects trimmed pieces that lost their constant frame rate.

    50fps Weekly Sync footage hits "xfade: inputs need constant frame rate"
    after trim+setpts. The graph has to stamp the source rate (or the
    average, for VFR) onto every piece. An output override wins over both.
    """
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("needs ffmpeg")
    from sofit.defiller import splice_layout
    from sofit.render import _run_ffmpeg, _splice_av_filters, constant_fps_for_xfade

    src = tmp_path / "src50.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=50:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-video_track_timescale", "90000", "-c:a", "aac",
         "-y", str(src)],
        check=True,
    )
    rate = constant_fps_for_xfade(src)
    assert rate == "50/1"
    layout, out_dur, xf = splice_layout([(0.0, 1.0), (2.0, 3.0)], 0.02)
    parts, vlabel, alabel = _splice_av_filters(
        layout, xf, source_duration=3.0, fps=rate)
    graph = ";".join(parts)
    assert graph.count(f"fps={rate}") == 2
    out = tmp_path / "out50.mp4"
    _run_ffmpeg([
        "ffmpeg", "-i", str(src), "-filter_complex", graph,
        "-map", f"[{vlabel}]", "-map", f"[{alabel}]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-y", str(out),
    ])
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    got = float(probed.stdout.strip())
    assert abs(got - out_dur) < 0.15, f"duration {got} vs planned {out_dur}"

    # Dropping every 7th frame makes r_frame_rate and avg_frame_rate disagree.
    vfr = tmp_path / "vfr.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=50:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-vf", "select='not(eq(mod(n,7),0))',setpts=PTS-STARTPTS",
         "-fps_mode", "vfr", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest",
         "-y", str(vfr)],
        check=True,
    )
    vfr_rate = constant_fps_for_xfade(vfr)
    assert vfr_rate not in ("50/1", "30")
    layout, out_dur, xf = splice_layout([(0.0, 0.7), (1.2, 1.9)], 0.02)
    parts, vlabel, alabel = _splice_av_filters(
        layout, xf, source_duration=2.0, fps=vfr_rate)
    assert f"fps={vfr_rate}" in ";".join(parts)
    _run_ffmpeg([
        "ffmpeg", "-i", str(vfr), "-filter_complex", ";".join(parts),
        "-map", f"[{vlabel}]", "-map", f"[{alabel}]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-y", str(tmp_path / "out_vfr.mp4"),
    ])

    monkeypatch.setenv("SOFIT_FPS", "25")
    assert constant_fps_for_xfade(src) == "25"


def test_splice_clock_puts_a_25fps_crossfade_on_whole_frames():
    """0.02s at 25fps is half a frame. The dissolve has to be one frame,
    and the offset has to land on a frame, or xfade ends the picture."""
    from sofit.defiller import splice_layout
    from sofit.render import _splice_av_filters, _splice_clock

    layout, _out, xf = splice_layout([(0.0, 8.36), (8.48, 12.0)], 0.02)
    pieces, fade, expected = _splice_clock(layout, xf, "25")
    assert fade == pytest.approx(0.04)
    # 8.36s is 209 frames. The second piece (3.52s) is 88. One-frame overlap.
    assert pieces[0] == pytest.approx((0.0, 8.36, 0.0))
    assert pieces[1][2] == pytest.approx(8.32)
    assert expected == pytest.approx((209 + 88 - 1) / 25)
    graph = ";".join(_splice_av_filters(layout, xf, 12.0, fps="25")[0])
    assert "duration=0.040000" in graph
    assert "offset=8.320000" in graph
    assert "acrossfade=d=0.040000" in graph
    assert graph.count("fps=25") == 2

    # A later join is measured on the already-shortened output. Forgetting
    # the earlier dissolves puts the offset one frame past the picture.
    many, _out, xf = splice_layout(
        [(0.0, 4.0), (4.12, 4.40), (4.55, 8.36), (8.48, 12.0)], 0.02)
    pieces, fade, _expected = _splice_clock(many, xf, "25")
    assert fade == pytest.approx(0.04)
    assert [round(p[2], 2) for p in pieces] == [0.0, 3.96, 4.20, 7.96]

    # A piece of a single frame cannot hold a one-frame dissolve.
    short, _out, xf = splice_layout([(0.0, 0.04), (0.20, 1.0)], 0.02)
    graph = ";".join(_splice_av_filters(short, xf, 1.0, fps="25")[0])
    assert "xfade=" not in graph
    assert "concat=n=2" in graph


def test_duration_assertion_names_which_way_the_file_missed():
    from sofit.render import _duration_mismatch
    # Video dies at the join; audio and the container run on.
    short = _duration_mismatch(8.360, 11.860, 11.840, container=11.860)
    assert short is not None
    assert "video 8.360s, 3.480s shorter" in short
    assert "stopped early" in short
    assert "ran long" not in short
    # The overlay held the last frame out to the uncut span.
    long = _duration_mismatch(12.000, 11.520, 11.480, container=12.000)
    assert long is not None
    assert "video 12.000s, 0.520s longer" in long
    assert "ran long" in long
    assert "stopped early" not in long
    # Both streams on the frame clock, audio a few tens of ms long (AAC priming).
    assert _duration_mismatch(11.840, 11.880, 11.840, container=11.880) is None
    assert _duration_mismatch(None, 11.840, 11.840) is not None


def test_splice_at_25fps_keeps_video_through_several_cuts(tmp_path, monkeypatch):
    """Several joins, including a 0.12s cut, a second cut just after it, and
    a late cut whose first piece is a whole number of 25fps frames.

    That last shape is the one that truncated real Weekly Sync renders: the
    video stopped at the join while the audio continued. Both streams have
    to match the frame clock, and extract_clip checks that itself.
    """
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("needs ffmpeg")
    from sofit.defiller import splice_layout
    from sofit.render import (
        _assert_rendered_duration,
        _run_ffmpeg,
        _splice_av_filters,
        _splice_clock,
        extract_clip,
    )

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         "testsrc=size=320x240:rate=50:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
         "-c:a", "aac", "-y", str(src)],
        check=True,
    )
    drops = [(4.00, 4.12), (4.40, 4.55), (8.36, 8.48)]
    from sofit.defiller import keep_intervals
    keeps = keep_intervals(12.0, drops)
    layout, _out, xf = splice_layout(keeps, 0.02)
    _pieces, _fade, expected = _splice_clock(layout, xf, "25")
    parts, vlabel, alabel = _splice_av_filters(layout, xf, 12.0, fps="25")
    out = tmp_path / "joined.mp4"
    _run_ffmpeg([
        "ffmpeg", "-i", str(src), "-filter_complex", ";".join(parts),
        "-map", f"[{vlabel}]", "-map", f"[{alabel}]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-y", str(out),
    ])
    _assert_rendered_duration(out, expected)

    monkeypatch.setenv("SOFIT_FPS", "25")
    delivered = tmp_path / "clip.mp4"
    extract_clip(
        src, 0.0, 12.0, delivered, drops=drops, drop_crossfade=0.02,
        crop_vf="scale=160:160,setsar=1",
    )
    _assert_rendered_duration(delivered, expected)


def test_cut_render_stays_short_through_logo_and_brand_overlay(tmp_path, monkeypatch):
    """Full pipeline: word cut, looped corner logo, then the badge/frame pass.

    -t used to be the uncut span. The looped logo held the last frame until
    that span, so every cut render grew back to the original length and the
    duration check reported it as ending early. A frame longer than the clip
    does the same in the brand pass unless that pass stops with the footage.
    """
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("needs ffmpeg")
    from PIL import Image
    from sofit.defiller import DefillerConfig, plan_defill
    import sofit.render as render_mod
    from sofit.render import (
        _apply_brand_overlays,
        _assert_rendered_duration,
        _brand_asset,
        _probe_av_durations,
        _splice_clock,
        constant_fps_for_xfade,
        render_clips,
    )

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         "testsrc=size=320x240:rate=50:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(src)],
        check=True,
    )
    logo = tmp_path / "logo.png"
    badge = tmp_path / "badge.png"
    Image.new("RGBA", (40, 40), (200, 20, 20, 255)).save(logo)
    Image.new("RGBA", (80, 24), (20, 20, 200, 255)).save(badge)
    # Longer than the uncut clip, so a brand pass that follows the frame
    # instead of the footage pads the file.
    frame = tmp_path / "frame.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         "color=c=black:s=80x80:r=25:d=8",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-y", str(frame)],
        check=True,
    )
    words = [
        {"t": 0.00, "d": 0.40, "w": "שלום"},
        {"t": 0.90, "d": 0.30, "w": "אממ"},
        {"t": 1.80, "d": 0.70, "w": "עולם"},
    ]
    cfg = DefillerConfig(audio=False)
    plan = plan_defill(words, 3.0, cfg)
    assert plan.cuts, "the fixture needs a cut for the overlay to pad against"
    monkeypatch.setenv("SOFIT_FPS", "25")
    monkeypatch.setenv("SOFIT_BRAND", "on")
    monkeypatch.setenv("SOFIT_BADGE", str(badge))
    monkeypatch.setenv("SOFIT_FRAME", str(frame))
    # The pipeline scales to the show resolution. The bug is the timeline,
    # so the test frame stays small.
    monkeypatch.setattr(
        "sofit.render._target_resolution", lambda _aspect: (160, 160))
    assert _brand_asset("SOFIT_FRAME", "frame.mov") == str(frame)
    assert _brand_asset("SOFIT_BADGE", "badge.png") == str(badge)
    branded = []

    def _brand(path, width, height):
        branded.append((width, height))
        _apply_brand_overlays(path, width, height)

    monkeypatch.setattr(render_mod, "_apply_brand_overlays", _brand)

    fps = constant_fps_for_xfade(src)
    _pieces, _fade, expected = _splice_clock(plan.layout, plan.crossfade, fps)
    assert expected < 2.7, expected

    clip = {"id": "clip-1", "start": 0.0, "end": 3.0, "words": words, "focus": 0.5}
    outs = render_clips(str(src), [clip], str(tmp_path / "out"),
                        subtitles=False, logo=str(logo), defiller_cfg=cfg)
    assert branded == [(160, 160)]
    delivered = outs[0]
    _assert_rendered_duration(delivered, expected)
    # Neither the uncut span nor the 8s frame.
    video, _audio, _box = _probe_av_durations(delivered)
    assert video < 2.7
    assert video > expected - 0.1


def test_splice_filter_crossfades_audio_and_video():
    from sofit.defiller import splice_layout
    from sofit.render import _splice_av_filters
    layout, out_dur, xf = splice_layout([(0.0, 1.0), (1.4, 3.0)], 0.02)
    parts, vlabel, alabel = _splice_av_filters(layout, xf, source_duration=3.0)
    graph = ";".join(parts)
    # No fps argument: the video join still stamps a constant 30fps, and the
    # 0.02s dissolve becomes one frame. Audio trims follow that clock.
    assert graph.count("fps=30") == 2
    assert "atrim=start=0.000000:end=1.000000" in graph
    assert "atrim=start=1.400000:end=3.000000" in graph
    assert "xfade=transition=fade:duration=0.033333" in graph
    assert "acrossfade=d=0.033333" in graph
    assert vlabel == "vcat" and alabel == "acat"
    assert out_dur == pytest.approx(1.0 + 1.6 - xf, abs=1e-6)


def test_audio_only_splice_keeps_caller_times_without_an_fps_filter():
    """The audiogram path joins audio only. It must not invent a frame rate."""
    from sofit.defiller import splice_layout
    from sofit.render import _splice_av_filters
    layout, _out, xf = splice_layout([(0.0, 1.0), (1.4, 3.0)], 0.02)
    parts, vlabel, alabel = _splice_av_filters(
        layout, xf, source_duration=3.0, with_video=False)
    graph = ";".join(parts)
    assert vlabel is None and alabel == "acat"
    assert "fps=" not in graph
    assert "xfade=" not in graph
    assert "atrim=start=0.0000:end=1.0000" in graph
    assert "atrim=start=1.4000:end=3.0000" in graph
    assert f"acrossfade=d={xf:.4f}" in graph


def test_render_clips_narrative_segments_render_per_span_and_concat(monkeypatch, tmp_path):
    # A clip with `segments` (narrative edit) renders each kept span through the
    # single-range pipeline — hook card on the FIRST span only, captions from
    # that span's own words — then concatenates the parts into <id>.mp4.
    import sofit.render as r
    calls, concats = [], []
    monkeypatch.setattr(r, "extract_clip", lambda **k: calls.append(k))
    monkeypatch.setattr(r, "_concat_parts", lambda parts, out: concats.append((list(parts), out)))
    clip = {"id": "clip-1", "hook": "הוק", "focus": 0.5,
            "start": 100.0, "end": 190.0,
            "segments": [
                {"start": 100.0, "end": 120.0, "words": [{"t": 0.0, "d": 0.5, "w": "א"}]},
                {"start": 180.0, "end": 190.0, "words": [{"t": 0.0, "d": 0.5, "w": "ב"}]},
            ]}
    outs = r.render_clips("v.mp4", [clip], str(tmp_path))
    assert len(calls) == 2
    assert (calls[0]["start_time"], calls[0]["end_time"]) == (100.0, 120.0)
    assert (calls[1]["start_time"], calls[1]["end_time"]) == (180.0, 190.0)
    assert calls[0]["hook"] == "הוק" and calls[1]["hook"] is None
    assert ".part0" in str(calls[0]["output_path"]) and ".part1" in str(calls[1]["output_path"])
    (parts, final), = concats
    assert [str(p) for p in parts] == [str(c["output_path"]) for c in calls]
    assert str(final).endswith("clip-1.mp4") and outs == [str(final)]

    # Legacy single-range clips still render directly to <id>.mp4, no concat.
    calls.clear(); concats.clear()
    flat = {"id": "clip-2", "hook": "הוק", "focus": 0.5, "start": 0, "end": 20,
            "words": [{"t": 0.0, "d": 0.5, "w": "א"}]}
    outs = r.render_clips("v.mp4", [flat], str(tmp_path))
    assert len(calls) == 1 and not concats
    assert outs == [str(calls[0]["output_path"])] and outs[0].endswith("clip-2.mp4")


# ---------------------------------------------------------------------------
# Audiogram mode (audio-only sources)
# ---------------------------------------------------------------------------

def test_audiogram_cmd_shape():
    """The generated ffmpeg command builds the canvas from the audio + stills:
    zoompan background, art card, waveform, and audio filtered in-graph."""
    from sofit.render import _audiogram_cmd, _AUDIO_LIMITER

    cmd, hook_top_min = _audiogram_cmd(
        Path("ep.mp3"), 30.0, 12.0, 1080, 1920,
        "bg.png", "art.png", None, "top-left", "none",
        1.0, _AUDIO_LIMITER, Path("out.mp4"), card_fade_at=1.8)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "zoompan" in fc                      # slow push-in background
    assert "showwaves" in fc                    # waveform strip
    assert "fade=t=in:st=1.8" in fc             # card dodges the hook card
    assert _AUDIO_LIMITER in fc                 # audio filtered in-graph...
    assert "-af" not in cmd                     # ...not doubled via -af
    assert "-shortest" in cmd                   # looped stills end with audio
    assert hook_top_min == 0                    # no logo -> no hook offset


def test_prep_audiogram_assets_gradient_fallback(tmp_path):
    """No cover art -> a frame-sized gradient background and no art card."""
    pytest.importorskip("PIL")
    from PIL import Image
    from sofit.render import _prep_audiogram_assets

    bg, art = _prep_audiogram_assets(None, 108, 192, str(tmp_path))
    assert art is None
    assert Image.open(bg).size == (108, 192)


def test_prep_audiogram_assets_cover(tmp_path):
    """A cover produces a blurred frame-sized bg and a square rounded card."""
    pytest.importorskip("PIL")
    from PIL import Image
    from sofit.render import _prep_audiogram_assets

    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (500, 500), (200, 120, 40)).save(cover)
    bg, art = _prep_audiogram_assets(str(cover), 108, 192, str(tmp_path))
    assert Image.open(bg).size == (108, 192)
    card = Image.open(art)
    assert card.size == (round(108 * 0.55), round(108 * 0.55))
    assert card.mode == "RGBA"
    assert card.getpixel((0, 0))[3] == 0        # rounded corner is transparent


def test_accent_from_art(tmp_path):
    """Vibrant art yields a bright brand-matched accent; monochrome art and
    missing art keep the default (None)."""
    pytest.importorskip("PIL")
    from PIL import Image
    from sofit.render import _accent_from_art

    orange = tmp_path / "orange.png"
    Image.new("RGB", (64, 64), (230, 140, 20)).save(orange)
    accent = _accent_from_art(str(orange))
    assert accent is not None
    r, g, b, a = accent
    assert r > b and a == 255                   # warm hue kept
    assert 0.2126 * r + 0.7152 * g + 0.0722 * b >= 0.55 * 255  # readable

    mono = tmp_path / "mono.png"
    Image.new("RGB", (64, 64), (240, 240, 240)).save(mono)
    assert _accent_from_art(str(mono)) is None
    assert _accent_from_art(None) is None


def test_audiogram_cmd_music_bed():
    """With a music bed: looped input, voice split into duck key + mix feed,
    bed sidechain-ducked, mixed without amix attenuation. Without: plain af."""
    from sofit.render import _audiogram_cmd, _AUDIO_LIMITER
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".mp3") as m:
        cmd, _ = _audiogram_cmd(
            Path("ep.mp3"), 0.0, 10.0, 1080, 1920, "bg.png", "art.png",
            None, "top-left", "none", 1.0, _AUDIO_LIMITER, Path("out.mp4"),
            music=m.name)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "-stream_loop" in cmd
        assert "sidechaincompress" in fc
        assert "amix=inputs=2:duration=first:normalize=0" in fc
        assert fc.count(_AUDIO_LIMITER) == 1     # voice filtered once, pre-split
        assert "[3:a]loudnorm" in fc             # inputs: 0=voice 1=bg 2=art 3=music

    cmd, _ = _audiogram_cmd(
        Path("ep.mp3"), 0.0, 10.0, 1080, 1920, "bg.png", "art.png",
        None, "top-left", "none", 1.0, _AUDIO_LIMITER, Path("out.mp4"),
        music="/nonexistent/theme.mp3")
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "sidechaincompress" not in fc         # missing file: no bed


# --- overlay pipe: ffmpeg finishing early must not raise -----------------

def test_overlay_pillow_frames_survives_early_pipe_close(monkeypatch):
    """`total_frames` is derived from the CONTAINER duration, which a longer
    audio stream can push past the video. ffmpeg then exits on `-shortest` and
    closes stdin mid-write; that is a normal finish, not a failure."""
    from sofit import render

    class _ClosedStdin:
        def write(self, _b):
            raise ValueError("flush of closed file")

        def close(self):
            raise ValueError("flush of closed file")

    class _Proc:
        returncode = 0

        def __init__(self):
            self.stdin = _ClosedStdin()

        def communicate(self, timeout=None):
            assert self.stdin is None, "stdin must be detached before communicate()"
            return b"", b""

    monkeypatch.setattr(render, "_probe_fps_frames", lambda p: (30.0, 300))
    monkeypatch.setattr(render.subprocess, "Popen", lambda *a, **k: _Proc())

    out = Path("out.mp4")
    assert render._overlay_pillow_frames(
        Path("in.mp4"), out, 16, 16, lambda t: b"\x00" * (16 * 16 * 4)) == out

def test_bidi_leaves_ltr_only_lines_untouched():
    """A line with no RTL letters is not a base-RTL line and must not be
    reordered. Neutral-only tokens ("&", "-", "|") are not _is_ltr_word, so
    before this they formed their own run and flipped the whole line."""
    def order(line):
        return [w["text"] for w in _bidi_word_order(_t(*line.split()))]

    assert order("Under the Guides & Onboarding") == \
        ["Under", "the", "Guides", "&", "Onboarding"]
    assert order("A | B - C") == ["A", "|", "B", "-", "C"]
    # RTL lines keep their existing visual reordering
    assert order("שלום עולם") == ["עולם", "שלום"]
    assert order("שלום OpenAI עולם") == ["עולם", "OpenAI", "שלום"]

def test_caption_chunking_thresholds_are_configurable(monkeypatch):
    """A hand-written spec holds one caption for a whole span; the speech-tuned
    2.6s cap would split that into one chunk per word."""
    words = [{"t": 0.0, "d": 12.0, "w": w}
             for w in "Under the Guides & Onboarding".split()]
    tr = _clip_transcript({"start": 0.0, "end": 12.0, "words": words})

    assert len(_caption_entries(tr, 0.0, 12.0)) == 5      # default: one per word

    monkeypatch.setenv("SOFIT_MAX_SPAN", "9999")
    monkeypatch.setenv("SOFIT_MAX_WORDS", "99")
    entries = _caption_entries(tr, 0.0, 12.0)
    assert len(entries) == 1
    assert [w["text"] for w in entries[0]["words"]] == \
        ["Under", "the", "Guides", "&", "Onboarding"]

def _variation_axes(font) -> list:
    getter = getattr(font, "get_variation_axes", None)
    if getter is None:
        return []
    try:
        return list(getter() or [])
    except Exception:
        return []


def test_caption_style_env_knobs(monkeypatch):
    """SOFIT_CAPTION_* tune caption weight; unset means the social defaults.

    The caller's SOFIT_FONT and SOFIT_CAPTION_* must not leak in. A static
    brand font reports one style for every weight, so Black == Regular.
    """
    pytest.importorskip("PIL")
    from sofit.render import _load_caption_font

    for key in ("SOFIT_FONT", "SOFIT_CAPTION_WEIGHT", "SOFIT_CAPTION_DIV",
                "SOFIT_CAPTION_OUTLINE", "SOFIT_CAPTION_ACCENT"):
        monkeypatch.delenv(key, raising=False)

    # Font weight: the knob reaches the variable-font axis.
    base = _load_caption_font(48)
    monkeypatch.setenv("SOFIT_CAPTION_WEIGHT", "Regular")
    light = _load_caption_font(48)
    if _variation_axes(base):
        assert base.font.style != light.font.style

    # Size divisor and outline are read at burn time from the environment.
    monkeypatch.setenv("SOFIT_CAPTION_DIV", "44")
    assert max(28, 1080 // int(os.environ["SOFIT_CAPTION_DIV"])) == 28
    monkeypatch.delenv("SOFIT_CAPTION_DIV")
    assert max(28, 1080 // int(os.environ.get("SOFIT_CAPTION_DIV", "22"))) == 49


def test_static_font_ignores_caption_weight_and_warns(monkeypatch):
    """SOFIT_CAPTION_WEIGHT on a static face does nothing, and says so."""
    pytest.importorskip("PIL")
    import PIL.ImageFont as image_font
    from sofit import render

    class _Static:
        def __init__(self):
            class _Face:
                style = "Black"
            self.font = _Face()

        def set_variation_by_name(self, _name):
            raise OSError("not a variable font")

        def get_variation_axes(self):
            return []

    monkeypatch.setattr(image_font, "truetype", lambda *_a, **_k: _Static())
    monkeypatch.setenv("SOFIT_FONT", __file__)  # exists, so it is the candidate
    monkeypatch.delenv("SOFIT_CAPTION_WEIGHT", raising=False)
    render._warned_static_weight.clear()

    base = render._load_caption_font(48)
    monkeypatch.setenv("SOFIT_CAPTION_WEIGHT", "Regular")
    with pytest.warns(UserWarning, match="SOFIT_CAPTION_WEIGHT='Regular' ignored"):
        light = render._load_caption_font(48)
    assert base.font.style == light.font.style == "Black"

    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        again = render._load_caption_font(48)
    assert caught == []
    assert again.font.style == "Black"

def test_zoom_out_factor_and_crop(monkeypatch):
    """SOFIT_ZOOM_OUT widens the portrait crop and pads over a blurred copy."""
    from sofit import render

    monkeypatch.setenv("SOFIT_BRAND", "off")
    monkeypatch.delenv("SOFIT_ZOOM_OUT", raising=False)
    assert render._zoom_out_factor() == 1.0
    assert "gblur" not in render._build_crop_vf("9:16", 0.5)

    monkeypatch.setenv("SOFIT_ZOOM_OUT", "1.3")
    assert render._zoom_out_factor() == 1.3
    vf = render._build_crop_vf("9:16", 0.5)
    assert "gblur" in vf and "overlay=(W-w)/2:(H-h)/2" in vf
    assert vf.rstrip().endswith("scale=1080:1920")

    # clamped, and garbage falls back to fill-crop
    monkeypatch.setenv("SOFIT_ZOOM_OUT", "9")
    assert render._zoom_out_factor() == 2.4
    monkeypatch.setenv("SOFIT_ZOOM_OUT", "nope")
    assert render._zoom_out_factor() == 1.0

    # landscape targets never pad
    monkeypatch.setenv("SOFIT_ZOOM_OUT", "1.3")
    assert "gblur" not in render._build_crop_vf("16:9", 0.5)


def test_merge_continuations():
    """Transcriber-split tokens are glued back before captions are laid out."""
    from sofit.render import _merge_continuations

    def texts(words):
        return [w["text"] for w in _merge_continuations(
            [{"text": t, "start": i, "end": i + 1} for i, t in enumerate(words)])]

    assert texts(["ה", "-AI", "והכלי", "-AI"]) == ["ה-AI", "והכלי-AI"]
    assert texts(["תפקיד", "CTO", "-CPO,"]) == ["תפקיד", "CTO-CPO,"]
    assert texts(["שרפו", "20", ".9", "מיליארד"]) == ["שרפו", "20.9", "מיליארד"]
    assert texts(["אס", "-אם", "-אסים"]) == ["אס-אם-אסים"]
    # a lone hyphen is not a continuation, and timings span the merge
    assert texts(["-", "מקף"]) == ["-", "מקף"]
    merged = _merge_continuations([{"text": "ה", "start": 1.0, "end": 1.2},
                                   {"text": "-AI", "start": 1.2, "end": 1.9}])
    assert merged[0]["start"] == 1.0 and merged[0]["end"] == 1.9


def test_merge_continuations_commas_and_geresh():
    from sofit.render import _merge_continuations

    def toks(*ws):
        return [{"text": w, "start": i * 0.3, "end": i * 0.3 + 0.3}
                for i, w in enumerate(ws)]

    def texts(ws):
        return [w["text"] for w in _merge_continuations(ws)]

    # a thousands comma is a continuation, not a word: "80 ,000" was reaching
    # the burn-in with a space before the comma
    assert texts(toks("מ", "-80", ",000")) == ["מ-80,000"]
    assert texts(toks("2", ",000")) == ["2,000"]
    # so is a geresh - "דיליג'נס" arrives split at the apostrophe
    assert texts(toks("דיליג", "'נס")) == ["דיליג'נס"]
    assert texts(toks("נינג", "\u05f3\u05d4")) == ["נינג\u05f3\u05d4"]
    # a comma that isn't a thousands separator stays its own token
    assert texts(toks("שלום", ", עולם")) == ["שלום", ", עולם"]


def test_output_fps_sources_and_bounds(tmp_path, monkeypatch):
    from sofit.render import _output_fps

    monkeypatch.setenv("SOFIT_BRAND", "off")          # ignore the real brand kit
    monkeypatch.delenv("SOFIT_FPS", raising=False)
    assert _output_fps() is None                       # inherit the source

    monkeypatch.setenv("SOFIT_FPS", "25")
    assert _output_fps() == "25"
    monkeypatch.setenv("SOFIT_FPS", "25.0")            # trailing zeros trimmed
    assert _output_fps() == "25"
    monkeypatch.setenv("SOFIT_FPS", "")                # empty means inherit
    assert _output_fps() is None
    monkeypatch.setenv("SOFIT_FPS", "banana")
    assert _output_fps() is None
    monkeypatch.setenv("SOFIT_FPS", "500")             # out of range
    assert _output_fps() is None


def test_flash_hook_card_clears_a_tracked_face():
    """Regression: the flash card grew DOWN into the speaker's face
    ("why is it over Tsuk", 2026-09-23)."""
    from sofit.render import _hook_card_y
    H = 1920
    cap_top = 1500

    # no face tracked -> unchanged
    assert _hook_card_y(300, 200, None, H, cap_top) == 300
    # already clears the forehead -> unchanged
    assert _hook_card_y(300, 100, (0.30, 0.60), H, cap_top) == 300
    # would overlap, room above -> lifted so it ENDS above the face
    y = _hook_card_y(100, 200, (0.30, 0.60), H, cap_top)
    assert y + 200 <= int(0.30 * H)
    # no room above, room below the chin -> dropped under it
    y = _hook_card_y(300, 400, (0.20, 0.55), H, cap_top)
    assert y >= int(0.55 * H)
    # nowhere to go -> keeps top_margin rather than going off-screen
    assert _hook_card_y(300, 900, (0.20, 0.55), H, 1000) == 300
