"""Offline tests for the generator logic — no ANTHROPIC_API_KEY needed.

We stub the Claude call to exercise the part the review flagged (and a live run
confirmed): Claude's segment indices drift over a long transcript, so chapters
are located by matching Claude's verbatim quote back to the transcript text, and
the timestamp comes from the located segment.
"""

import json

import pytest

import sofit.generate as gen
from sofit.generate import GenerationError, make_chapters
from sofit.transcribe import Segment, Word


def _seg(i, start, text):
    return Segment(index=i, start=start, end=start + 5, text=text, words=[Word(start, start + 1, text)])


SEGMENTS = [
    _seg(0, 0.0, "שלום וברוכים הבאים לפודקאסט"),
    _seg(1, 30.0, "היום נדבר על בינה מלאכותית"),
    _seg(2, 90.0, "עכשיו נעבור לנושא השני עתיד העבודה"),
]


def _stub_claude(monkeypatch, payload):
    class _Block:
        type = "text"
        text = json.dumps(payload, ensure_ascii=False)

    class _Msg:
        content = [_Block()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                return _Msg()

    monkeypatch.setattr(gen, "_client", lambda: _Client())


def test_chapters_located_by_quote(monkeypatch):
    _stub_claude(monkeypatch, [
        {"title": "פתיחה", "quote": "שלום וברוכים הבאים"},
        {"title": "עתיד העבודה", "quote": "עכשיו נעבור לנושא"},
    ])
    chapters = make_chapters(SEGMENTS)
    # timestamps come from the located segments, not any LLM-supplied number
    assert [c.start for c in chapters] == [0.0, 90.0]
    assert chapters[1].title == "עתיד העבודה"


def test_quote_matches_despite_punctuation(monkeypatch):
    # Claude quotes with different punctuation; normalized match still locates it.
    _stub_claude(monkeypatch, [{"title": "x", "quote": "היום, נדבר. על בינה"}])
    chapters = make_chapters(SEGMENTS)
    assert chapters[0].start == 30.0


def test_unlocatable_chapter_is_dropped_not_fatal(monkeypatch):
    _stub_claude(monkeypatch, [
        {"title": "real", "quote": "שלום וברוכים"},
        {"title": "ghost", "quote": "משפט שלא נאמר מעולם בכלל"},
    ])
    chapters = make_chapters(SEGMENTS)
    assert len(chapters) == 1
    assert chapters[0].title == "real"


def test_all_unlocatable_raises(monkeypatch):
    _stub_claude(monkeypatch, [{"title": "x", "quote": "טקסט מומצא לחלוטין שאיננו"}])
    with pytest.raises(GenerationError):
        make_chapters(SEGMENTS)


def test_order_enforced_by_cursor(monkeypatch):
    # Second quote points backward (seg 0); cursor has advanced past it, so it's
    # dropped rather than producing an out-of-order chapter.
    _stub_claude(monkeypatch, [
        {"title": "second", "quote": "עכשיו נעבור לנושא"},
        {"title": "backward", "quote": "שלום וברוכים"},
    ])
    chapters = make_chapters(SEGMENTS)
    assert [c.start for c in chapters] == [90.0]


def test_max_chapters_cap_enforced(monkeypatch):
    # Claude ignores "at most N" and returns all 3; code must cap to max_chapters.
    _stub_claude(monkeypatch, [
        {"title": "a", "quote": "שלום וברוכים"},
        {"title": "b", "quote": "היום נדבר"},
        {"title": "c", "quote": "עכשיו נעבור"},
    ])
    assert len(make_chapters(SEGMENTS, max_chapters=2)) == 2


def test_empty_segments_returns_empty(monkeypatch):
    _stub_claude(monkeypatch, [])
    assert make_chapters([]) == []


def test_clip_words_relative_and_fallback():
    segs = [
        Segment(0, 10.0, 12.0, "a b", [Word(10.0, 10.5, "a"), Word(11.0, 11.4, "b")]),
        Segment(1, 20.0, 22.0, "x y", []),  # no word timestamps → even distribution
    ]
    words = gen._clip_words(segs, start=10.0, end=22.0)
    # word times are relative to clip start (10.0)
    assert words[0] == {"t": 0.0, "d": 0.5, "w": "a"}
    assert words[1]["t"] == 1.0
    # fallback: "x y" spread evenly over [20,22] → t=10.0 and t=11.0 (relative)
    assert [w["w"] for w in words[2:]] == ["x", "y"]
    assert words[2]["t"] == 10.0 and words[3]["t"] == 11.0


def test_make_clips_structure(monkeypatch):
    _stub_claude(monkeypatch, [
        {"title": "רגע מעניין", "hook_type": "question", "score": 9,
         "quote_start": "שלום וברוכים", "quote_end": "היום נדבר"},
    ])
    clips = gen.make_clips(SEGMENTS)
    assert len(clips) == 1
    c = clips[0]
    assert c["id"] == "clip-1"
    assert c["hook"] == "רגע מעניין"
    assert c["focus"] is None
    assert c["start"] == 0.0
    assert c["words"][0]["t"] == 0.0  # first word at clip start


def test_make_quotes_drops_weak_and_short(monkeypatch):
    # Weak hook (score < 7) and a too-short clip must both be dropped; only the
    # strong, long-enough clip survives.
    _stub_claude(monkeypatch, [
        {"title": "חלש", "score": 4, "quote_start": "שלום וברוכים", "quote_end": "היום נדבר"},
        {"title": "קצר", "score": 9, "quote_start": "היום נדבר", "quote_end": "היום נדבר"},
        {"title": "טוב", "score": 8, "quote_start": "שלום וברוכים", "quote_end": "היום נדבר"},
    ])
    quotes = gen.make_quotes(SEGMENTS)
    assert len(quotes) == 1
    assert quotes[0].text == "טוב"


def test_resolve_clip_item_is_the_shared_contract():
    # resolve_clip_item is the single selection path (make_quotes AND the skill's
    # candidate pool call it), so pin its gates directly — they drifted once when
    # the two were parallel copies.
    segs = [
        _wseg(0, 10.0, ["אז", "למה", "כולם", "טועים"]),
        _wseg(1, 200.0, ["וזאת", "הסיבה"]),
    ]
    end_of_audio = segs[-1].end
    item = {"title": "כותרת", "score": 9, "quote_start": "למה כולם טועים",
            "quote_end": "וזאת הסיבה", "hook_variants": ["חלופה"]}

    q = gen.resolve_clip_item(item, segs, end_of_audio)
    assert q is None  # hook->payoff span exceeds max_sec: dropped, never clamped

    # A span that fits is kept, with the hook snap applied.
    near = [_wseg(0, 10.0, ["אז", "למה", "כולם", "טועים"]),
            _wseg(1, 40.0, ["וזאת", "הסיבה"])]
    q = gen.resolve_clip_item(item, near, near[-1].end)
    assert q.start == pytest.approx(10.5)          # snapped past "אז"
    assert q.end - q.start <= 45.0
    assert q.variants == ("חלופה",)

    assert gen.resolve_clip_item({**item, "score": 3}, segs, end_of_audio) is None   # weak
    assert gen.resolve_clip_item({**item, "quote_end": "למה כולם"},               # too short
                                 segs, end_of_audio) is None
    assert gen.resolve_clip_item({**item, "quote_start": "לא קיים"},              # unlocatable
                                 segs, end_of_audio) is None


def test_make_quotes_keeps_hook_variants(monkeypatch):
    # Alternates are captured, capped at 2, and the primary is never duplicated
    # into the variant list (nor are blanks).
    _stub_claude(monkeypatch, [
        {"title": "טוב", "score": 8, "quote_start": "שלום וברוכים", "quote_end": "היום נדבר",
         "hook_variants": ["גרסה א", "  ", "טוב", "גרסה ב", "גרסה ג"]},
    ])
    q = gen.make_quotes(SEGMENTS)[0]
    assert q.variants == ("גרסה א", "גרסה ב")


def test_make_quotes_variants_default_empty(monkeypatch):
    # A model that omits hook_variants must not break selection.
    _stub_claude(monkeypatch, [
        {"title": "טוב", "score": 8, "quote_start": "שלום וברוכים", "quote_end": "היום נדבר"},
    ])
    assert gen.make_quotes(SEGMENTS)[0].variants == ()


def _wseg(i, start, tokens, step=0.5):
    """Segment with one Word per token (the real transcript shape)."""
    words = [Word(start + n * step, start + n * step + 0.4, t) for n, t in enumerate(tokens)]
    return Segment(index=i, start=start, end=words[-1].end, text=" ".join(tokens), words=words)


def test_make_quotes_opens_on_the_hook_not_the_throat_clearing(monkeypatch):
    # The hook sits mid-segment, after filler. Second zero must be the hook, so
    # the clip start snaps to the hook's own first word (11.5), not the segment
    # start (10.0) — otherwise the hook lands a beat late and retention dies.
    segs = [
        _wseg(0, 10.0, ["אז", "כן", "אה", "למה", "כולם", "טועים", "בזה"]),
        _wseg(1, 40.0, ["וזאת", "הסיבה", "האמיתית"]),
    ]
    _stub_claude(monkeypatch, [
        {"title": "הוק", "score": 9, "quote_start": "למה כולם טועים",
         "quote_end": "וזאת הסיבה האמיתית"},
    ])
    assert gen.make_quotes(segs)[0].start == pytest.approx(11.5)


def test_make_quotes_keeps_start_when_hook_is_already_first(monkeypatch):
    # No filler to skip: the start must not move (the snap only goes forward).
    segs = [
        _wseg(0, 10.0, ["למה", "כולם", "טועים", "בזה"]),
        _wseg(1, 40.0, ["וזאת", "הסיבה", "האמיתית"]),
    ]
    _stub_claude(monkeypatch, [
        {"title": "הוק", "score": 9, "quote_start": "למה כולם טועים",
         "quote_end": "וזאת הסיבה האמיתית"},
    ])
    assert gen.make_quotes(segs)[0].start == pytest.approx(10.0)


def test_hook_word_start_matches_across_punctuation():
    # Matching ignores punctuation (same normalization as _locate).
    seg = _wseg(0, 5.0, ["אז,", "טוב", "—", "למה", "כולם", "טועים?"])
    assert gen._hook_word_start(seg, "למה כולם טועים") == pytest.approx(6.5)


def test_hook_word_start_none_when_absent():
    seg = _wseg(0, 5.0, ["שלום", "עולם"])
    assert gen._hook_word_start(seg, "משהו אחר לגמרי") is None


def test_make_quotes_raises_when_none_qualify(monkeypatch):
    _stub_claude(monkeypatch, [
        {"title": "חלש", "score": 3, "quote_start": "שלום וברוכים", "quote_end": "היום נדבר"},
    ])
    with pytest.raises(GenerationError):
        gen.make_quotes(SEGMENTS)


def test_titler_claude_cli_uses_cli_transport(monkeypatch):
    # titler="claude-cli" must route through _call_claude_cli, not the API client.
    import json as _json
    calls = {"api": 0, "cli": 0}

    def fake_cli(system, user, model):
        calls["cli"] += 1
        return _json.dumps([{"title": "פתיחה", "quote": "שלום וברוכים הבאים"}], ensure_ascii=False)

    def boom_api(*a, **k):
        calls["api"] += 1
        raise AssertionError("API transport must not be called for claude-cli")

    monkeypatch.setattr(gen, "_call_claude_cli", fake_cli)
    monkeypatch.setattr(gen, "_call_api", boom_api)
    chapters = make_chapters(SEGMENTS, titler="claude-cli")
    assert chapters[0].start == 0.0
    assert calls == {"api": 0, "cli": 1}


def test_claude_cli_missing_binary_raises(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(GenerationError):
        gen._call_claude_cli("sys", "user", "model")


def _perf(tmp_path, rows):
    p = tmp_path / "perf.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return str(p)


def test_performance_hint_silent_until_enough_data(tmp_path):
    # Below the row threshold the loop must say nothing — "what worked" over 3
    # posts is noise, and a confident-sounding hint would be worse than none.
    rows = [{"hook": f"h{i}", "retention": 50} for i in range(3)]
    assert gen.performance_hint(_perf(tmp_path, rows)) == ""
    assert gen.performance_hint(str(tmp_path / "nope.jsonl")) == ""


def test_performance_hint_ranks_by_retention(tmp_path):
    rows = [{"hook": f"mid{i}", "retention": 40} for i in range(6)]
    rows.append({"hook": "WINNER", "retention": 88})
    rows.append({"hook": "LOSER", "retention": 4})
    hint = gen.performance_hint(_perf(tmp_path, rows), n=1)
    held, lost = hint.split("Hooks that lost them:")
    assert "WINNER" in held and "WINNER" not in lost
    assert "LOSER" in lost and "LOSER" not in held


def test_performance_hint_survives_a_bad_line(tmp_path):
    # One corrupt line must not lose the whole log.
    p = tmp_path / "perf.jsonl"
    good = [json.dumps({"hook": f"h{i}", "views": 100 + i}) for i in range(8)]
    p.write_text("\n".join(good[:4] + ["{not json"] + good[4:]), encoding="utf-8")
    assert "REAL PERFORMANCE from 8 posted clips" in gen.performance_hint(str(p))


def test_cli_timeout_fails_fast_without_retrying(monkeypatch):
    # A `claude -p` timeout must surface immediately, not burn a second attempt on
    # the retry path (that is 2x the wall clock for a call already too slow).
    import subprocess
    monkeypatch.setattr(gen.shutil if hasattr(gen, "shutil") else subprocess, "which",
                        lambda _: "/usr/bin/claude", raising=False)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd="claude", timeout=gen.CLI_TIMEOUT)

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    with pytest.raises(TimeoutError, match="SOFIT_CLI_TIMEOUT"):
        gen.call_claude_json("sys", "usr", lambda o: o, titler="claude-cli")
    assert calls["n"] == 1  # exactly one attempt


def test_cli_timeout_is_configurable(monkeypatch):
    import importlib
    monkeypatch.setenv("SOFIT_CLI_TIMEOUT", "1234")
    assert int(importlib.reload(gen).CLI_TIMEOUT) == 1234
    monkeypatch.delenv("SOFIT_CLI_TIMEOUT")
    importlib.reload(gen)  # restore the default for the rest of the suite


def test_resolve_clip_item_drops_clips_whose_payoff_falls_outside_max_sec():
    # WS204 regression: the end used to be clamped to start+max_sec, which kept
    # the hook and threw the payoff away — clip-2 promised "Fiverr is collapsing"
    # and ended 40s before the $350M-vs-$600M-cash line. A moment whose hook and
    # payoff cannot fit in one window is not a short-form clip; drop it.
    segs = [_wseg(0, 10.0, ["למה", "כולם", "טועים"]),
            _wseg(1, 300.0, ["וזאת", "הסיבה"])]          # payoff ~5 min later
    item = {"title": "t", "score": 9, "quote_start": "למה כולם טועים",
            "quote_end": "וזאת הסיבה"}
    assert gen.resolve_clip_item(item, segs, segs[-1].end) is None


# ---------------------------------------------------------------------------
# Narrative edits (beats -> segments)
# ---------------------------------------------------------------------------

_HOOK_TOKENS = ["למה", "כולם", "טועים", "בזה", "לגמרי", "תמיד", "אבל",
                "באמת", "שווה", "להבין", "את", "זה", "לעומק", "ממש",
                "כי", "יש", "כאן", "משהו", "מעניין", "פה", "שאף", "אחד",
                "לא", "רואה", "אותו", "בכלל", "למרות", "שהוא", "מולנו",
                "כל", "הזמן", "ואי", "אפשר", "להתעלם", "ממנו", "יותר",
                "אז", "בואו", "נדבר", "עליו"]  # ~19.9s at the _wseg cadence

_PAYOFF_TOKENS = ["וזאת", "הסיבה", "האמיתית", "שכולם", "מפספסים", "אותה",
                  "כבר", "שנים", "רבות", "מאוד", "בתעשייה", "שלנו",
                  "וזה", "הלקח", "המרכזי", "שחשוב", "לזכור", "תמיד",
                  "בסוף", "היום"]  # ~9.9s


def _story_segs():
    """Hook at 10s, filler at 32s, payoff at 60s — the shape that needs an edit."""
    return [
        _wseg(0, 10.0, _HOOK_TOKENS),
        _wseg(1, 32.0, ["סתם", "פטפוט", "על", "משהו", "אחר", "לגמרי"]),
        _wseg(2, 60.0, _PAYOFF_TOKENS),
    ]


def test_resolve_clip_item_builds_a_narrative_edit_from_beats():
    segs = _story_segs()
    item = {"title": "כותרת", "score": 9, "beats": [
        {"quote_start": "למה כולם טועים", "quote_end": "נדבר עליו"},
        {"quote_start": "וזאת הסיבה האמיתית", "quote_end": "בסוף היום"},
    ]}
    q = gen.resolve_clip_item(item, segs, segs[-1].end)
    assert q is not None
    assert len(q.beats) == 2
    assert q.beats[0][0] == pytest.approx(10.0)
    assert q.beats[1][0] == pytest.approx(60.0)   # filler at 30s is not kept
    assert q.start == q.beats[0][0] and q.end == q.beats[-1][1]  # envelope
    kept = sum(e - s for s, e in q.beats)
    assert 18.0 <= kept <= 45.0


def test_resolve_clip_item_beats_gates():
    segs = _story_segs()
    base = {"title": "כותרת", "score": 9}
    # Out-of-order beats: speech is never reordered.
    assert gen.resolve_clip_item({**base, "beats": [
        {"quote_start": "וזאת הסיבה האמיתית", "quote_end": "בסוף היום"},
        {"quote_start": "למה כולם טועים", "quote_end": "נדבר עליו"},
    ]}, segs, segs[-1].end) is None
    # An unlocatable beat drops the whole clip (an untrustworthy edit).
    assert gen.resolve_clip_item({**base, "beats": [
        {"quote_start": "למה כולם טועים", "quote_end": "נדבר עליו"},
        {"quote_start": "לא קיים בכלל", "quote_end": "בסוף היום"},
    ]}, segs, segs[-1].end) is None
    # Nearly-touching beats merge into one contiguous span (no stutter cuts).
    hook_end = 10.0 + (len(_HOOK_TOKENS) - 1) * 0.5 + 0.4   # last hook word's end
    near = [_wseg(0, 10.0, _HOOK_TOKENS),
            _wseg(1, hook_end + 0.4, _PAYOFF_TOKENS)]        # 0.4s gap: a blink
    q = gen.resolve_clip_item({**base, "beats": [
        {"quote_start": "למה כולם טועים", "quote_end": "נדבר עליו"},
        {"quote_start": "וזאת הסיבה האמיתית", "quote_end": "בסוף היום"},
    ]}, near, near[-1].end)
    assert q is not None and len(q.beats) == 1


def test_clip_spec_emits_segments_only_for_multi_beat_edits():
    segs = _story_segs()
    q = gen.resolve_clip_item({"title": "כותרת", "score": 9, "beats": [
        {"quote_start": "למה כולם טועים", "quote_end": "נדבר עליו"},
        {"quote_start": "וזאת הסיבה האמיתית", "quote_end": "בסוף היום"},
    ]}, segs, segs[-1].end)
    spec = gen.clip_spec(q, segs, "clip-1")
    assert "words" not in spec and len(spec["segments"]) == 2
    for seg_spec, (bs, be) in zip(spec["segments"], q.beats):
        assert seg_spec["start"] == pytest.approx(bs, abs=0.001)
        # words are relative to the SEGMENT start, starting near zero
        assert seg_spec["words"][0]["t"] == pytest.approx(0.0, abs=0.01)

    # Single beat -> legacy flat shape, so old clips.json consumers still work.
    q1 = gen.resolve_clip_item(
        {"title": "כותרת", "score": 9,
         "quote_start": "למה כולם טועים", "quote_end": "נדבר עליו"},
        segs, segs[-1].end)
    spec1 = gen.clip_spec(q1, segs, "clip-2")
    assert "segments" not in spec1 and spec1["words"]


def test_json_image_transport_keeps_text_calls_compatible(monkeypatch, tmp_path):
    from sofit import generate as g
    image = tmp_path / 'frame.jpg'
    image.write_bytes(b'jpeg')
    seen = []
    def transport(system, user, model, images=None):
        seen.append(images)
        return '{"ok":true}'
    monkeypatch.setattr(g, '_call_api', transport)
    assert g.call_claude_json('system', 'user', lambda x:x, images=[image]) == {'ok': True}
    assert g.call_claude_json('system', 'user', lambda x:x) == {'ok': True}
    assert seen == [[image], None]


def test_image_cli_has_no_tools_and_failure_explains_login(monkeypatch, tmp_path):
    import subprocess
    import shutil
    from types import SimpleNamespace
    from sofit import generate as g
    image = tmp_path / 'frame.jpg'
    image.write_bytes(b'jpeg')
    monkeypatch.setattr(shutil, 'which', lambda *a: '/bin/claude')
    def run(cmd, **kwargs):
        assert cmd[cmd.index('--tools') + 1] == ''
        assert '--strict-mcp-config' in cmd and '--disable-slash-commands' in cmd
        assert json.loads(kwargs['input'])['message']['content'][1]['source']['data'] == 'anBlZw=='
        return SimpleNamespace(returncode=1, stdout='Not logged in', stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(g.GenerationError, match='Not logged in'):
        g._call_claude_cli('system', 'user', 'model', images=[image])


def test_image_cli_native_stream_result(monkeypatch, tmp_path):
    import shutil
    import subprocess
    from types import SimpleNamespace
    from sofit import generate as g
    image = tmp_path / 'frame.jpg'
    image.write_bytes(b'jpeg')
    monkeypatch.setattr(shutil, 'which', lambda *a: '/bin/claude')
    def run(cmd, **kwargs):
        assert cmd[cmd.index('--input-format') + 1] == 'stream-json'
        assert json.loads(kwargs['input'])['message']['content'][0]['text'] == 'inspect'
        return SimpleNamespace(returncode=0, stderr='', stdout='\n'.join([
            '{"type":"system","subtype":"init"}',
            '{"type":"result","is_error":false,"result":"{\\"ok\\":true}"}']))
    monkeypatch.setattr(subprocess, 'run', run)
    assert g._call_claude_cli('system', 'inspect', 'model', images=[image]) == '{"ok":true}'
