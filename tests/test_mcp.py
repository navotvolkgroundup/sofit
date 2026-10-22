"""MCP server tests. Skipped if the optional `mcp` dependency isn't installed."""

import pytest

pytest.importorskip("mcp")

from sofit import mcp_server as m  # noqa: E402


def test_generate_kit_errors_when_not_transcribed(tmp_path):
    # No cached transcript and a nonexistent file -> clear error, no Claude call.
    res = m.generate_kit(str(tmp_path / "missing.mp3"))
    assert "error" in res and "transcribe" in res["error"]


def test_status_not_started_for_unknown(tmp_path):
    res = m.transcription_status(str(tmp_path / "missing.mp3"))
    assert res["status"] == "not_started"


def test_status_ready_when_cached(monkeypatch, tmp_path):
    # A real file that exists + stubbed cache lookup -> 'ready', no real transcript.
    f = tmp_path / "ep.mp3"
    f.write_bytes(b"x")
    monkeypatch.setattr(m.transcribe, "cached_segments", lambda *a, **k: [object()])
    assert m.transcription_status(str(f))["status"] == "ready"


def test_render_clips_dry_run_does_not_render(monkeypatch, tmp_path):
    f = tmp_path / "ep.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(m.transcribe, "cached_segments", lambda *a, **k: [object()])
    clip = {"id": "clip-1", "start": 0.0, "end": 2.0, "words": [
        {"t": 0.0, "d": 0.4, "w": "שלום"},
        {"t": 0.5, "d": 0.4, "w": "אממ"},
        {"t": 1.2, "d": 0.4, "w": "עולם"},
    ]}
    monkeypatch.setattr(m.generate, "make_clips", lambda *a, **k: [clip])
    import sofit.render as r

    def boom(*a, **k):
        raise AssertionError("dry-run must not render")
    monkeypatch.setattr(r, "render_clips", boom)
    res = m.render_clips(str(f), str(tmp_path / "out"), defiller_dry_run=True)
    assert res["dry_run"] is True
    assert "אממ" in res["report"]
    assert any(h["text"] == "אממ" and h["applied"] for row in res["spans"] for h in row["hits"])


def test_render_clips_passes_defiller_flag(monkeypatch, tmp_path):
    f = tmp_path / "ep.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(m.transcribe, "cached_segments", lambda *a, **k: [object()])
    monkeypatch.setattr(m.generate, "make_clips", lambda *a, **k: [
        {"id": "clip-1", "start": 0, "end": 1, "words": []},
    ])
    import sofit.render as r
    seen = {}
    monkeypatch.setattr(r, "render_clips",
                        lambda *a, **k: (seen.update(k) or ["out/clip-1.mp4"]))
    res = m.render_clips(str(f), str(tmp_path / "out"), defiller=False)
    assert res["clips"] == 1
    assert seen["defiller"] is False


def test_tools_registered():
    # The tools are exposed on the FastMCP instance.
    import asyncio

    names = {t.name for t in asyncio.run(m.mcp.list_tools())}
    assert {"transcribe_episode", "transcription_status", "generate_kit",
            "render_clips", "correct_clip"} <= names


# --- correct_clip -------------------------------------------------------

def _clips_doc(tmp_path):
    import json
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"x")
    doc = {"schema_version": 1, "source": {"video": str(video)}, "clips": [
        {"id": "clip-1", "words": [{"t": 0, "d": 0.3, "w": "קטאר"}]},
        {"id": "clip-2", "words": [{"t": 0, "d": 0.3, "w": "שלום"}]},
        {"id": "clip-3", "words": [{"t": 0, "d": 0.3, "w": "קטאר"}]},
    ]}
    p = tmp_path / "clips.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _stub_render(monkeypatch):
    import sofit.render as r
    calls = {}
    monkeypatch.setattr(r, "render_clips",
                        lambda v, clips, out, aspect="9:16", **k:
                        (calls.__setitem__("clips", clips)
                         or [f"{out}/{c['id']}.mp4" for c in clips]))
    return calls


def test_correct_clip_episode_wide(monkeypatch, tmp_path):
    import json
    p = _clips_doc(tmp_path)
    calls = _stub_render(monkeypatch)
    res = m.correct_clip(str(p), "קטאר", "Qatar")
    assert res["n_replaced"] == 2
    assert set(res["clips_affected"]) == {"clip-1", "clip-3"}
    assert {c["id"] for c in calls["clips"]} == {"clip-1", "clip-3"}  # only affected rendered
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["clips"][0]["words"][0]["w"] == "Qatar"  # persisted


def test_correct_clip_scoped_to_one(monkeypatch, tmp_path):
    import json
    p = _clips_doc(tmp_path)
    _stub_render(monkeypatch)
    res = m.correct_clip(str(p), "קטאר", "Qatar", clip_id="clip-3")
    assert res["clips_affected"] == ["clip-3"]
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["clips"][0]["words"][0]["w"] == "קטאר"  # clip-1 untouched


def test_correct_clip_not_found_no_render_no_write(monkeypatch, tmp_path):
    import sofit.render as r
    p = _clips_doc(tmp_path)
    orig = p.read_text(encoding="utf-8")
    flag = {"rendered": False}
    monkeypatch.setattr(r, "render_clips",
                        lambda *a, **k: (flag.__setitem__("rendered", True) or []))
    res = m.correct_clip(str(p), "xyz", "Q")
    assert res["n_replaced"] == 0
    assert flag["rendered"] is False           # never rendered
    assert p.read_text(encoding="utf-8") == orig  # never written


def test_correct_clip_render_failure_keeps_file(monkeypatch, tmp_path):
    import sofit.render as r
    p = _clips_doc(tmp_path)
    orig = p.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(r, "render_clips", boom)
    res = m.correct_clip(str(p), "קטאר", "Qatar")
    assert "error" in res and "unchanged" in res["error"]
    assert p.read_text(encoding="utf-8") == orig  # render-first: file intact
