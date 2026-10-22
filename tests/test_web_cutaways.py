"""Visual planning, editable specs, CLI compatibility and existing composition."""

import json
from pathlib import Path

import pytest

from sofit import cli, render, storyboard as sb
from sofit import footage_selection as fs


def beat():
    return {"span": 0, "start": 4, "end": 8, "source": "web", "intent": "Unitree robot running",
            "query": "Unitree robot running", "context": "A running robot", "prompt": "A running robot"}


def doc(tmp_path):
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"original")
    return {"schema_version": 1, "source": {"video": str(source)}, "clips": [
        {"id": "clip-1", "start": 100, "end": 114, "words": [{"t": 4, "d": 1, "w": "רובוט"}]}]}


def test_plan_uses_existing_backend_and_respects_timeline(monkeypatch, tmp_path):
    def call(system, user, validate, **kwargs):
        assert "source='web'" in system and "[4.0]רובוט" in user
        assert "at most 2" in system
        return validate({"cutaways": [beat(), {**beat(), "start": 5, "end": 10}]})
    monkeypatch.setattr(sb, "call_claude_json", call)
    plan = sb.plan_cutaways(doc(tmp_path)["clips"][0], web=True)
    assert len(plan) == 1 and plan[0]["duration"] == 4
    def empty(system, user, validate, **kwargs):
        return validate({"cutaways": [{**beat(), "source": "original"}]})
    monkeypatch.setattr(sb, "call_claude_json", empty)
    assert sb.plan_cutaways(doc(tmp_path)["clips"][0], web=True) == []


def test_plan_multispan_bounds_and_invalid_data(monkeypatch):
    clip = {"segments": [{"start": 0, "end": 6, "words": []}, {"start": 30, "end": 40, "words": []}]}
    def call(system, user, validate, **kwargs):
        with pytest.raises(sb.GenerationError):
            validate({"cutaways": [{"start": "bad"}]})
        return validate({"cutaways": [{**beat(), "span": 1, "start": 3, "end": 20}]})
    monkeypatch.setattr(sb, "call_claude_json", call)
    assert sb.plan_cutaways(clip, web=True)[0]["end"] == 7.5


def test_spec_reuses_saved_plan_and_asset(monkeypatch, tmp_path):
    d = doc(tmp_path)
    planned, found = [], []
    def plan(*a, **k):
        planned.append(1)
        return [beat()]
    asset = tmp_path / "cutaway.mp4"
    asset.write_bytes(b"normalized")
    def find(*a, **k):
        found.append(1)
        return {"video": str(asset), "fit": "contain", "source": {"provider": "fake"}}
    monkeypatch.setattr(sb, "plan_cutaways", plan)
    monkeypatch.setattr(fs, "find_footage", find)
    path = tmp_path / "clips.json"
    words = d["clips"][0]["words"].copy()
    assert sb.add_web_cutaways(d, str(path)) == 1
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == 1 and saved["clips"][0]["words"] == words
    assert sb.add_web_cutaways(saved, str(path)) == 0
    assert len(planned) == len(found) == 1
    asset.unlink()
    assert sb.add_web_cutaways(saved, str(path)) == 1  # missing asset re-resolves
    assert len(found) == 2


@pytest.mark.parametrize("generated,image_fails,expected", [(False, False, 0), (True, False, 1), (True, True, 0)])
def test_fallback_web_to_generated_to_original(monkeypatch, tmp_path, generated, image_fails, expected):
    d = doc(tmp_path)
    monkeypatch.setattr(sb, "plan_cutaways", lambda *a, **k: [beat()])
    monkeypatch.setattr(fs, "find_footage", lambda *a, **k: None)
    def image(prompt, style, sheet, path):
        if image_fails:
            raise RuntimeError("no provider")
        path.write_bytes(b"image")
    monkeypatch.setattr(sb, "_scene_image", image)
    assert sb.add_web_cutaways(d, str(tmp_path / "spec.json"), generated=generated) == expected
    assert len(d["clips"][0]["cutaways"]) == expected


def test_failed_planning_preserves_manual_cutaways(monkeypatch, tmp_path):
    d = doc(tmp_path)
    original = {"start": 1, "end": 2, "image": "local.png"}
    d["clips"][0]["cutaways"] = [original]
    def fail(*a, **k):
        raise TimeoutError()
    monkeypatch.setattr(sb, "plan_cutaways", fail)
    sb.add_web_cutaways(d, str(tmp_path / "spec.json"))
    assert d["clips"][0]["cutaways"] == [original]


def test_cli_opt_in_and_legacy_render_from(monkeypatch, tmp_path):
    path = tmp_path / "clips.json"
    path.write_text(json.dumps(doc(tmp_path)))
    enhancements = []
    monkeypatch.setattr(sb, "add_web_cutaways", lambda *a, **k: enhancements.append(k) or 0)
    monkeypatch.setattr(render, "render_clips", lambda *a, **k: ["clip.mp4"])
    import sofit.transcribe as tx
    def forbidden(*a, **k):
        raise AssertionError("render-from must not transcribe")
    monkeypatch.setattr(tx, "transcribe", forbidden)
    assert cli.main(["--render-from", str(path)]) == 0
    assert enhancements == []
    assert cli.main(["--render-from", str(path), "--web-cutaways-safe-only", "--cutaways"]) == 0
    assert enhancements[0]["safe_only"] and enhancements[0]["generated"]
    assert cli.main(["--render-from", str(path), "--web-cutaways", "--storyboard"]) == 1
    assert cli.main(["episode.mp4", "--web-cutaways"]) == 1


def test_video_only_asset_and_contain_composition(monkeypatch, tmp_path):
    asset = tmp_path / "video.mp4"
    asset.write_bytes(b"video")
    cutaways = [{"start": 3, "end": 7, "video": str(asset), "fit": "contain"}]
    assert render._usable_cutaways(cutaways) == cutaways
    assert render._usable_cutaways([{**cutaways[0], "asset_sha256": "incorrect"}]) == []
    assert render._usable_cutaways([{**cutaways[0], "start": float("nan")}]) == []
    commands = []
    monkeypatch.setattr(render, "_run_ffmpeg", lambda cmd: commands.append(cmd))
    render.extract_clip(tmp_path / "source.mp4", 0, 12, tmp_path / "out.mp4", cutaways=cutaways)
    command = commands[0]
    filters = command[command.index("-filter_complex") + 1]
    assert "force_original_aspect_ratio=decrease" in filters and "pad=1080:1920" in filters
    assert command[command.index("-map", command.index("-map") + 1) + 1] == "0:a?"
    assert "between(t,3.0,7.0)" in filters
    # The same helper works on an audio-only podcast's canvas.
    command, _ = render._audiogram_cmd(tmp_path / "episode.mp3", 0, 12, 1080, 1920,
                                      "bg.png", None, None, "top-left", "none", 1,
                                      render._AUDIO_LIMITER, tmp_path / "out.mp4", cutaways=cutaways)
    assert str(asset) in command and "[a_raw]" in command[command.index("-filter_complex") + 1]


def test_composition_failure_retries_recording_and_no_false_credits(monkeypatch, tmp_path):
    d = doc(tmp_path)
    asset = tmp_path / "video.mp4"
    asset.write_bytes(b"video")
    d["clips"][0].update(focus=0.5, footage_coverage=90, footage_coverage_report={},
                         cutaways=[{"start": 4, "end": 8, "video": str(asset),
                                            "source": {"provider": "fake"}}])
    monkeypatch.setattr(render, "_is_audio_only", lambda *a: False)
    monkeypatch.setattr(render, "_apply_brand_overlays", lambda *a: None)
    calls = []
    def extract(**kwargs):
        calls.append(kwargs)
        if kwargs["cutaways"]:
            raise RuntimeError("bad external codec")
        kwargs["output_path"].write_bytes(b"rendered original")
    monkeypatch.setattr(render, "extract_clip", extract)
    result = render.render_clips(d["source"]["video"], d["clips"], str(tmp_path))
    assert len(calls) == 2 and calls[0]["cutaways"] and not calls[1]["cutaways"]
    assert Path(result[0]).read_bytes() == b"rendered original"
    assert not Path(result[0]).with_suffix(".sources.json").exists()
    report = json.loads(Path(result[0]).with_suffix(".coverage.json").read_text())
    assert report["achieved_percent"] == 0 and report["requested_percent"] == 90
    assert d["clips"][0]["footage_coverage_report"] == report


def test_safe_only_rechecks_saved_assets_even_when_planning_fails(monkeypatch, tmp_path):
    d = doc(tmp_path)
    d['clips'][0]['cutaways'] = [{'start': 4, 'end': 8, 'video': 'old.mp4',
                                 'source': {'license': 'unknown'}}]
    def fail(*a, **k):
        raise sb.GenerationError('backend unavailable')
    monkeypatch.setattr(sb, 'plan_cutaways', fail)
    assert sb.add_web_cutaways(d, str(tmp_path / 'clips.json'), safe_only=True) == 0
    assert d['clips'][0]['cutaways'] == []


def test_saved_original_beat_never_generates_art(monkeypatch, tmp_path):
    d = doc(tmp_path)
    d['clips'][0]['visual_plan'] = [{**beat(), 'source': 'original'}]
    def forbidden(*a, **k):
        raise AssertionError('original beat must remain original')
    monkeypatch.setattr(fs, 'find_footage', forbidden)
    monkeypatch.setattr(sb, '_scene_image', forbidden)
    assert sb.add_web_cutaways(d, str(tmp_path / 'clips.json'), generated=True) == 0
    assert not d['clips'][0]['cutaways']


def test_missing_art_prompt_uses_visual_intent_for_enabled_fallback(monkeypatch, tmp_path):
    d = doc(tmp_path)
    d['clips'][0]['visual_plan'] = [{**beat(), 'prompt': ''}]
    monkeypatch.setattr(fs, 'find_footage', lambda *a, **k: None)
    prompts = []
    def image(prompt, style, sheet, path):
        prompts.append(prompt)
        path.write_bytes(b'image')
    monkeypatch.setattr(sb, '_scene_image', image)
    assert sb.add_web_cutaways(d, str(tmp_path / 'clips.json'), generated=True) == 1
    assert prompts == [beat()['intent']]


@pytest.mark.parametrize('field,value', [('start', float('nan')), ('end', float('inf'))])
def test_web_plan_rejects_nonfinite_times_before_clamping(monkeypatch, tmp_path, field, value):
    monkeypatch.setattr(sb, 'call_claude_json', lambda system, user, validate, **k:
                        validate({'cutaways': [{**beat(), field: value}]}))
    assert sb.plan_cutaways(doc(tmp_path)['clips'][0], web=True) == []
