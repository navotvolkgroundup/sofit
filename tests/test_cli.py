"""CLI --render-from tests: render from a saved clips.json without transcribing."""

import json

from sofit import cli


def _doc(tmp_path):
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"x")
    doc = {"schema_version": 1, "source": {"video": str(video)}, "clips": [
        {"id": "clip-1", "words": [{"t": 0, "d": 0.3, "w": "א"}]},
        {"id": "clip-2", "words": [{"t": 0, "d": 0.3, "w": "ב"}]},
    ]}
    p = tmp_path / "clips.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _no_transcribe(monkeypatch):
    import sofit.transcribe as t

    def boom(*a, **k):
        raise AssertionError("transcription must not run for --render-from")
    monkeypatch.setattr(t, "transcribe", boom)


def test_render_from_skips_transcription(monkeypatch, tmp_path):
    p = _doc(tmp_path)
    _no_transcribe(monkeypatch)
    import sofit.render as r
    rendered = {}
    monkeypatch.setattr(r, "render_clips",
                        lambda v, clips, out, aspect="9:16", **k:
                        (rendered.__setitem__("clips", clips)
                         or [f"{out}/{c['id']}.mp4" for c in clips]))
    rc = cli.main(["--render-from", str(p), "--render-clips", str(tmp_path / "out")])
    assert rc == 0
    assert {c["id"] for c in rendered["clips"]} == {"clip-1", "clip-2"}


def test_render_from_only_filters_one_clip(monkeypatch, tmp_path):
    p = _doc(tmp_path)
    _no_transcribe(monkeypatch)
    import sofit.render as r
    rendered = {}
    monkeypatch.setattr(r, "render_clips",
                        lambda v, clips, out, aspect="9:16", **k:
                        (rendered.__setitem__("clips", clips) or []))
    rc = cli.main(["--render-from", str(p), "--only", "clip-2",
                   "--render-clips", str(tmp_path / "o")])
    assert rc == 0
    assert {c["id"] for c in rendered["clips"]} == {"clip-2"}


def test_render_from_only_missing_id_errors(tmp_path):
    p = _doc(tmp_path)
    assert cli.main(["--render-from", str(p), "--only", "clip-9"]) == 1


def test_media_required_without_render_from():
    assert cli.main([]) == 1


def test_missing_codex_fails_before_transcription(monkeypatch, tmp_path, capsys):
    media = tmp_path / "episode.mp3"
    media.write_bytes(b"audio")
    _no_transcribe(monkeypatch)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main([str(media), "--titler", "codex-cli"]) == 1
    assert "codex CLI not found" in capsys.readouterr().err


class _Seg:
    start = 0.0
    text = ""
    words = []
    index = 0

    def __init__(self, end):
        self.end = end


def _stub_pipeline(monkeypatch, clips):
    """Stub transcription + generation + render so --render-clips runs offline."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    import sofit.generate as g
    import sofit.render as r
    import sofit.transcribe as t
    monkeypatch.setattr(t, "transcribe", lambda *a, **k: [_Seg(1.0)])
    monkeypatch.setattr(g, "make_chapters", lambda *a, **k: [])  # default output; keep it cheap
    monkeypatch.setattr(g, "make_clips", lambda *a, **k: clips)
    rendered = {}
    monkeypatch.setattr(r, "render_clips",
                        lambda v, c, out, aspect="9:16", **k:
                        (rendered.__setitem__("clips", c)
                         or rendered.update(k)
                         or [f"{out}/{x['id']}.mp4" for x in c]))
    return rendered


def test_render_clips_autowrites_default_spec(monkeypatch, tmp_path):
    media = tmp_path / "WS203_EDIT.mp4"
    media.write_bytes(b"x")
    clips = [{"id": "clip-1", "start": 0, "end": 1, "hook": "h",
              "focus": None, "words": [{"t": 0, "d": 0.3, "w": "א"}]}]
    rendered = _stub_pipeline(monkeypatch, clips)
    out = tmp_path / "WS203"
    rc = cli.main([str(media), "--render-clips", str(out)])
    assert rc == 0
    spec = out / "WS203_EDIT.clips.json"          # derived from media stem, in render dir
    assert spec.exists()
    doc = json.loads(spec.read_text(encoding="utf-8"))
    assert doc["clips"] == clips and doc["source"]["video"] == str(media)
    assert rendered["clips"] == clips             # rendered the SAME spec it saved


def test_render_clips_keeps_existing_corrected_spec(monkeypatch, tmp_path):
    media = tmp_path / "WS203_EDIT.mp4"
    media.write_bytes(b"x")
    out = tmp_path / "WS203"
    out.mkdir()
    spec = out / "WS203_EDIT.clips.json"
    spec.write_text('{"corrected":true}', encoding="utf-8")  # pretend user corrected it
    _stub_pipeline(monkeypatch, [{"id": "clip-1", "words": []}])
    rc = cli.main([str(media), "--render-clips", str(out)])
    assert rc == 0
    assert spec.read_text(encoding="utf-8") == '{"corrected":true}'  # NOT clobbered


def test_logo_flag_passes_through_to_render(monkeypatch, tmp_path):
    media = tmp_path / "WS203_EDIT.mp4"
    media.write_bytes(b"x")
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"x")
    rendered = _stub_pipeline(monkeypatch, [{"id": "clip-1", "words": []}])
    rc = cli.main([str(media), "--render-clips", str(tmp_path / "o"),
                   "--logo", str(logo), "--logo-pos", "top-right"])
    assert rc == 0
    assert rendered["logo"] == str(logo)
    assert rendered["logo_pos"] == "top-right"
