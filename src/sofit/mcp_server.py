"""MCP server — run the episode kit from any MCP-capable AI app.

Transcription is ~real-time (tens of minutes for a full episode), far longer
than an MCP call should block. So the pipeline is split across tools: start
transcription in the background, poll its status, then generate the (fast) kit
once the transcript is cached.

Run:  sofit-mcp        (stdio transport)
Wire it into Claude Desktop / Claude Code as an MCP server (see README).
"""

from __future__ import annotations

import os
import threading

from mcp.server.fastmcp import FastMCP

from . import format as fmt
from . import generate, transcribe

mcp = FastMCP("sofit")

# cache_key -> {"status": "running"|"done"|"error", "error": str|None}
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _key(path: str, model: str) -> str:
    return transcribe.cache_key(path, model or transcribe.DEFAULT_MODEL, "he", "int8")


@mcp.tool()
def transcription_status(path: str, model: str = "") -> dict:
    """Is the transcript for this media file ready? Returns status:
    'ready' | 'running' | 'not_started' | 'error'."""
    if not os.path.exists(path):
        return {"status": "not_started", "error": f"file not found: {path}"}
    if transcribe.cached_segments(path, model or transcribe.DEFAULT_MODEL) is not None:
        return {"status": "ready"}
    with _lock:
        job = _jobs.get(_key(path, model))
    if job:
        return {"status": job["status"], "error": job.get("error")}
    return {"status": "not_started"}


@mcp.tool()
def transcribe_episode(path: str, model: str = "") -> dict:
    """Start transcribing a media file (mp3/mp4) in the background, or report that
    it is already cached. Poll transcription_status until it is 'ready', then call
    generate_kit. Transcription can take tens of minutes for a full episode."""
    if not os.path.exists(path):
        return {"status": "error", "error": f"file not found: {path}"}
    if transcribe.cached_segments(path, model or transcribe.DEFAULT_MODEL) is not None:
        return {"status": "ready", "message": "Transcript already cached."}
    key = _key(path, model)
    with _lock:
        if _jobs.get(key, {}).get("status") == "running":
            return {"status": "running", "message": "Transcription already in progress."}
        _jobs[key] = {"status": "running", "error": None}

    def _run():
        try:
            transcribe.transcribe(path, model=model or transcribe.DEFAULT_MODEL)
            with _lock:
                _jobs[key] = {"status": "done", "error": None}
        except Exception as e:  # noqa: BLE001 - report any failure back to the client
            with _lock:
                _jobs[key] = {"status": "error", "error": str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "running", "message": "Transcription started; poll transcription_status."}


@mcp.tool()
def generate_kit(
    path: str,
    chapter_format: str = "md",
    max_chapters: int = 12,
    shownotes: bool = False,
    quotes: bool = False,
    model: str = "",
) -> dict:
    """Generate chapters (and optionally Hebrew show notes + pull-quotes) for an
    already-transcribed episode. Requires the transcript to be cached — call
    transcribe_episode first. chapter_format: md|txt|youtube|spotify|podcast.
    Needs ANTHROPIC_API_KEY in the server's environment."""
    segs = transcribe.cached_segments(path, model or transcribe.DEFAULT_MODEL)
    if segs is None:
        return {"error": "not transcribed yet — call transcribe_episode(path) first"}
    audio_end = segs[-1].end
    out: dict = {}

    chapters = generate.make_chapters(segs, max_chapters=max_chapters)
    if chapter_format in ("youtube", "spotify"):
        gap = 30.0 if chapter_format == "spotify" else 10.0
        out["chapters"] = fmt.render_chapters_youtube(chapters, audio_end, min_gap=gap) \
            or fmt.render_chapters_md(chapters)
    elif chapter_format == "podcast":
        out["chapters"] = fmt.render_chapters_podcast_json(chapters)
    else:
        out["chapters"] = fmt.render_chapters_md(chapters)

    if shownotes:
        out["shownotes"] = fmt.render_shownotes_md(generate.make_shownotes(segs))
    if quotes:
        out["quotes"] = fmt.render_quotes_md(generate.make_quotes(segs))
    return out


@mcp.tool()
def render_clips(path: str, out_dir: str, aspect: str = "9:16", model: str = "",
                 logo: str = "", logo_pos: str = "top-left",
                 hook_card: bool = True, hook_variant: int = 0,
                 defiller: bool = True, defiller_dry_run: bool = False,
                 defiller_min_gap: float = 0.40,
                 defiller_discourse: bool = False) -> dict:
    """Render each pull-quote of an already-transcribed episode to a vertical
    (default 9:16) clip with burned Hebrew captions, written to out_dir. Pass
    `logo` (a transparent PNG path) to overlay it in the `logo_pos` corner of
    every clip.

    Hesitation sounds are cut from the picture, the audio, and the karaoke by
    default: transcript tokens (אממ, אה between pauses, um, uh, hmm, and
    stretched spellings) and, where the transcript dropped them, a steady
    voiced hum inside a gap of 0.30s or more. Laughter, breaths, the other
    host, and a silent pause are left in. `defiller=false` keeps everything.
    `defiller_min_gap` (seconds, default 0.40) is the minimum speech that
    must remain between two cuts. `defiller_discourse=true` also cuts opt-in
    words such as כאילו (off by default). `defiller_dry_run` returns each
    candidate with its source time, whether it came from the word list or
    the audio, and a confidence, and does not render.

    Requires the transcript cached (call transcribe_episode first). A real
    render also needs the [render] extra and ffmpeg. Returns the output file
    paths, or the dry-run report."""
    segs = transcribe.cached_segments(path, model or transcribe.DEFAULT_MODEL)
    if segs is None:
        return {"error": "not transcribed yet — call transcribe_episode(path) first"}
    clips = generate.make_clips(segs, titler="api")
    from .defiller import analyze_clips, config_from_env, format_spans
    cfg = config_from_env(defiller_min_gap, discourse=defiller_discourse)
    if defiller_dry_run:
        spans = analyze_clips(clips, cfg, media=path)
        return {
            "dry_run": True,
            "defiller": defiller,
            "report": format_spans(spans),
            "spans": spans,
        }
    try:
        from . import render
    except ImportError:
        return {"error": "install the render extra: pip install 'sofit-cli[render]'"}
    outs = render.render_clips(path, clips, out_dir, aspect=aspect,
                               logo=logo or None, logo_pos=logo_pos,
                               hook_card=hook_card, hook_variant=hook_variant,
                               defiller=defiller, defiller_cfg=cfg)
    result = {"clips": len(outs), "files": outs}
    # This path regenerates clips from the transcript, so it ignores any caption
    # corrections saved in a clips.json. Flag it so the fix isn't silently lost.
    stem = os.path.splitext(path)[0]
    if os.path.exists(stem + ".clips.json"):
        result["note"] = ("a clips.json exists for this media; render_clips regenerates "
                          "from the transcript and ignores caption corrections — use "
                          "correct_clip / render from the clips.json to keep them.")
    return result


@mcp.tool()
def correct_clip(clips_json: str, find: str, replace: str,
                 clip_id: str = "", aspect: str = "9:16", out_dir: str = "",
                 logo: str = "", logo_pos: str = "top-left",
                 hook_card: bool = True, hook_variant: int = 0) -> dict:
    """Fix a caption typo in a saved clips.json and re-render the affected clip(s),
    preserving per-word karaoke timing (a multi-token find like the three tokens
    'OpenAI' transcribes into collapses to one, merging their time span).

    By DEFAULT the fix applies to EVERY clip — recurring brand names show up in
    many; pass clip_id to scope to a single clip. Renders FIRST, then atomically
    rewrites clips.json only if the render succeeds, so a failure leaves the file
    intact. Needs the [render] extra + ffmpeg. Returns n_replaced, the affected
    clip ids, before/after caption text (so you can confirm), and output paths.
    """
    import json
    from pathlib import Path

    from . import corrections

    p = Path(clips_json)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"error": f"cannot read clips json {clips_json}: {e}"}
    clips = doc.get("clips") or []
    video = (doc.get("source") or {}).get("video")
    if not video or not os.path.exists(video):
        return {"error": f"source video not found: {video}"}

    cid = clip_id or None

    def _cap(clip: dict) -> str:
        return " ".join(w.get("w", "") for w in clip.get("words", []))

    before = {c.get("id"): _cap(c) for c in clips if cid is None or c.get("id") == cid}
    try:
        n, affected = corrections.correct_clips(clips, find, replace, clip_id=cid)
    except ValueError as e:
        return {"error": str(e)}
    if n == 0:
        return {"n_replaced": 0, "clips_affected": [],
                "message": f"'{find}' not found — nothing changed, clips.json untouched"}

    try:
        from . import render
    except ImportError:
        return {"error": "install the render extra: pip install 'sofit-cli[render]'"}

    out = out_dir or os.path.dirname(os.path.abspath(clips_json)) or "."
    to_render = [c for c in clips if c.get("id") in set(affected)]
    # Render FIRST — if it fails, the on-disk clips.json is never touched.
    try:
        from .defiller import config_from_env
        outs = render.render_clips(video, to_render, out, aspect=aspect,
                                   logo=logo or None, logo_pos=logo_pos,
                                   hook_card=hook_card, hook_variant=hook_variant,
                                   defiller_cfg=config_from_env())
    except Exception as e:  # noqa: BLE001 - surface render failure, keep file intact
        return {"error": f"render failed, clips.json unchanged: {e}"}

    # Render succeeded -> persist atomically (temp file + os.replace).
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)

    after = {c.get("id"): _cap(c) for c in clips if c.get("id") in set(affected)}
    return {
        "n_replaced": n,
        "clips_affected": affected,
        "before_caption": {k: before.get(k) for k in affected},
        "after_caption": after,
        "output_paths": outs,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
