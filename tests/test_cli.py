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


def _filler_doc(tmp_path):
    import json
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"x")
    doc = {"schema_version": 1, "source": {"video": str(video)}, "clips": [{
        "id": "clip-1", "start": 12.0, "end": 15.0, "hook": "h",
        "words": [
            {"t": 0.00, "d": 0.40, "w": "אז"},
            {"t": 0.50, "d": 0.40, "w": "אממ"},
            {"t": 1.00, "d": 0.28, "w": "אם"},
            {"t": 1.32, "d": 0.30, "w": "אתה"},
            {"t": 1.66, "d": 0.40, "w": "רוצה"},
        ],
    }]}
    p = tmp_path / "clips.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def test_defiller_dry_run_reports_without_rendering(monkeypatch, tmp_path, capsys):
    p = _filler_doc(tmp_path)
    _no_transcribe(monkeypatch)
    import sofit.render as r

    def boom(*a, **k):
        raise AssertionError("dry-run must not render")
    monkeypatch.setattr(r, "render_clips", boom)
    rc = cli.main(["--render-from", str(p), "--defiller-dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "אממ" in out and "12.500" in out
    assert any(ln.strip().startswith("cut") and "אממ" in ln.split() for ln in out.splitlines())
    assert any(ln.strip().startswith("keep") and "אם" in ln.split() for ln in out.splitlines())


def test_defiller_dry_run_reports_an_audio_hum(monkeypatch, tmp_path, capsys):
    # No filler tokens. The hum is only in the audio, which is the Weekly
    # Sync case: ivrit-ai writes none of them.
    import math
    import struct
    import wave
    sr = 8000
    dur = 2.4
    samples = [0.0] * int(sr * dur)
    hum = [0.45 * math.sin(2 * math.pi * 200 * i / sr) for i in range(int(sr * 0.40))]
    at = int(0.70 * sr)
    samples[at:at + len(hum)] = hum
    media = tmp_path / "ep.wav"
    with wave.open(str(media), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples))
    doc = {"schema_version": 1, "source": {"video": str(media)}, "clips": [{
        "id": "clip-1", "start": 0.0, "end": dur,
        "words": [
            {"t": 0.00, "d": 0.30, "w": "שלום"},
            {"t": 1.70, "d": 0.40, "w": "עולם"},
        ],
    }]}
    p = tmp_path / "clips.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    _no_transcribe(monkeypatch)
    import sofit.render as r

    def boom(*a, **k):
        raise AssertionError("dry-run must not render")
    monkeypatch.setattr(r, "render_clips", boom)
    rc = cli.main(["--render-from", str(p), "--defiller-dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert any(ln.strip().startswith("cut") and "audio" in ln.split()
               for ln in out.splitlines()), out


def test_discourse_flag_is_opt_in(monkeypatch, tmp_path, capsys):
    doc = {"schema_version": 1, "source": {"video": str(tmp_path / "missing.mp4")},
           "clips": [{
               "id": "clip-1", "start": 0.0, "end": 1.5,
               "words": [
                   {"t": 0.00, "d": 0.40, "w": "זה"},
                   {"t": 0.50, "d": 0.35, "w": "כאילו"},
                   {"t": 1.00, "d": 0.40, "w": "ברור"},
               ],
           }]}
    p = tmp_path / "clips.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    _no_transcribe(monkeypatch)
    rc = cli.main(["--render-from", str(p), "--defiller-dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert not any(ln.strip().startswith("cut") and "כאילו" in ln.split()
                   for ln in out.splitlines())
    rc = cli.main(["--render-from", str(p), "--defiller-dry-run",
                   "--defiller-discourse"])
    assert rc == 0
    out = capsys.readouterr().out
    assert any(ln.strip().startswith("cut") and "כאילו" in ln.split()
               for ln in out.splitlines())


def test_skip_flag_and_file_veto_one_cut(monkeypatch, tmp_path, capsys):
    p = _filler_doc(tmp_path)
    _no_transcribe(monkeypatch)
    rc = cli.main(["--render-from", str(p), "--defiller-dry-run",
                   "--defiller-skip", "12.50"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "id=0:12.50" in out
    assert any(ln.strip().startswith("keep") and "אממ" in ln.split()
               for ln in out.splitlines())
    assert not any(ln.strip().startswith("cut") and "אממ" in ln.split()
                   for ln in out.splitlines())

    ids = tmp_path / "skip.txt"
    ids.write_text("# from the dry run\n0:12.50\n", encoding="utf-8")
    rc = cli.main(["--render-from", str(p), "--defiller-dry-run",
                   "--defiller-skip", str(ids)])
    assert rc == 0
    out = capsys.readouterr().out
    assert any(ln.strip().startswith("keep") and "אממ" in ln.split()
               for ln in out.splitlines())


def test_elongation_flag_is_opt_in(monkeypatch, tmp_path, capsys):
    import math
    import struct
    import wave
    sr = 8000
    dur = 2.4
    samples = [0.0] * int(sr * dur)
    hum = [0.45 * math.sin(2 * math.pi * 200 * i / sr) for i in range(int(sr * 0.40))]
    at = int(0.30 * sr)  # flush with the end of "מה"
    samples[at:at + len(hum)] = hum
    media = tmp_path / "ep.wav"
    with wave.open(str(media), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples))
    doc = {"schema_version": 1, "source": {"video": str(media)}, "clips": [{
        "id": "clip-1", "start": 0.0, "end": dur,
        "words": [
            {"t": 0.00, "d": 0.30, "w": "מה"},
            {"t": 1.80, "d": 0.40, "w": "עולם"},
        ],
    }]}
    p = tmp_path / "clips.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    _no_transcribe(monkeypatch)
    rc = cli.main(["--render-from", str(p), "--defiller-dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert any(ln.strip().startswith("keep") and "elongation" in ln.split()
               for ln in out.splitlines()), out
    rc = cli.main(["--render-from", str(p), "--defiller-dry-run",
                   "--defiller-elongation"])
    assert rc == 0
    out = capsys.readouterr().out
    assert any(ln.strip().startswith("cut") and "elongation" in ln.split()
               for ln in out.splitlines()), out


def test_no_defiller_flag_reaches_render(monkeypatch, tmp_path):
    p = _filler_doc(tmp_path)
    _no_transcribe(monkeypatch)
    import sofit.render as r
    rendered = {}
    monkeypatch.setattr(r, "render_clips",
                        lambda v, clips, out, aspect="9:16", **k:
                        (rendered.update(k) or []))
    rc = cli.main(["--render-from", str(p), "--no-defiller",
                   "--render-clips", str(tmp_path / "o")])
    assert rc == 0
    assert rendered["defiller"] is False


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
