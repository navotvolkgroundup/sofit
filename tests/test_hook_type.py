"""Hook-type labels on the shortlist, the clip spec, and the performance log.

The label is a soft preference. A flagged opening is kept, an old row with no
hook_type still parses, and the length bar stays 20-45s.
"""

import importlib.util
import json
from pathlib import Path

import pytest

import sofit.generate as gen
from sofit.transcribe import Segment, Word


ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wseg(i, start, text):
    tokens = text.split()
    words = [Word(start + n * 0.5, start + n * 0.5 + 0.4, t) for n, t in enumerate(tokens)]
    return Segment(index=i, start=start, end=words[-1].end, text=text, words=words)


SEGMENTS = [
    _wseg(0, 0.0, "שלום וברוכים הבאים לפודקאסט"),
    _wseg(1, 30.0, "היום נדבר על בינה מלאכותית"),
]


def _item(**extra):
    base = {
        "title": "כותרת",
        "score": 9,
        "quote_start": "שלום וברוכים",
        "quote_end": "היום נדבר",
    }
    base.update(extra)
    return base


def test_normalize_and_parse_hook_type():
    assert gen.normalize_hook_type("Bold Claim") == "bold_claim"
    assert gen.normalize_hook_type("high-stakes") == "high_stakes"
    assert gen.normalize_hook_type("surprise") == "curiosity_gap"  # legacy label
    assert gen.normalize_hook_type("story") == "middle_of_action"
    assert gen.normalize_hook_type("nope") == "other"
    assert gen.normalize_hook_type(None) == "other"
    assert gen.parse_hook_type("high-stakes") == "high_stakes"
    assert gen.parse_hook_type("surprise") == "curiosity_gap"
    with pytest.raises(ValueError):
        gen.parse_hook_type("nope")


def test_length_bar_and_prompt_contract_unchanged():
    # Clips stay inside the existing 20-45s window (under 60s). The new labels
    # share the prompt the shortlist already used; they are not a second call.
    import inspect
    params = inspect.signature(gen.resolve_clip_item).parameters
    assert params["min_sec"].default == 18.0
    assert params["max_sec"].default == 45.0
    assert "20-45" in gen.CLIP_RULES
    assert "SOFT PREFERENCE" in gen.CLIP_RULES
    assert "curiosity_gap" in gen.CLIP_FIELDS
    assert "relatable_callout" in gen.CLIP_FIELDS
    assert "question|bold_claim|surprise|emotion|story" not in gen.CLIP_FIELDS
    pool = _load("sofit_pool", ROOT / "scripts" / "pool.py")
    assert "curiosity_gap" in pool.POOL_SYSTEM
    assert "hook_reason" in pool.POOL_SYSTEM
    assert gen.CLIP_FIELDS in pool.POOL_SYSTEM
    skill = (ROOT / "skills" / "sofit" / "clips.py").read_text(encoding="utf-8")
    assert skill == (ROOT / "scripts" / "pool.py").read_text(encoding="utf-8")


def test_flagged_opening_is_kept_and_alt_must_be_in_the_transcript():
    q = gen.resolve_clip_item(_item(
        hook_type="bold_claim",
        hook_reason="opens on the provocation\nsecond line ignored",
        alt_opening="היום נדבר על בינה",
        alt_hook_type="curiosity_gap",
    ), SEGMENTS, SEGMENTS[-1].end)
    assert q is not None
    assert q.hook_type == "bold_claim"
    assert q.hook_reason == "opens on the provocation"
    assert q.alt_opening == "היום נדבר על בינה"
    assert q.alt_hook_type == "curiosity_gap"

    # Invented alt is dropped. The candidate stays.
    invented = gen.resolve_clip_item(_item(
        hook_type="question",
        alt_opening="משפט שלא נאמר מעולם בכלל",
        alt_hook_type="high_stakes",
    ), SEGMENTS, SEGMENTS[-1].end)
    assert invented is not None
    assert invented.hook_type == "question"
    assert invented.alt_opening == ""

    # A preferred type does not grow an alt, and a weak score is still dropped.
    plain = gen.resolve_clip_item(_item(
        hook_type="curiosity_gap",
        alt_opening="היום נדבר על בינה",
    ), SEGMENTS, SEGMENTS[-1].end)
    assert plain.hook_type == "curiosity_gap"
    assert plain.alt_opening == ""
    assert gen.resolve_clip_item(_item(hook_type="relatable_callout", score=4),
                                 SEGMENTS, SEGMENTS[-1].end) is None


def test_equal_scores_lean_and_nothing_is_dropped_for_its_type():
    # Same overlap rule as the pool: higher score wins, equal scores lean.
    rows = [
        {"score": 8, "hook_type": "bold_claim", "start": 0, "end": 10},
        {"score": 8, "hook_type": "relatable_callout", "start": 0, "end": 10},
        {"score": 9, "hook_type": "question", "start": 20, "end": 30},
        {"score": 8, "hook_type": "curiosity_gap", "start": 40, "end": 50},
    ]
    rows.sort(key=lambda c: gen.hook_rank_key(c["score"], c["hook_type"]))
    kept = []
    for c in rows:
        if any(not (c["end"] <= k["start"] or c["start"] >= k["end"]) for k in kept):
            continue
        kept.append(c)
    assert [c["hook_type"] for c in kept] == [
        "question",            # score 9 still beats the preferred 8s
        "relatable_callout",   # wins the tie against bold_claim
        "curiosity_gap",
    ]


def test_shortlist_lines_show_type_reason_flag_and_alt():
    gap = gen.format_pool_candidate(1, {
        "start": 14 * 60 + 22, "end": 893, "beats": [[862, 893]],
        "score": 9, "hook": 'כתבת "אקס גוגל" בלינקדאין? זה משדר בדיוק הפוך',
        "hook_type": "curiosity_gap",
        "hook_reason": "withholds what the LinkedIn line actually signals",
        "reason": "viewer's own profile line, payoff lands later in the clip",
    })
    flagged = gen.format_pool_candidate(2, {
        "start": 22 * 60 + 8, "end": 1355, "beats": [[1328, 1355]],
        "score": 8, "hook": "זה בולשיט מוחלט",
        "hook_type": "bold_claim",
        "hook_reason": "opens on the provocation, no stake yet",
        "reason": "sharp opinion, but the claim is the whole first line",
        "alt_opening": "הלקוח שילם ארבעים אלף וקיבל דף אחד",
        "alt_hook_type": "curiosity_gap",
    })
    assert gap == (
        ' 1. [14:22] (31s, curiosity_gap, score 9) '
        'כתבת "אקס גוגל" בלינקדאין? זה משדר בדיוק הפוך\n'
        "      type: withholds what the LinkedIn line actually signals\n"
        "      why: viewer's own profile line, payoff lands later in the clip"
    )
    assert flagged == (
        " 2. [22:08] (27s, bold_claim flag, score 8) זה בולשיט מוחלט\n"
        "      type: opens on the provocation, no stake yet\n"
        "      why: sharp opinion, but the claim is the whole first line\n"
        "      alt opening (curiosity_gap): הלקוח שילם ארבעים אלף וקיבל דף אחד"
    )
    # The skill shows this exact shape, so the operator and the code can't drift.
    skill = (ROOT / "skills" / "sofit-clips" / "SKILL.md").read_text(encoding="utf-8")
    assert gap in skill and flagged in skill


def test_clip_spec_carries_hook_type_and_omits_it_when_absent():
    q = gen.resolve_clip_item(_item(
        hook_type="high_stakes",
        hook_reason="money on the table",
        hook_variants=["חלופה אחת", "חלופה שתיים"],
        hook_variant_types=["relatable_callout", "curiosity_gap"],
    ), SEGMENTS, SEGMENTS[-1].end)
    # A variant that repeats the title is dropped, and its type goes with it
    # so the remaining type still names the remaining line.
    dropped = gen.resolve_clip_item(_item(
        hook_type="high_stakes",
        hook_variants=["כותרת", "חלופה אמיתית"],
        hook_variant_types=["bold_claim", "curiosity_gap"],
    ), SEGMENTS, SEGMENTS[-1].end)
    assert dropped.variants == ("חלופה אמיתית",)
    assert dropped.variant_types == ("curiosity_gap",)

    spec = gen.clip_spec(q, SEGMENTS, "clip-1")
    assert spec["hook_type"] == "high_stakes"
    assert spec["hook_reason"] == "money on the table"
    assert spec["hook_variant_types"] == ["relatable_callout", "curiosity_gap"]
    assert "alt_opening" not in spec

    bare = gen.clip_spec(gen.Quote(start=0, end=20, text="x"), [], "clip-2")
    assert "hook_type" not in bare
    assert "hook_variant_types" not in bare


def test_build_copies_hook_type_and_old_pool_stays_unlabeled(tmp_path, monkeypatch):
    pool = _load("sofit_pool_build", ROOT / "scripts" / "pool.py")
    segs = [Segment(0, 0.0, 20.0, "היום נדבר על בינה", [])]
    monkeypatch.setattr(pool, "cached_segments", lambda video: segs)
    src = tmp_path / "ep.pool.json"
    src.write_text(json.dumps({"candidates": [
        {"start": 0, "end": 20, "beats": [[0, 20]], "hook": "היום נדבר",
         "hook_type": "Bold Claim", "hook_reason": "flat assertion",
         "alt_opening": "היום נדבר על בינה", "alt_hook_type": "curiosity-gap",
         "hook_variants": ["חלופה"], "hook_variant_types": ["relatable_callout"]},
        {"start": 0, "end": 20, "hook": "בלי תווית", "type": "question"},
        {"start": 0, "end": 20, "hook": "ישן"},
    ]}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "ep.clips.json"
    pool.cmd_build("ep.mp4", str(src), "1,2,3", str(out))
    clips = json.loads(out.read_text(encoding="utf-8"))["clips"]
    assert clips[0]["hook_type"] == "bold_claim"
    assert clips[0]["alt_hook_type"] == "curiosity_gap"
    assert clips[0]["hook_variant_types"] == ["relatable_callout"]
    assert clips[1]["hook_type"] == "question"  # legacy `type` key still copies
    assert "hook_type" not in clips[2]


def test_manual_log_hook_type_is_optional(tmp_path):
    pool = _load("sofit_pool_log", ROOT / "scripts" / "pool.py")
    log = tmp_path / "performance.jsonl"
    pool.cmd_log("WS1", "clip-1", "הוק", "tiktok", 10, 40.0, 0, str(log))
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["hook"] == "הוק" and "hook_type" not in row
    assert gen._perf_rows(str(log)) == [row]

    pool.cmd_log("WS1", "clip-2", "הוק 2", "tiktok", 11, 50.0, 0, str(log),
                 hook_type="high-stakes")
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert rows[1]["hook_type"] == "high_stakes"
    assert len(gen._perf_rows(str(log))) == 2  # the unlabeled row still counts

    spec = tmp_path / "ep.clips.json"
    spec.write_text(json.dumps({"clips": [{
        "id": "clip-5", "hook": "מהספק", "hook_type": "curiosity_gap",
        "hook_variants": ["חלופה"], "hook_variant_types": ["relatable_callout"],
    }]}), encoding="utf-8")
    pool.cmd_log("WS1", "clip-5", "חלופה", "tiktok", 3, 20.0, 1, str(log),
                 spec=str(spec))
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert last["hook_type"] == "relatable_callout"

    with pytest.raises(SystemExit):
        pool.cmd_log("WS1", "clip-9", "x", "tiktok", 1, 1.0, 0, str(log),
                     hook_type="not-a-type")


def test_publish_log_copies_hook_type_and_old_rows_omit_it(tmp_path):
    from sofit import publog
    spec = tmp_path / "ep.clips.json"
    spec.write_text(json.dumps({"clips": [{
        "id": "clip-3", "hook": "הוק ראשי", "hook_type": "relatable_callout",
        "hook_variants": ["חלופה"], "hook_variant_types": ["curiosity_gap"],
    }]}, ensure_ascii=False), encoding="utf-8")
    log = tmp_path / "perf.jsonl"

    rc = publog.main(["clip-3", "--episode", "T1", "--platform", "tiktok",
                      "--url", "https://t.example/1", "--spec", str(spec),
                      "--log", str(log)])
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["hook"] == "הוק ראשי"
    assert row["hook_type"] == "relatable_callout"

    rc = publog.main(["clip-3.hook1", "--episode", "T1", "--platform", "tiktok",
                      "--url", "https://t.example/2", "--spec", str(spec),
                      "--log", str(log)])
    assert rc == 0
    variant = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert variant["hook"] == "חלופה"
    assert variant["hook_type"] == "curiosity_gap"
    assert variant["variant"] == 1

    # Override wins, and a spec with no label does not invent one.
    bare = tmp_path / "old.clips.json"
    bare.write_text(json.dumps({"clips": [{"id": "clip-1", "hook": "ישן"}]}),
                    encoding="utf-8")
    rc = publog.main(["clip-1", "--episode", "T1", "--platform", "instagram",
                      "--url", "https://t.example/3", "--spec", str(bare),
                      "--log", str(log)])
    assert rc == 0
    parsed = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    old = parsed[-1]
    assert "hook_type" not in old
    # A row with the field and a row without it are both valid JSON objects.
    assert any("hook_type" in r for r in parsed) and any("hook_type" not in r for r in parsed)

    rc = publog.main(["clip-1", "--episode", "T1", "--platform", "youtube",
                      "--url", "https://t.example/4", "--hook", "ישן",
                      "--hook-type", "high stakes", "--log", str(log)])
    assert rc == 0
    over = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert over["hook_type"] == "high_stakes"

    rc = publog.main(["clip-1", "--episode", "T1", "--platform", "tiktok",
                      "--url", "https://t.example/5", "--hook", "ישן",
                      "--hook-type", "nope", "--log", str(log)])
    assert rc == 1
    assert "t.example/5" not in log.read_text(encoding="utf-8")


def test_performance_hint_names_hook_type_when_present(tmp_path):
    rows = [{"hook": f"h{i}", "retention": 40} for i in range(7)]
    rows.append({"hook": "WINNER", "retention": 90, "hook_type": "curiosity_gap"})
    p = tmp_path / "perf.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    hint = gen.performance_hint(str(p), n=1)
    assert "curiosity_gap" in hint
    assert "WINNER" in hint


def test_metrics_summary_groups_by_hook_type_and_skips_old_rows():
    scrape = _load("sofit_scrape", ROOT / "scripts" / "publish" / "scrape.py")
    lines = [
        {"platform": "tiktok", "hook_type": "curiosity_gap", "avg_watch_s": 10},
        {"platform": "tiktok", "hook_type": "curiosity_gap", "avg_watch_s": 20},
        {"platform": "tiktok", "hook_type": "bold_claim", "avg_watch_s": 4},
        {"platform": "tiktok", "avg_watch_s": 99},  # pre-hook_type row
        {"platform": "instagram", "hook_type": "question", "views": 100},
        {"platform": "instagram", "views": 5},
    ]
    grouped = scrape.metric_cells(lines, "hook_type")
    assert grouped["tiktok"] == {
        "bold_claim": {"clips": 1, "avg_watch_s": 4.0},
        "curiosity_gap": {"clips": 2, "avg_watch_s": 15.0},
    }
    assert "question" in grouped["instagram"]
    assert all("99" not in str(v) for v in grouped["tiktok"].values())
    # hook_style's default bucket is a different field and must still work.
    styled = scrape.metric_cells(
        [{"platform": "tiktok", "avg_watch_s": 3, "hook_style": "persistent"},
         {"platform": "tiktok", "avg_watch_s": 5}],
        "hook_style", "flash")
    assert set(styled["tiktok"]) == {"persistent", "flash"}
