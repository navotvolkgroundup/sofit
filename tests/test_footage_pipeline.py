"""Bounded source reuse, streaming checkpoints, cache lifecycle and real FFmpeg composition."""

import io
import json
import subprocess
import threading
from dataclasses import replace

import pytest

from sofit import footage as f, footage_index as index, footage_pipeline as pipeline
from sofit import (
    footage_progress as progress,
    footage_cache as lifecycle,
    storyboard as sb,
)


@pytest.mark.parametrize("delta,accepted", [(0, True), (1e-8, True), (0.005, False)])
def test_exact_span_end_clamped_without_editor_workarounds(
    tmp_path, monkeypatch, delta, accepted
):
    from sofit import footage_selection as fs

    video = tmp_path / "asset.mp4"
    video.write_bytes(b"asset")
    monkeypatch.setattr(fs, "find_footage", lambda *a, **k: {"video": str(video)})
    clip = {
        "id": "boundary",
        "start": 3210.64,
        "end": 3217.68,
        "visual_plan": [
            {
                "span": 0,
                "start": 0,
                "end": 7.04 + delta,
                "source": "web",
                "intent": "robot running",
                "query": "robot",
            }
        ],
    }
    sb.add_web_cutaways({"clips": [clip]}, str(tmp_path / "spec.json"))
    assert bool(clip["cutaways"]) == accepted
    if accepted:
        assert clip["cutaways"][0]["end"] <= clip["end"] - clip["start"]
        assert clip["footage_coverage_report"]["achieved_percent"] == 100


def source(tmp_path, name="source", seconds=40):
    out = tmp_path / (name + ".mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=red:s=320x240:r=30:d={seconds}",
            "-vf",
            "drawbox=color=green:t=fill:enable='between(t,2,10)+between(t,14,22)+between(t,26,38)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


class GreenJudge:
    cache_key = "green-fixture-v1"

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def score_many(self, frames, intents):
        from PIL import Image

        with self.lock:
            self.calls.append((len(frames), len(intents)))
        scored = []
        for frame in frames:
            with Image.open(frame.path) as image:
                r, g, b = image.getpixel((20, 20))
            scored.append(
                (0.95 if g > r else 0.1, "green action" if g > r else "unrelated red")
            )
        return [scored for i in intents]


def setup_source(monkeypatch, tmp_path, seconds=40):
    media = source(tmp_path, seconds=seconds)
    candidate = f.Candidate(
        "youtube",
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://example.org/source.mp4",
        "Unitree robot running",
        seconds,
        320,
        240,
        creator="Unitree",
    )
    resolves = []
    downloads = []
    monkeypatch.setattr(pipeline, "available", lambda: True)

    def resolve(self, url):
        resolves.append(url)
        return candidate

    monkeypatch.setattr(pipeline.YouTubeProvider, "resolve", resolve)

    def download(url, timeout=30):
        downloads.append(url)
        response = io.BytesIO(media.read_bytes())
        response.headers = {}
        return response

    monkeypatch.setattr(f, "open_url", download)
    return candidate, resolves, downloads


def test_shared_source_cold_warm_and_multiple_excerpts(tmp_path, monkeypatch):
    candidate, resolves, downloads = setup_source(monkeypatch, tmp_path)
    cache = f.FootageCache(tmp_path / "cache")
    judge = GreenJudge()
    intents = [
        f.VisualIntent(
            "Unitree robot running",
            "Unitree robot running",
            d,
            source_urls=(candidate.source_url,),
            required_terms=("Unitree",),
        )
        for d in (12, 10)
    ]
    reporter = progress.Progress()
    with reporter.active(), pipeline.FootageSession(cache, judge, workers=2) as session:
        session.submit(intents)
        results = [session.find(i) for i in intents]
    assert len(downloads) == len(resolves) == 1
    assert (
        reporter.counts["source_analyses"] == 1 and reporter.counts["frames_calls"] == 1
    )
    assert all(
        r and sum(p["duration"] for p in r["parts"]) == i.duration
        for i, r in zip(intents, results)
    )
    assert (
        len(results[0]["parts"]) >= 2
    )  # one long request can use several verified short windows
    selected = [
        (p["source"]["selection"]["start"], p["source"]["selection"]["end"])
        for r in results
        for p in r["parts"]
    ]
    assert all(
        b <= c or d <= a
        for n, (a, b) in enumerate(selected)
        for c, d in selected[n + 1 :]
    )
    previous = len(judge.calls)
    with pipeline.FootageSession(cache, judge) as session:
        session.submit(intents)
        assert [session.find(i) for i in intents] == results
    assert len(downloads) == len(resolves) == 1 and len(judge.calls) == previous
    assert not list(cache.root.rglob("*.part")) and not list(
        cache.root.rglob("frames-*")
    )


def test_distinct_actions_batch_but_do_not_share_semantic_scores(tmp_path, monkeypatch):
    candidate, _, _ = setup_source(monkeypatch, tmp_path)
    one = f.VisualIntent(
        "running", "Unitree robot", 4, source_urls=(candidate.source_url,)
    )
    two = replace(one, intent="sitting")

    class Judge(GreenJudge):
        def score_many(self, frames, intents):
            values = super().score_many(frames, intents)
            return [
                v if i.intent == "running" else [(0.1, "not sitting") for f in frames]
                for i, v in zip(intents, values)
            ]

    judge = Judge()
    with pipeline.FootageSession(f.FootageCache(tmp_path / "cache"), judge) as session:
        session.submit([one, two])
        assert session.find(one)
        assert session.find(two) is None
    assert all(actions == 2 for frames, actions in judge.calls)
    assert max(frames for frames, actions in judge.calls) <= 25


def test_independent_clips_reuse_ranges_but_reserve_existing_shots(
    tmp_path, monkeypatch
):
    candidate, resolves, downloads = setup_source(monkeypatch, tmp_path)
    intent = f.VisualIntent(
        "running", "Unitree robot", 4, source_urls=(candidate.source_url,)
    )
    judge = GreenJudge()
    with pipeline.FootageSession(f.FootageCache(tmp_path / "cache"), judge) as session:
        session.begin_clip(0)
        first = session.find(intent)
        second = session.find(intent)
        assert first != second  # no repeated excerpt within one clip
        calls = len(judge.calls)
        session.begin_clip(1)
        assert session.find(intent) == first
        session.reserve_existing(first["parts"], scope=2)
        session.begin_clip(2)
        assert session.find(intent) == second
        assert len(judge.calls) == calls
    assert len(downloads) == len(resolves) == 1


def test_queued_analysis_batches_actions_discovered_during_first_pass(
    tmp_path, monkeypatch
):
    candidate, _, downloads = setup_source(monkeypatch, tmp_path)
    entered, release = threading.Event(), threading.Event()
    batches, seen = [], set()

    def evidence(media, metadata, intents, judge, **kwargs):
        pending = {i.intent for i in intents} - seen
        if pending:
            batches.append(pending)
            if len(batches) == 1:
                entered.set()
                assert release.wait(5)
            seen.update(pending)
        return {index.semantic_key(i): [] for i in intents}

    monkeypatch.setattr(pipeline, "evidence", evidence)
    intents = [
        f.VisualIntent(action, "Unitree robot", 4)
        for action in ("running", "sitting", "carrying", "waving")
    ]
    with pipeline.FootageSession(
        f.FootageCache(tmp_path / "cache"), GreenJudge()
    ) as session:
        futures = [session._analysis_future(candidate, intents[0])]
        try:
            assert entered.wait(5)
            futures += [session._analysis_future(candidate, i) for i in intents[1:]]
        finally:
            release.set()
        for future in futures:
            future.result(timeout=10)
    assert batches == [{"running"}, {"sitting", "carrying", "waving"}]
    assert len(downloads) == 1


def test_search_cache_reuses_results_expires_and_does_not_cache_failures(
    tmp_path, monkeypatch
):
    from sofit import footage_selection
    from types import SimpleNamespace

    candidate = f.Candidate(
        "youtube",
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://example.org/video.mp4",
        "Robot",
        20,
        320,
        240,
    )
    calls, replies = [], [[candidate], [candidate], [], []]

    def search(providers, query):
        calls.append(query)
        return replies.pop(0)

    monkeypatch.setattr(footage_selection, "_search", search)
    cache = f.FootageCache(tmp_path / "cache")
    providers = [SimpleNamespace(name="youtube")]

    def lookup(query="robot"):
        with pipeline.FootageSession(cache, GreenJudge()) as session:
            return session._search_cached(providers, query, ("search", query))

    assert lookup() == lookup() == [candidate]
    assert calls == ["robot"]
    manifest = next(cache.root.glob("*/metadata.json"))
    saved = json.loads(manifest.read_text())
    saved["saved_at"] -= 3601
    manifest.write_text(json.dumps(saved))
    assert lookup() == [candidate] and len(calls) == 2
    assert lookup("unavailable") == lookup("unavailable") == []
    assert len(calls) == 4


def test_quota_failure_stops_launches_and_is_not_persisted_as_negative_evidence(
    tmp_path, monkeypatch
):
    from sofit.generate import GenerationError

    candidate, _, _ = setup_source(monkeypatch, tmp_path)

    class Judge:
        cache_key = "unavailable"
        calls = 0

        def score_many(self, *a):
            self.calls += 1
            raise GenerationError("You've hit your session limit")

    judge = Judge()
    gate = pipeline.JudgeGate(judge, 2)
    for _ in range(5):
        with pytest.raises((GenerationError, f.FootageError)):
            gate.score_many([], [])
    assert judge.calls == 1
    media = tmp_path / "source.mp4"
    assert (
        index.evidence(
            media, {"sha256": f._hash(media)}, [f.VisualIntent("run", "run", 3)], gate
        )
        == {}
    )
    assert not [p for p in tmp_path.glob("*.json") if p.name != "index.json"]


def test_download_failure_is_reused_with_reordered_urls(tmp_path, monkeypatch):
    candidate, resolves, downloads = setup_source(monkeypatch, tmp_path)

    def fail(*a, **k):
        downloads.append("fail")
        raise f.FootageError("offline")

    monkeypatch.setattr(f, "open_url", fail)
    a = f.VisualIntent(
        "running", "Unitree robot", 4, source_urls=(candidate.source_url,)
    )
    with pipeline.FootageSession(
        f.FootageCache(tmp_path / "cache"), GreenJudge()
    ) as session:
        session.submit([a, replace(a, duration=8)])
        assert session.find(a) is None and session.find(replace(a, duration=8)) is None
    assert len(downloads) == 1 and len(resolves) == 1
    assert not list((tmp_path / "cache").rglob("*.part"))


def test_cache_prune_legacy_files_lease_symlinks_and_dry_run(tmp_path):
    root = tmp_path / "cache"
    directory = root / ("a" * 64)
    directory.mkdir(parents=True)
    media = directory / "source.media"
    media.write_bytes(b"video")
    (directory / "source.json").write_text("{}")
    partial = directory / "tmpabcdefgh.part"
    partial.write_bytes(b"partial")
    final = directory / "final.mp4"
    final.write_bytes(b"user final output")
    external = tmp_path / "source.mp4"
    external.write_bytes(b"user source")
    (directory / ("b" * 64 + ".mp4")).symlink_to(external)
    preview = lifecycle.prune(root, clean=True, dry_run=True)
    assert preview["removed_files"] == 3 and media.exists() and partial.exists()
    with lifecycle.lease(root):
        assert lifecycle.prune(root, clean=True)["busy"] and media.exists()
    assert lifecycle.prune(root, clean=True)["removed_files"] == 3
    assert final.exists() and external.read_bytes() == b"user source"
    assert lifecycle.status(root)["files"] == 0


def test_progress_json_is_parseable_including_diagnostics(tmp_path, capsys):
    import sys

    path = tmp_path / "progress.jsonl"
    with progress.Progress("json", path).active():
        with progress.stage("search"):
            progress.count("cache_hit")
        print("warning: unavailable source", file=sys.stderr)
        progress.event("complete")
    console = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert console[-1]["counts"]["cache_hit"] == 1
    assert console[-1]["seconds"]["search"] >= 0
    assert any(row["stage"] == "log" for row in console)
    assert [json.loads(line) for line in path.read_text().splitlines()] == console


def test_frame_index_corruption_recovers_and_never_reads_outside_index(tmp_path):
    media = source(tmp_path, seconds=5)
    digest = f._hash(media)
    frames = index.frame_index(media, digest)
    assert len(frames) == 5
    frames[1].path.write_bytes(b"broken")
    assert index.frame_index(media, digest)[1].path.read_bytes() != b"broken"
    manifest = tmp_path / "index.json"
    data = json.loads(manifest.read_text())
    data["frames"][0]["name"] = "../../outside.jpg"
    manifest.write_text(json.dumps(data))
    assert index.frame_index(media, digest)[0].path.name == "000000.jpg"


def test_ready_clip_renders_before_unrelated_source_finishes(tmp_path, monkeypatch):
    c, _, _ = setup_source(monkeypatch, tmp_path)
    other = replace(
        c,
        source_url="https://www.youtube.com/watch?v=otherother1",
        media_url="https://example.org/other.mp4",
    )
    release = threading.Event()
    waiting = threading.Event()
    rendered = []
    monkeypatch.setattr(
        pipeline.YouTubeProvider,
        "resolve",
        lambda self, url: other if url == other.source_url else c,
    )
    original = pipeline.FootageSession._analyze

    def analyze(self, candidate, intents):
        if candidate.source_url == other.source_url:
            waiting.set()
            assert release.wait(10), "rendering waited for an unrelated source"
        return original(self, candidate, intents)

    monkeypatch.setattr(pipeline.FootageSession, "_analyze", analyze)
    clips = []
    for n, candidate in enumerate([c, other]):
        clips.append(
            {
                "id": str(n),
                "start": 0,
                "end": 4,
                "visual_plan": [
                    {
                        "source": "web",
                        "span": 0,
                        "start": 0,
                        "end": 4,
                        "query": "Unitree robot running",
                        "intent": "robot running",
                        "source_urls": [candidate.source_url],
                    }
                ],
            }
        )
    path = tmp_path / "spec.json"
    doc = {"clips": clips}

    def ready(clip):
        rendered.append(clip["id"])
        assert path.exists()  # checkpoint is available before the next source finishes
        if clip["id"] == "0":
            assert waiting.wait(5)
            assert clips[0]["footage_coverage_report"]["achieved_percent"] == 100
            release.set()

    with pipeline.FootageSession(
        f.FootageCache(tmp_path / "cache"), GreenJudge()
    ) as session:
        sb.add_web_cutaways(doc, str(path), session=session, on_clip=ready)
    assert rendered == ["0", "1"]


def test_pipeline_to_real_render_audio_and_captions(tmp_path, monkeypatch):
    from sofit import render

    candidate, _, _ = setup_source(monkeypatch, tmp_path)
    original = tmp_path / "audio.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=12",
            "-y",
            str(original),
        ],
        check=True,
    )
    clip = {
        "id": "full",
        "start": 0,
        "end": 12,
        "footage_coverage": 90,
        "words": [{"t": i, "d": 0.9, "w": "בדיקה"} for i in range(12)],
        "visual_plan": [
            {
                "source": "web",
                "span": 0,
                "start": 0,
                "end": 12,
                "query": "Unitree robot",
                "intent": "robot running",
                "source_urls": [candidate.source_url],
            }
        ],
    }
    monkeypatch.setattr(render, "_target_resolution", lambda *a: (180, 320))
    with pipeline.FootageSession(
        f.FootageCache(tmp_path / "cache"), GreenJudge()
    ) as session:
        sb.add_web_cutaways(
            {"clips": [clip]}, str(tmp_path / "spec.json"), session=session
        )
        output = render.render_clips(
            str(original), [clip], str(tmp_path / "out"), hook_card=False
        )[0]
    assert clip["footage_coverage_report"]["achieved_percent"] == 100
    baseline = render.render_clips(
        str(original),
        [{**clip, "cutaways": []}],
        str(tmp_path / "base"),
        hook_card=False,
    )[0]

    def audio(path):
        return subprocess.check_output(
            ["ffmpeg", "-v", "error", "-i", path, "-map", "0:a", "-f", "s16le", "-"]
        )

    assert audio(output) == audio(baseline)
    from PIL import Image

    for at in [1, 6, 10]:
        png = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(at),
                "-i",
                output,
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ]
        )
        im = Image.open(io.BytesIO(png))
        r, g, b = im.getpixel((5, 160))
        assert g > r
        assert (
            sum(
                im.getpixel((x, y))[0] > 180
                for x in range(180)
                for y in range(170, 290)
            )
            > 10
        )


def test_cli_streaming_progress_and_cache_commands(tmp_path, monkeypatch, capsys):
    from sofit import cli, render

    candidate, _, _ = setup_source(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "ClaudeFrameJudge", lambda *a: GreenJudge())
    monkeypatch.setattr(render, "_is_audio_only", lambda *a: True)
    order = []

    def rendered(video, clips, out, **kwargs):
        order.extend(c["id"] for c in clips)
        return []

    monkeypatch.setattr(render, "render_clips", rendered)
    clip = {
        "id": "one",
        "start": 0,
        "end": 4,
        "visual_plan": [
            {
                "source": "web",
                "start": 0,
                "end": 4,
                "query": "Unitree robot",
                "intent": "robot running",
                "source_urls": [candidate.source_url],
            }
        ],
    }
    spec = tmp_path / "clips.json"
    spec.write_text(
        json.dumps({"source": {"video": str(tmp_path / "source.mp4")}, "clips": [clip]})
    )
    assert (
        cli.main(["--render-from", str(spec), "--web-cutaways", "--progress", "json"])
        == 0
    )
    rows = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{")
    ]
    assert any(row["stage"] == "rendered" for row in rows) and order == ["one"]
    selecting = next(
        i for i, row in enumerate(rows) if row["stage"] == "clip_selection"
    )
    selected = next(i for i, row in enumerate(rows) if row["stage"] == "beat_selected")
    assert selecting < selected and rows[selecting]["clip"] == "one"
    saved = json.loads(spec.read_text())
    assert saved["clips"][0]["cutaways"]
    assert cli.main(["cache", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["bytes"] > 0
    assert cli.main(["cache", "clean", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["removed_files"] > 0


def test_distant_actions_retain_contiguous_fine_evidence(tmp_path, monkeypatch):
    from sofit.footage_selection import Frame

    frames = [Frame(i + 0.5, tmp_path / "unused.jpg") for i in range(240)]
    monkeypatch.setattr(index, "frame_index", lambda *a: frames)
    intents = [f.VisualIntent(str(n), "robot", 20) for n in range(4)]

    class Judge:
        cache_key = "distant"

        def score_many(self, frames, intents):
            return [
                [
                    (
                        0.95
                        if int(intent.intent) * 60 + 5
                        <= frame.at
                        <= int(intent.intent) * 60 + 40
                        else 0.1,
                        "evidence",
                    )
                    for frame in frames
                ]
                for intent in intents
            ]

    results = index.evidence(
        tmp_path / "source.media",
        {"sha256": "hash", "video": {"duration": 240}},
        intents,
        Judge(),
    )
    assert all(
        any(end - start >= 10 for start, end, _, _ in ranges)
        for ranges in results.values()
    )


def test_cache_does_not_own_arbitrary_source_files(tmp_path):
    directory = tmp_path / ("a" * 64)
    directory.mkdir()
    source = directory / "source.media"
    source.write_bytes(b"user input")
    output = directory / "tmp-final.mp4"
    output.write_bytes(b"final")
    assert lifecycle.prune(tmp_path, clean=True)["removed_files"] == 0
    assert source.exists() and output.exists()


def test_json_candidate_restores_subtitles_and_tags():
    c = f.Candidate(
        "fixture",
        "https://example.org/source",
        "https://example.org/a.mp4",
        "robot",
        10,
        320,
        240,
        cues=(f.Cue(2, 5, "robot running"),),
        tags=("robot",),
    )
    from dataclasses import asdict

    assert f.candidate_from_dict(json.loads(json.dumps(asdict(c)))) == c


def test_batch_judge_transport_validates_every_action(monkeypatch, tmp_path):
    from sofit import generate
    from sofit.footage_selection import ClaudeFrameJudge, Frame

    intents = [
        f.VisualIntent("running", "robot", 3),
        f.VisualIntent("sitting", "robot", 5),
    ]

    def call(system, user, validate, **kwargs):
        assert len(json.loads(user)["actions"]) == 2 and len(kwargs["images"]) == 1
        with pytest.raises(generate.GenerationError):
            validate({"frames": [{"index": 0, "scores": [0.9], "reason": "robot"}]})
        return validate(
            {"frames": [{"index": 0, "scores": [0.9, 0.1], "reason": "robot running"}]}
        )

    monkeypatch.setattr(generate, "call_claude_json", call)
    result = ClaudeFrameJudge().score_many([Frame(1, tmp_path / "frame.jpg")], intents)
    assert result == [[(0.9, "robot running")], [(0.1, "robot running")]]


def test_blocked_service_still_serves_cached_evidence(tmp_path, monkeypatch):
    candidate, _, _ = setup_source(monkeypatch, tmp_path)
    intent = f.VisualIntent(
        "robot running", "Unitree robot", 4, source_urls=(candidate.source_url,)
    )
    cache = f.FootageCache(tmp_path / "cache")
    judge = GreenJudge()
    with pipeline.FootageSession(cache, judge) as session:
        assert session.find(intent)
    before = len(judge.calls)
    with pipeline.FootageSession(cache, judge) as session:
        session.judge.blocked = "quota"
        assert session.find(intent)
    assert len(judge.calls) == before


def test_direct_link_legacy_manifest_and_warm_batch(tmp_path, monkeypatch):
    candidate, _, downloads = setup_source(monkeypatch, tmp_path)
    intent = f.VisualIntent(
        "robot running", "robot", 4, source_urls=(candidate.media_url,)
    )
    cache = f.FootageCache(tmp_path / "cache")
    judge = GreenJudge()
    for _ in range(2):
        with pipeline.FootageSession(cache, judge) as session:
            result = session.find(intent)
            assert result and result["parts"][0]["source"]["provider"] == "direct"
    assert len(downloads) == 1


def test_progress_file_failure_is_nonfatal(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    p = progress.Progress(path=directory)
    with p.active():
        p.event("rendered", clip="1")
    assert p.snapshot()["progress_file_error"] == "IsADirectoryError"
    assert p.path is None


def test_cache_under_aliased_parent_is_managed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    root = alias / "footage"
    directory = root / ("b" * 64)
    directory.mkdir(parents=True)
    (directory / "source.json").write_text("{}")
    (directory / "source.media").write_bytes(b"cached")
    assert lifecycle.status(root)["files"] == 2
    assert lifecycle.prune(root, dry_run=True, clean=True)["removed_files"] == 2
    assert (directory / "source.media").exists()


def test_cancelled_download_cleans_partial_file(tmp_path, monkeypatch):
    candidate = f.Candidate(
        "fixture",
        "https://example.org/v",
        "https://example.org/v.mp4",
        "robot",
        10,
        320,
        240,
    )
    response = io.BytesIO(b"source bytes")
    response.headers = {}
    monkeypatch.setattr(f, "open_url", lambda *args: response)
    checks = []

    def cancelled():
        checks.append(True)
        return len(checks) > 2  # cancellation arrives after opening the response

    reporter = progress.Progress()
    with reporter.active(), pytest.raises(f.FootageError, match="cancelled"):
        f.FootageCache(tmp_path).retrieve(candidate, cancelled=cancelled)
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("source.media"))
    assert reporter.counts["downloads_cancelled"] == 1
    assert response.closed


def test_progress_tracks_overlapping_stages_without_stale_errors():
    reporter = progress.Progress()
    with reporter.active(), progress.stage("source"):
        progress.event("source_rejected", reason="wrong subject", source="source-id")
        with progress.stage("render"):
            snapshot = reporter.snapshot()
            assert snapshot["active_stages"] == {"source": 1, "render": 1}
            assert "reason" not in snapshot and "source" not in snapshot
        assert reporter.snapshot()["active_stages"] == {"source": 1}
    assert reporter.snapshot()["active_stages"] == {}
