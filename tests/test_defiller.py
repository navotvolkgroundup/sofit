"""Filler-word detection. No ffmpeg — the cut plan and the dry-run report.

Real Hebrew words that merely look like hesitations must survive. אם ("if")
is alef + final mem; the filler אמ is alef + medial mem. אה is both a
hesitation and a real interjection, so it is cut only when it is isolated
or the transcriber was unsure.
"""

import pytest

from sofit.defiller import (
    CLEAR_FILLERS,
    DefillerConfig,
    classify_token,
    config_from_env,
    format_report,
    is_stretch,
    normalize_token,
    plan_defill,
    splice_layout,
)
from sofit.transcribe import Word


# --- stretch matching ----------------------------------------------------

def test_stretch_holds_letters_and_keeps_final_mem_distinct():
    assert is_stretch("אממממ", "אמ")
    assert is_stretch("אההה", "אהה")
    assert is_stretch("uuuuh", "uh")
    assert is_stretch("hmmmmm", "hmm")
    assert is_stretch("הממ", "הממ")          # the base itself
    # "if" is final mem (ם), not medial mem (מ).
    assert not is_stretch("אם", "אמ")
    assert not is_stretch("אמא", "אמ")
    assert not is_stretch("אהבה", "אה")
    assert not is_stretch("המון", "הממ")
    assert not is_stretch("umbrella", "um")
    assert not is_stretch("human", "uh")


def test_normalize_strips_nikud_and_edge_punctuation_but_not_final_mem():
    assert normalize_token("אממ,") == "אממ"
    assert normalize_token("UM...") == "um"
    assert normalize_token("אֵם") == "אם"     # "if" with a tsere
    assert normalize_token("אָמ") == "אמ"     # hesitation with a qamats
    assert normalize_token("אה!") == "אה"


# --- clear fillers are cut; real words are not ---------------------------

def _span(*words, dur=None):
    """words are (t, d, text) or (t, d, text, prob)."""
    out = []
    for w in words:
        item = {"t": w[0], "d": w[1], "w": w[2]}
        if len(w) > 3 and w[3] is not None:
            item["p"] = w[3]
        out.append(item)
    end = dur if dur is not None else (out[-1]["t"] + out[-1]["d"] + 0.2 if out else 0)
    return out, end


def _texts(plan):
    return [w["w"] for w in plan.words]


def test_clear_fillers_and_stretches_are_cut():
    words, dur = _span(
        (0.0, 0.40, "שלום"),
        (0.50, 0.35, "אממ"),
        (1.10, 0.30, "אמ"),
        (1.50, 0.40, "אהההה"),       # stretched אהה
        (2.10, 0.30, "אהמ"),
        (2.60, 0.35, "המממ"),        # stretched הממ
        (3.20, 0.30, "מממ"),
        (3.70, 0.25, "um"),
        (4.10, 0.40, "uuuuh"),
        (4.70, 0.35, "hmmmm"),
        (5.30, 0.40, "עולם"),
    )
    plan = plan_defill(words, dur)
    assert _texts(plan) == ["שלום", "עולם"]
    assert all(h.applied for h in plan.hits)
    assert plan.words[0]["t"] == pytest.approx(0.0, abs=0.02)
    # Everything after the first cut sits earlier than it did in the source.
    assert plan.words[-1]["t"] < 5.30
    assert plan.duration < dur


def test_real_hebrew_words_are_not_fillers():
    # Tight, ordinary gaps — fluent speech, not a hesitation sitting alone.
    tokens = ["אם", "אמא", "אהבה", "אהל", "אמן", "אמת", "אומה", "המון",
              "מה", "הם", "עם", "אמיר", "umbrella", "human", "אה"]
    words = []
    t = 0.0
    for tok in tokens:
        words.append((t, 0.30, tok))
        t += 0.34                          # 40ms gap: normal word spacing
    plan = plan_defill(*_span(*words, dur=t + 0.2))
    assert _texts(plan) == tokens
    # אה was seen and deliberately left in. אם too. Nothing was cut.
    assert plan.cuts == []
    seen = {h.text: h for h in plan.hits}
    assert seen["אה"].applied is False and seen["אה"].reason == "ambiguous"
    assert seen["אם"].applied is False and seen["אם"].reason == "ambiguous"
    # The lookalikes never even registered as filler-shaped.
    assert "אמא" not in seen and "אהבה" not in seen and "המון" not in seen


def test_im_with_nikud_in_a_sentence_stays():
    words, dur = _span(
        (0.00, 0.30, "רק"),
        (0.34, 0.25, "אֵם"),               # אם, "if"
        (0.62, 0.30, "אתה"),
        (0.96, 0.30, "רוצה"),
    )
    plan = plan_defill(words, dur)
    assert _texts(plan) == ["רק", "אֵם", "אתה", "רוצה"]


def test_short_token_cut_when_isolated_or_low_confidence():
    # אה between pauses on both sides — a standalone hesitation.
    paused, dur = _span(
        (0.0, 0.40, "תראה"),
        (1.10, 0.25, "אה"),                # 0.70s before, 0.55s after
        (1.90, 0.40, "נכון"),
        dur=2.5,
    )
    plan = plan_defill(paused, dur)
    assert _texts(plan) == ["תראה", "נכון"]
    hit = next(h for h in plan.hits if h.text == "אה")
    assert hit.applied and hit.reason == "pause"

    # אם ("if") with a low word probability is the transcriber guessing at an "אמ".
    unsure, dur = _span(
        (0.00, 0.30, "זה"),
        (0.34, 0.20, "אם", 0.20),
        (0.58, 0.30, "כלי"),
        dur=1.2,
    )
    plan = plan_defill(unsure, dur)
    assert "אם" not in _texts(plan)
    assert plan.hits[0].reason == "low_confidence"

    # High confidence and tight gaps: אם stays, even though it is short.
    sure, dur = _span(
        (0.00, 0.30, "זה"),
        (0.34, 0.20, "אם", 0.92),
        (0.58, 0.30, "כלי"),
        dur=1.2,
    )
    assert "אם" in _texts(plan_defill(sure, dur))


def test_punctuation_and_case_still_match():
    words, dur = _span(
        (0.0, 0.4, "אז"),
        (0.5, 0.3, "אממ,"),
        (1.0, 0.3, "Um"),
        (1.5, 0.4, "הלאה"),
    )
    assert _texts(plan_defill(words, dur)) == ["אז", "הלאה"]


def test_min_gap_skips_a_cut_that_would_strand_a_short_word():
    # "כן" (0.20s) sandwiched between two fillers. Cutting both leaves a
    # flash of a word between two joins. min_gap refuses that.
    words, dur = _span(
        (0.00, 1.00, "שלום"),
        (1.10, 0.30, "אממ"),
        (1.45, 0.20, "כן"),
        (1.70, 0.30, "אהה"),
        (2.20, 2.00, "עולם"),
        dur=4.5,
    )
    plan = plan_defill(words, dur, DefillerConfig(min_gap=0.40))
    assert "כן" in _texts(plan)
    applied = [h for h in plan.hits if h.applied]
    skipped = [h for h in plan.hits if not h.applied]
    assert len(applied) == 1 and len(skipped) == 1
    assert skipped[0].reason == "min_gap"
    # The kept speech between the remaining cuts is not a sub-min_gap island.
    # (One filler was dropped, so there is only one cut.)
    assert len(plan.cuts) == 1


def test_edge_filler_is_trimmed_without_a_min_gap_complaint():
    # A leading "אמ" is a trim of the head, not a jump between two cuts.
    words, dur = _span(
        (0.00, 0.30, "אמ"),
        (0.50, 2.00, "שלום"),
        dur=2.6,
    )
    plan = plan_defill(words, dur)
    assert _texts(plan) == ["שלום"]
    assert plan.hits[0].applied


def test_configurable_list_and_env(monkeypatch):
    words, dur = _span(
        (0.0, 0.4, "יש"),
        (0.5, 0.4, "אהל"),                 # "tent" — not a default filler
        (1.1, 0.4, "כאן"),
        dur=1.8,
    )
    assert "אהל" in _texts(plan_defill(words, dur))
    cfg = DefillerConfig(clear=CLEAR_FILLERS + ("אהל",))
    assert "אהל" not in _texts(plan_defill(words, dur, cfg))

    monkeypatch.setenv("SOFIT_FILLERS", "אהל")
    env_cfg = config_from_env()
    assert "אהל" in env_cfg.clear
    # The plain constructor ignores the environment, so tests stay hermetic.
    assert "אהל" not in DefillerConfig().clear
    # The env extra must not leak into a default plan.
    assert "אהל" in _texts(plan_defill(words, dur))


def test_crossfade_is_accounted_for_in_the_shifted_clock():
    words, dur = _span(
        (0.0, 0.40, "שלום"),
        (0.50, 0.40, "אממ"),
        (1.20, 0.50, "עולם"),
        dur=2.0,
    )
    cfg = DefillerConfig(padding=0.04, crossfade=0.02)
    plan = plan_defill(words, dur, cfg)
    assert len(plan.cuts) == 1
    cut = plan.cuts[0][1] - plan.cuts[0][0]
    # One join: the next word moves forward by the cut plus the overlap.
    assert plan.words[1]["w"] == "עולם"
    assert 1.20 - plan.words[1]["t"] == pytest.approx(cut + plan.crossfade, abs=0.03)
    layout, out_dur, xf = splice_layout(
        [(0.0, plan.cuts[0][0]), (plan.cuts[0][1], dur)], cfg.crossfade)
    assert out_dur == pytest.approx(plan.duration, abs=1e-6)
    assert xf == pytest.approx(plan.crossfade, abs=1e-6)
    assert layout[1][2] == pytest.approx(plan.words[1]["t"] - (1.20 - plan.cuts[0][1]), abs=0.03)


# --- dry-run report on a sample transcript --------------------------------

SAMPLE_TRANSCRIPT = [
    {"start": 12.00, "end": 12.40, "text": "אז"},
    {"start": 12.50, "end": 12.90, "text": "אממ"},
    {"start": 13.00, "end": 13.28, "text": "אם"},
    {"start": 13.32, "end": 13.60, "text": "אתה"},
    {"start": 13.64, "end": 14.00, "text": "רוצה"},
    {"start": 14.10, "end": 14.50, "text": "אהבה"},
    {"start": 15.20, "end": 15.50, "text": "אה"},       # pauses both sides
    {"start": 16.10, "end": 16.50, "text": "נכון"},
    {"start": 16.60, "end": 17.00, "text": "אמא"},
    {"start": 17.10, "end": 17.40, "text": "um"},
    {"start": 17.55, "end": 17.95, "text": "המממ"},
    {"start": 18.10, "end": 18.40, "text": "זה"},
    {"start": 18.48, "end": 18.90, "text": "המון"},
    {"start": 19.00, "end": 19.40, "text": "אומה"},
]


def test_dry_run_report_on_sample_transcript():
    clip = {"id": "clip-3", "start": 12.0, "end": 19.6, "words": [
        # clip-relative, the shape render_clips actually sees
        {"t": round(w["start"] - 12.0, 3), "d": round(w["end"] - w["start"], 3), "w": w["text"]}
        for w in SAMPLE_TRANSCRIPT
    ]}
    # And the same tokens as a raw transcript (absolute start/end/text).
    report = format_report([clip])
    assert "clip-3" in report
    assert "12.500" in report and "אממ" in report
    # אממ, the isolated אה, um, and the stretched המממ are cut.
    for token in ("אממ", "אה", "um", "המממ"):
        line = next(ln for ln in report.splitlines() if token in ln.split())
        assert line.strip().startswith("cut"), line
    # אם in "אם אתה רוצה", plus the real words, are not cut.
    for token in ("אם", "אהבה", "אמא", "המון", "אומה"):
        cut_lines = [ln for ln in report.splitlines()
                     if ln.strip().startswith("cut") and token in ln.split()]
        assert cut_lines == [], token
    assert any(ln.strip().startswith("keep") and "אם" in ln.split()
               for ln in report.splitlines())

    # Transcript-shaped words: timestamps in the report are the source times.
    from sofit.defiller import plan_defill as _plan
    plan = _plan(SAMPLE_TRANSCRIPT, 19.6)
    assert {h.text for h in plan.applied_hits} >= {"אממ", "אה", "um", "המממ"}
    assert all(h.text != "אם" or not h.applied for h in plan.hits)
    assert "אהבה" not in {h.text for h in plan.hits}


def test_classify_table():
    cfg = DefillerConfig()
    clear = ["אמ", "אממ", "אממממ", "אהה", "אהממ", "הממ", "ממ", "מממ",
             "um", "umm", "uh", "uhhh", "hmm", "hmmm"]
    short = ["אה", "אם", "hm"]
    real = ["אמא", "אהבה", "אהל", "אמן", "המון", "מה", "הם", "עם", "umbrella"]
    for tok in clear:
        assert classify_token(normalize_token(tok), cfg) == "clear", tok
    for tok in short:
        assert classify_token(normalize_token(tok), cfg) == "short", tok
    for tok in real:
        assert classify_token(normalize_token(tok), cfg) is None, tok


def test_clip_words_carry_probability_for_the_low_confidence_gate():
    from sofit.generate import _clip_words
    from sofit.transcribe import Segment
    segs = [Segment(0, 0.0, 1.0, "אם", [Word(0.1, 0.4, "אם", probability=0.31)])]
    words = _clip_words(segs, 0.0, 1.0)
    assert words == [{"t": 0.1, "d": 0.3, "w": "אם", "p": 0.31}]
    # Without a stored probability the clips.json word stays the old shape.
    bare = [Segment(0, 0.0, 1.0, "אם", [Word(0.0, 0.4, "אם")])]
    assert _clip_words(bare, 0.0, 1.0) == [{"t": 0.0, "d": 0.4, "w": "אם"}]


def test_word_probability_roundtrip_is_optional():
    # Older cached transcripts have no probability field.
    legacy = Word(**{"start": 0.0, "end": 0.4, "text": "אם"})
    assert legacy.probability is None
    fresh = Word(0.0, 0.4, "אמ", probability=0.3)
    assert fresh.probability == 0.3
