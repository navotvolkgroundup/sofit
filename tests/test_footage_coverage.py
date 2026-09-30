"""Dense audio visuals, topic-aware discovery and long-shot validation."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from sofit import (
    footage as f,
    footage_selection as fs,
    storyboard as sb,
    generate,
    cli,
    render,
)
from sofit.transcribe import Segment, Word


def beat(start=0, end=4):
    return {
        "span": 0,
        "start": start,
        "end": end,
        "source": "web",
        "intent": "Figure robot collecting toys",
        "query": "Figure Helix 2.5 30 homes demonstration",
        "required_terms": ["Figure", "Helix 2.5"],
        "preferred_channels": ["Figure"],
    }


def test_context_and_full_timeline_planning(monkeypatch):
    clip = {
        "start": 0,
        "end": 40,
        "hook": "56 percent",
        "words": [{"t": 0, "w": "הרובוט"}],
        "visual_context": "פיגר השיקה אליקס 2.5 בשלושים בתים",
    }

    def call(system, user, validate, **kwargs):
        assert "אליקס 2.5" in user and "56 percent" in user
        assert "Target 90%" in system and "not 'robot home'" in system
        return validate({"cutaways": [beat(i, i + 4) for i in range(0, 40, 4)]})

    monkeypatch.setattr(sb, "call_claude_json", call)
    plan = sb.plan_cutaways(clip, web=True, coverage=90)
    assert len(plan) == 10 and plan[0]["start"] == 0 and plan[-1]["end"] == 40
    assert plan[0]["required_terms"] == ["Figure", "Helix 2.5"]


def test_clip_spec_retains_topic_introduction_outside_kept_words():
    segs = [
        Segment(0, 100, 110, "Figure Helix 2.5 announcement", []),
        Segment(1, 250, 260, "56 percent", [Word(250, 251, "56")]),
    ]
    clip = generate.clip_spec(generate.Quote(250, 260, "A robot"), segs, "clip")
    assert "Helix 2.5" in clip["visual_context"]
    assert clip["words"] == [{"t": 0, "d": 1, "w": "56"}]


def test_planner_receives_exact_fractional_span_duration(monkeypatch):
    import re

    def call(system, user, validate, **kwargs):
        limit = float(re.search(r"span 0 \(0\.\.([\d.]+)s\)", user)[1])
        assert limit == pytest.approx(7.04)
        return validate({"cutaways": [beat(0, limit)]})

    monkeypatch.setattr(sb, "call_claude_json", call)
    plan = sb.plan_cutaways({"start": 3210.64, "end": 3217.68}, web=True, coverage=90)
    assert plan[0]["end"] == pytest.approx(7.04)


def test_coverage_does_not_report_floating_point_zero_length_gaps(tmp_path):
    asset = tmp_path / "asset.mp4"
    asset.write_bytes(b"video")
    clip = {"start": 0, "end": 10.02, "cutaways": [
        {"video": str(asset), "start": 0, "end": 5},
        {"video": str(asset), "start": 5 + 1e-12, "end": 10.02 - 1e-12},
    ]}
    assert sb.footage_coverage(clip, 90)["gaps"] == []


def test_manual_many_beats_and_thirty_second_shot_are_not_truncated(
    monkeypatch, tmp_path
):
    video = tmp_path / "shot.mp4"
    video.write_bytes(b"video")
    plan = [beat(0, 30)] + [beat(i, i + 5) for i in range(30, 70, 5)]
    clip = {"id": "full", "start": 0, "end": 70, "visual_plan": plan}
    doc = {"clips": [clip]}
    seen = []

    def find(intent, **kwargs):
        seen.append(intent)
        return {"video": str(video)}

    monkeypatch.setattr(fs, "find_footage", find)
    assert sb.add_web_cutaways(doc, str(tmp_path / "spec.json"), coverage=90) == 9
    assert len(seen) == 9 and seen[0].duration == 30
    assert all(i.required_terms == ("Figure", "Helix 2.5") for i in seen)
    assert clip["footage_coverage_report"]["achieved_percent"] == 100
    assert not clip["footage_coverage_report"]["gaps"]
    assert sb.add_web_cutaways(doc, str(tmp_path / "spec.json"), coverage=95) == 0
    assert clip["footage_coverage_report"]["requested_percent"] == 95


def test_coverage_reports_actual_union_and_missing_assets(tmp_path):
    v = tmp_path / "video.mp4"
    v.write_bytes(b"video")
    clip = {
        "start": 0,
        "end": 40,
        "cutaways": [
            {"start": 0, "end": 10, "video": str(v)},
            {"start": 5, "end": 15, "video": str(v)},
            {"start": 15, "end": 40, "video": "missing.mp4"},
        ],
    }
    result = sb.footage_coverage(clip, 85)
    assert result["video_seconds"] == 15 and result["achieved_percent"] == 37.5
    assert result["gaps"] == [{"span": 0, "start": 15, "end": 40}]


def test_edited_plan_replaces_obsolete_automatic_assets_but_preserves_manual(
    monkeypatch, tmp_path
):
    video = tmp_path / "shot.mp4"
    video.write_bytes(b"video")
    manual = {"start": 10, "end": 14, "video": str(video)}
    clip = {
        "id": "edit",
        "start": 0,
        "end": 20,
        "visual_plan": [beat()],
        "cutaways": [manual],
    }
    monkeypatch.setattr(fs, "find_footage", lambda *a, **k: {"video": str(video)})
    doc = {"clips": [clip]}
    path = str(tmp_path / "spec.json")
    assert sb.add_web_cutaways(doc, path) == 1
    old_id = clip["cutaways"][1]["plan_id"]
    clip["visual_plan"][0]["query"] = "Figure Helix 2.5 official demonstration"
    assert sb.add_web_cutaways(doc, path) == 1
    assert len(clip["cutaways"]) == 2 and manual in clip["cutaways"]
    assert clip["cutaways"][1]["plan_id"] != old_id


def test_unmet_target_warns_instead_of_claiming_full_coverage(
    monkeypatch, tmp_path, capsys
):
    clip = {"id": "partial", "start": 0, "end": 40, "visual_plan": [beat()]}
    monkeypatch.setattr(fs, "find_footage", lambda *a, **k: None)
    assert (
        sb.add_web_cutaways({"clips": [clip]}, str(tmp_path / "spec.json"), coverage=85)
        == 0
    )
    assert clip["footage_coverage_report"]["achieved_percent"] == 0
    assert "coverage not met" in capsys.readouterr().err


def test_specific_release_filter_and_publisher_preference():
    intent = f.VisualIntent(
        "Figure robot",
        "Figure Helix 2.5",
        4,
        required_terms=("Figure Helix 2.5",),
        preferred_channels=("Figure",),
    )
    c = f.Candidate(
        "youtube",
        "https://example.org/new",
        "https://example.org/new.mp4",
        "Helix 2.5 in homes",
        60,
        1280,
        720,
        creator="Figure",
    )
    news = replace(
        c,
        media_url="https://example.org/news.mp4",
        creator="News",
        description="Figure Helix 2.5",
    )
    old = replace(
        c, media_url="https://example.org/old.mp4", title="Helix 2.0 in 5 homes"
    )
    other = replace(c, media_url="https://example.org/other.mp4", creator="Unitree")
    assert f.rank_candidates([news, old, other, c], intent) == [c, news]
    assert f.matches_subject(replace(c, title="Helix2.5"), intent.required_terms)


def test_thirty_second_selection_preserves_density_and_bounded_batches(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(fs, "probe_video", lambda *a: f.VideoInfo(80, 1280, 720))
    samples = []

    def sample(media, times, directory):
        samples.append(times)
        return [fs.Frame(t, Path("frame.jpg")) for t in times]

    monkeypatch.setattr(fs, "_sample", sample)

    class Judge:
        def __init__(self):
            self.batches = []

        def score(self, frames, intent):
            self.batches.append(len(frames))
            return [(0.9, "visible task") for f in frames]

    judge = Judge()
    c = f.Candidate(
        "fake",
        "https://example.org/x",
        "https://example.org/x.mp4",
        "task",
        80,
        1280,
        720,
    )
    selected = fs.select_segment(
        tmp_path / "source", c, f.VisualIntent("task", "task", 30), [], judge
    )
    assert selected and selected.end - selected.start == 30
    assert (
        len(samples[1]) == 61
        and max(b - a for a, b in zip(samples[1], samples[1][1:])) <= 1.01
    )
    assert max(judge.batches) <= 25
    with pytest.raises(ValueError):
        f.VisualIntent("task", "task", 31)


def test_cli_audio_default_and_explicit_target_with_cached_context(
    monkeypatch, tmp_path
):
    from sofit import transcribe

    source = tmp_path / "episode.mp3"
    source.write_bytes(b"audio")
    path = tmp_path / "spec.json"
    path.write_text(
        json.dumps(
            {
                "source": {"video": str(source)},
                "clips": [{"id": "a", "start": 250, "end": 270}],
            }
        )
    )
    monkeypatch.setattr(render, "_is_audio_only", lambda *a: True)
    monkeypatch.setattr(
        transcribe,
        "cached_segments",
        lambda *a: [Segment(0, 100, 110, "Helix 2.5", [])],
    )

    def forbidden(*a, **k):
        raise AssertionError("render must not transcribe")

    monkeypatch.setattr(transcribe, "transcribe", forbidden)
    calls = []

    def add(doc, *a, **k):
        calls.append((doc, k))
        return 0

    monkeypatch.setattr(sb, "add_web_cutaways", add)
    monkeypatch.setattr(render, "render_clips", lambda *a, **k: [])
    assert cli.main(["--render-from", str(path), "--web-cutaways"]) == 0
    assert calls[0][0]["clips"][0]["footage_coverage"] == 85
    assert "Helix 2.5" in calls[0][0]["clips"][0]["visual_context"]
    assert cli.main(["--render-from", str(path), "--footage-coverage", "95"]) == 0
    assert calls[-1][1]["coverage"] == 95
    assert cli.main(["--render-from", str(path), "--footage-coverage", "nan"]) == 1


def test_detailed_search_retries_exact_subject_and_reuses_discovery(
    monkeypatch, tmp_path
):
    c = f.Candidate(
        "fake",
        "https://example.org/x",
        "https://example.org/x.mp4",
        "Helix 2.5",
        60,
        1280,
        720,
        creator="Figure",
    )
    calls = []

    class Provider:
        name = "fake"

        def search(self, query, limit=8):
            calls.append(query)
            return [c] if query == "Figure Helix 2.5" else []

        def subtitles(self, c):
            return []

    class Cache:
        limits = f.Limits()

        def retrieve(self, c):
            raise f.FootageError("mock download failure")

    intent = f.VisualIntent(
        "robot picking up toys",
        "Figure Helix 2.5 picking toys slow home demo",
        4,
        required_terms=("Figure", "Helix 2.5"),
        preferred_channels=("Figure",),
    )
    discoveries = {}
    provider = Provider()
    assert (
        fs.find_footage(intent, [provider], Cache(), discovery_cache=discoveries)
        is None
    )
    assert calls == [intent.query, "Figure Helix 2.5"]
    fs.find_footage(intent, [provider], Cache(), discovery_cache=discoveries)
    assert len(calls) == 2


def test_partial_planning_retry_does_not_discard_useful_beats(monkeypatch):
    clip = {"start": 0, "end": 40, "words": []}

    def call(system, user, validate, **kwargs):
        return validate({"cutaways": [beat(0, 4)]})

    monkeypatch.setattr(sb, "call_claude_json", call)
    # The transport normally retries validation; after exhaustion keep the
    # partial result and let the measured coverage report disclose the gap.
    assert len(sb.plan_cutaways(clip, web=True, coverage=85)) == 1


def test_anchor_search_finds_primary_publisher_even_when_news_is_eligible(monkeypatch):
    official = f.Candidate(
        "fake",
        "https://example.org/official",
        "https://example.org/official.mp4",
        "Helix 2.5 in homes",
        60,
        1280,
        720,
        creator="Figure",
    )
    news = replace(
        official,
        source_url="https://example.org/news",
        media_url="https://example.org/news.mp4",
        title="Figure Helix 2.5 robot collecting toys demonstration",
        creator="News",
    )
    calls = []
    downloads = []

    class Provider:
        name = "fake"

        def search(self, query, limit=8):
            calls.append(query)
            return [official] if query == "Figure Helix 2.5" else [news]

    class Cache:
        limits = f.Limits()

        def retrieve(self, c):
            downloads.append(c)
            raise f.FootageError("mock download failure")

    intent = f.VisualIntent(
        "robot collecting toys",
        "Figure Helix 2.5 robot collecting toys demonstration",
        4,
        required_terms=("Figure", "Helix 2.5"),
        preferred_channels=("Figure",),
    )
    fs.find_footage(intent, [Provider()], Cache())
    assert calls == [intent.query, "Figure Helix 2.5"]
    assert downloads[0] == official
