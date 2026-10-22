"""Bounded temporal selection with deterministic, mocked visual judgments."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from sofit import footage as f
from sofit import footage_selection as s


def candidate():
    return f.Candidate("fake", "https://example.org/robot", "https://example.org/robot.webm",
                       "Unitree robot running", 600, 1280, 720)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*a, **k):
        raise AssertionError("network not allowed")
    monkeypatch.setattr(f, "open_url", denied)


def test_coarse_samples_follow_subtitles_or_are_bounded():
    intent = f.VisualIntent("Unitree robot running", "Unitree robot running", 4)
    times = s.coarse_times(600, intent, [f.Cue(420, 425, "Unitree robot running"), f.Cue(20, 22, "Hello")])
    assert len(times) == 4 and min(times) >= 418 and max(times) <= 427
    times = s.coarse_times(600, intent, [])
    assert len(times) == 12 and max(times) > 590
    assert s.coarse_times(600, intent, [f.Cue(float("nan"), 3, "robot")]) == times


@pytest.mark.parametrize("gap,expected", [(False, True), (True, False)])
def test_temporal_selection_does_not_bridge_unrelated_frames(monkeypatch, tmp_path, gap, expected):
    intent = f.VisualIntent("Unitree robot running", "Unitree robot running", 4)
    monkeypatch.setattr(s, "probe_video", lambda *a: f.VideoInfo(600, 1280, 720))
    calls = []
    def sample(media, times, directory):
        calls.append(times)
        return [s.Frame(t, Path("fake.jpg")) for t in times]
    monkeypatch.setattr(s, "_sample", sample)
    class Judge:
        cache_key = "test"
        def score(self, frames, intent):
            return [(0.95 if 417 <= frame.at <= 430 and not (gap and 420 < frame.at < 421)
                     else 0.1, "robot visibly running" if not gap else "scene boundary") for frame in frames]
    result = s.select_segment(tmp_path / "source", candidate(), intent,
                              [f.Cue(420, 425, "Unitree robot running")], Judge())
    assert (result is not None) is expected
    assert len(calls) == 2 and len(calls[0]) <= 12 and len(calls[1]) <= 25
    if result:
        assert result.end - result.start == 4 and 417 <= result.start < result.end <= 430
        assert result.confidence == 0.95 and result.source == candidate().source_url


def test_low_confidence_stops_before_refinement(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "probe_video", lambda *a: f.VideoInfo(600, 1280, 720))
    monkeypatch.setattr(s, "_sample", lambda media, times, directory: [s.Frame(t, Path("frame")) for t in times])
    class Judge:
        def score(self, frames, intent):
            return [(0.3, "identity uncertain") for _ in frames]
    assert s.select_segment(tmp_path / "source", candidate(), f.VisualIntent("robot", "robot", 4), [], Judge()) is None


def test_vision_payload_and_validation(monkeypatch):
    frames = [s.Frame(12, Path("a.jpg")), s.Frame(13, Path("b.jpg"))]
    def call(system, user, validate, **kwargs):
        assert "untrusted" in system and json.loads(user)["timestamps"] == [12, 13]
        assert kwargs["images"] == [f.path for f in frames]
        for bad in [{}, {"frames": [{"index": 0, "score": float("nan"), "reason": "x"}, {}]}]:
            with pytest.raises(s.generate.GenerationError):
                validate(bad)
        return validate({"frames": [{"index": i, "score": 0.9, "reason": "visible robot"} for i in range(2)]})
    monkeypatch.setattr(s.generate, "call_claude_json", call)
    assert s.ClaudeFrameJudge("claude-cli").score(frames, f.VisualIntent("robot", "robot", 4))[0][0] == 0.9


def test_pipeline_caches_selection_and_provenance(monkeypatch, tmp_path):
    media = tmp_path / "source.media"
    media.write_bytes(b"video")
    c = candidate()
    class Provider:
        name = "fake"
        def search(self, query, limit=8):
            return [c]
        def subtitles(self, candidate):
            return []
    class Cache:
        limits = f.Limits()
        def retrieve(self, candidate):
            return media, {"sha256": "content-hash", "retrieved_at": "2026-01-01T00:00:00+00:00"}
    class Judge:
        cache_key = "test-v1"
    calls = []
    def select(*args):
        calls.append(args)
        return s.SelectedSegment(c.source_url, 420, 424, 0.9, "running robot visible")
    def normalize(media, segment, path):
        path.write_bytes(b"silent H264")
        return path
    monkeypatch.setattr(s, "select_segment", select)
    monkeypatch.setattr(s, "normalize_segment", normalize)
    monkeypatch.setattr(s, "probe_video", lambda *a: f.VideoInfo(4, 1280, 720))
    intent = f.VisualIntent("Unitree robot running", "Unitree robot running", 4)
    one = s.find_footage(intent, [Provider()], Cache(), Judge())
    two = s.find_footage(intent, [Provider()], Cache(), Judge())
    assert one == two and len(calls) == 1
    assert one["source"]["selection"]["start"] == 420
    assert one["source"]["creator"] == c.creator
    assert one["source"]["retrieved_at"].endswith("+00:00")
    assert one["source"]["intent"] == json.loads(json.dumps(asdict(intent)))
    # Corrupt normalized bytes regenerate only the selection/asset, not source bytes.
    Path(one["video"]).write_bytes(b"broken")
    assert s.find_footage(intent, [Provider()], Cache(), Judge())
    assert len(calls) == 2


def test_provider_and_download_failures_fall_back():
    class Broken:
        name = "broken"
        def search(self, *a, **k):
            raise TimeoutError()
    intent = f.VisualIntent("robot", "robot", 4)
    assert s.find_footage(intent, [Broken()]) is None
    class Working:
        name = "fake"
        def search(self, *a, **k):
            return [candidate()]
    class Cache:
        limits = f.Limits()
        def retrieve(self, *a):
            raise f.FootageError("unsupported codec")
    assert s.find_footage(intent, [Working()], Cache()) is None
