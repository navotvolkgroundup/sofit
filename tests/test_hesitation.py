"""Audio-gap hesitations. Synthetic PCM only — no episode, no model.

A steady hum inside a gap is cut. A laugh, a breath, the other host, and a
silent pause are not. The cut is the hum, not the gap it sat in.
"""

import math
import random
import shutil
import struct
import wave

import pytest

from sofit.defiller import DefillerConfig, format_report, plan_defill


# read_pcm decodes through ffmpeg, which CI does not install — same guard
# the render tests use. The synthetic-PCM tests below need no decoder.
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

SR = 8000


def _sine(sr, dur, freq, amp=0.45):
    n = int(sr * dur)
    return [amp * math.sin(2 * math.pi * freq * i / sr) for i in range(n)]


def _noise(sr, dur, amp=0.25, seed=1):
    rng = random.Random(seed)
    return [rng.uniform(-amp, amp) for _ in range(int(sr * dur))]


def _silence(sr, dur, amp=0.0004, seed=2):
    rng = random.Random(seed)
    return [rng.uniform(-amp, amp) for _ in range(int(sr * dur))]


def _laugh(sr, dur, freq=180, rate=6.0, amp=0.5):
    out = []
    for i in range(int(sr * dur)):
        t = i / sr
        env = max(0.0, math.sin(2 * math.pi * rate * t)) ** 2
        out.append(amp * env * math.sin(2 * math.pi * freq * t))
    return out


def _speech(sr, dur):
    """Continuous vowels at two pitches. Flat envelope, moving f0."""
    specs = ((0.00, 0.28, 120.0), (0.28, 0.55, 240.0), (0.55, 0.90, 140.0))
    out = []
    n = int(sr * dur)
    for i in range(n):
        t = i / sr
        freq = 180.0
        for a, b, f in specs:
            if a <= t < b:
                freq = f
                break
        out.append(0.4 * math.sin(2 * math.pi * freq * i / sr))
    return out


def _put(buf, t, chunk):
    i = int(t * SR)
    buf[i:i + len(chunk)] = chunk
    return buf


def _buf(dur):
    return [0.0] * int(SR * dur)


def _plan(samples, words, dur):
    return plan_defill(words, dur, DefillerConfig(), pcm=samples, sample_rate=SR)


def _audio(plan, kind=None):
    hits = [h for h in plan.hits if h.source == "audio"]
    if kind is not None:
        hits = [h for h in hits if h.text == kind or h.reason == kind]
    return hits


def test_steady_hum_in_a_gap_is_cut_and_shifts_the_next_word():
    dur = 2.4
    samples = _buf(dur)
    _put(samples, 0.70, _sine(SR, 0.40, 200))
    words = [
        {"t": 0.00, "d": 0.30, "w": "שלום"},
        {"t": 1.70, "d": 0.40, "w": "עולם"},
    ]
    plan = _plan(samples, words, dur)
    applied = [h for h in plan.applied_hits if h.source == "audio"]
    assert len(applied) == 1, [(h.text, h.detail, h.reason) for h in plan.hits]
    hum = applied[0]
    assert hum.text == "אמ"
    assert hum.confidence >= 0.72
    assert hum.start == pytest.approx(0.70, abs=0.08)
    assert hum.end - hum.start == pytest.approx(0.40, abs=0.12)
    # The pause around the hum stays. The cut is not the whole 1.4s gap.
    assert plan.cuts[0][1] - plan.cuts[0][0] < 0.80
    assert _texts_kept(plan) == ["שלום", "עולם"]
    cut = plan.cuts[0][1] - plan.cuts[0][0]
    assert 1.70 - plan.words[1]["t"] == pytest.approx(cut + plan.crossfade, abs=0.04)


def _texts_kept(plan):
    return [w["w"] for w in plan.words]


def test_breath_laugh_speech_and_silence_are_not_cut():
    dur = 2.2
    words = [
        {"t": 0.00, "d": 0.25, "w": "שלום"},
        {"t": 1.70, "d": 0.30, "w": "עולם"},
    ]

    breath = _buf(dur)
    _put(breath, 0.60, _noise(SR, 0.50))
    plan = _plan(breath, words, dur)
    assert plan.cuts == []
    assert all(h.reason != "audio" or not h.applied for h in plan.hits)
    assert not any(h.applied for h in _audio(plan))

    laugh = _buf(dur)
    _put(laugh, 0.50, _laugh(SR, 0.80))
    plan = _plan(laugh, words, dur)
    assert not any(h.applied for h in plan.hits), [
        (h.text, h.reason, h.detail) for h in plan.hits]
    assert any(h.text == "laugh" for h in plan.hits), [
        (h.text, h.detail) for h in plan.hits]

    speech = _buf(dur)
    _put(speech, 0.45, _speech(SR, 0.90))
    plan = _plan(speech, words, dur)
    assert not any(h.applied for h in plan.hits)
    spoken = _audio(plan)
    assert spoken and spoken[0].text == "speech", [
        (h.text, h.detail) for h in plan.hits]

    pause = _silence(SR, dur)
    plan = _plan(pause, words, dur)
    assert plan.cuts == []
    assert _audio(plan) == []


def test_hum_is_found_in_a_clip_that_is_mostly_loud_speech():
    # The noise estimate used to be a percentile of every frame. Once the
    # clip was mostly vowels that percentile was speech, the threshold sat
    # above every sample, and a real episode reported no audio cuts.
    dur = 8.0
    samples = _buf(dur)
    for t in (0.0, 1.2, 2.4, 5.2, 6.4):
        _put(samples, t, _sine(SR, 0.90, 150, amp=0.50))
    _put(samples, 3.60, _sine(SR, 0.40, 200, amp=0.35))
    words = [
        {"t": 0.0, "d": 0.90, "w": "אחד"},
        {"t": 1.2, "d": 0.90, "w": "שתיים"},
        {"t": 2.4, "d": 0.90, "w": "שלוש"},
        {"t": 5.2, "d": 0.90, "w": "ארבע"},
        {"t": 6.4, "d": 0.90, "w": "חמש"},
    ]
    plan = _plan(samples, words, dur)
    hums = [h for h in plan.applied_hits if h.source == "audio"]
    assert len(hums) == 1, [(h.text, h.detail, h.start, h.end) for h in plan.hits]
    assert hums[0].start == pytest.approx(3.60, abs=0.12)
    assert plan.cuts[0][1] - plan.cuts[0][0] < 0.80


def test_long_steady_voice_is_kept():
    # Two seconds of monotone can be the other host. The cap is the hum,
    # not a multi-second stretch, even when the pitch barely moves.
    dur = 4.0
    samples = _buf(dur)
    _put(samples, 0.40, _sine(SR, 2.60, 190, amp=0.4))
    words = [
        {"t": 0.00, "d": 0.25, "w": "שלום"},
        {"t": 3.50, "d": 0.30, "w": "עולם"},
    ]
    plan = _plan(samples, words, dur)
    assert not any(h.applied for h in plan.hits)


def test_hum_beside_speech_is_kept_until_silence_separates_them():
    dur = 3.2
    words = [
        {"t": 0.00, "d": 0.20, "w": "שלום"},
        {"t": 2.80, "d": 0.25, "w": "עולם"},
    ]
    close = _buf(dur)
    _put(close, 0.40, _sine(SR, 0.35, 200))
    _put(close, 0.80, _speech(SR, 0.70))
    plan = _plan(close, words, dur)
    assert not any(h.applied and h.source == "audio" and h.text == "אמ"
                   for h in plan.hits), [(h.text, h.detail) for h in plan.hits]

    apart = _buf(dur)
    _put(apart, 0.40, _sine(SR, 0.35, 200))
    _put(apart, 1.30, _speech(SR, 0.70))
    plan = _plan(apart, words, dur)
    hums = [h for h in plan.applied_hits if h.source == "audio"]
    assert len(hums) == 1 and hums[0].text == "אמ"
    # The speech stays in the report as a keep, and the cut does not cover it.
    assert any(h.source == "audio" and h.text == "speech" and not h.applied
               for h in plan.hits)
    assert plan.cuts[0][1] < 1.10


@needs_ffmpeg
def test_dry_run_report_lists_audio_source_and_confidence(tmp_path):
    # The wav is the source file. The clip starts 1s in, which is how a
    # dry run seeks: span-relative words, absolute timestamps in the report.
    dur = 2.4
    start = 1.0
    samples = _buf(start + dur)
    _put(samples, start + 0.70, _sine(SR, 0.40, 210))
    media = tmp_path / "episode.wav"
    _write_wav(media, samples, SR)
    clip = {"id": "clip-7", "start": start, "end": start + dur, "words": [
        {"t": 0.00, "d": 0.30, "w": "שלום"},
        {"t": 1.70, "d": 0.40, "w": "עולם"},
    ]}
    report = format_report([clip], media=str(media))
    line = next(ln for ln in report.splitlines()
                if ln.strip().startswith("cut") and "audio" in ln.split())
    assert "אמ" in line.split()
    conf = float(line.split()[line.split().index("audio") + 1])
    assert conf >= 0.72
    # Absolute time of the hum, and not the whole 1.4s gap.
    abs_start = float(line.split()[1])
    abs_end = float(line.split()[3])
    assert abs_start == pytest.approx(start + 0.70, abs=0.12)
    assert abs_end - abs_start < 0.80


def _write_wav(path, samples, sr):
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        frames = bytearray()
        for sample in samples:
            frames += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
        handle.writeframes(frames)


def test_hum_on_a_word_ending_is_an_elongation_unless_opted_in():
    # The hum starts where "מה" ends: the drawn-out tail, not a separate um.
    # A hum a quarter-second later is still a hesitation and is cut.
    dur = 2.4
    words = [
        {"t": 0.00, "d": 0.30, "w": "מה"},
        {"t": 1.80, "d": 0.40, "w": "עולם"},
    ]
    tail = _buf(dur)
    _put(tail, 0.30, _sine(SR, 0.40, 200))
    plan = _plan(tail, words, dur)
    elong = [h for h in plan.hits if h.source == "audio"]
    assert elong and all(h.text == "elongation" and not h.applied for h in elong
                         if h.reason == "elongation" or h.text == "elongation")
    marked = [h for h in plan.hits if h.text == "elongation"]
    assert len(marked) == 1 and marked[0].applied is False
    assert marked[0].reason == "elongation"
    assert plan.cuts == []

    later = _buf(dur)
    _put(later, 0.55, _sine(SR, 0.40, 200))
    plan = _plan(later, words, dur)
    hums = [h for h in plan.applied_hits if h.source == "audio"]
    assert len(hums) == 1 and hums[0].text == "אמ"
    assert not any(h.text == "elongation" for h in plan.hits)

    opted = plan_defill(words, dur, DefillerConfig(elongation=True),
                        pcm=tail, sample_rate=SR)
    applied = [h for h in opted.applied_hits if h.source == "audio"]
    assert len(applied) == 1 and applied[0].text == "elongation"
    assert applied[0].reason == "elongation"


def test_min_gap_still_guards_an_audio_cut():
    # A 0.20s word between two hums. Cutting both would flash it.
    dur = 3.0
    samples = _buf(dur)
    _put(samples, 0.50, _sine(SR, 0.30, 200))
    _put(samples, 1.20, _sine(SR, 0.30, 200))
    words = [
        {"t": 0.00, "d": 0.30, "w": "שלום"},
        {"t": 0.90, "d": 0.20, "w": "כן"},
        {"t": 1.70, "d": 0.80, "w": "עולם"},
    ]
    plan = _plan(samples, words, dur)
    assert "כן" in _texts_kept(plan)
    applied = [h for h in plan.hits if h.applied]
    assert len(applied) == 1
    assert len(plan.cuts) == 1
