"""Record a post in the performance log the moment its uploader has a URL.

Uploading and logging were two separate steps, and nothing enforced the second.
On 2026-09-17 the WS211 batch printed `20/20 submitted` and logged NONE of
them: the episode sat with 4 rows and no speakers until it was backfilled by
hand two days later, so every "who should front clips" question silently
excluded it. CLAUDE.md already said "Never post without logging" - a rule the
tooling did not help anyone keep.

Uploaders call log() right where they print their success line. It is
best-effort by construction: any failure is reported in that line and never
breaks an upload that already succeeded.

The speaker comes from the plan post's `speaker` key. It is the field the whole
loop exists to answer, so a post without one is reported as
`autolog: logged_no_speaker` rather than passing quietly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def sofit_py_candidates() -> list[Path]:
    """Interpreters that can run `python -m sofit.cli`.

    `$SOFIT_PY` first, when it is set. Then the historical checkout venv
    (the Mac default). Then `~/tools/sofit-venv`, which is the box layout
    without a hard-coded home directory. Then the python sitting next to
    `sofit` on `PATH`, then this process. A missing override falls through.
    """
    cands: list[Path] = []
    env = os.environ.get("SOFIT_PY")
    if env:
        cands.append(Path(env).expanduser())
    cands.append(Path.home() / "src" / "hebrew-chapters" / ".venv" / "bin" / "python")
    cands.append(Path.home() / "tools" / "sofit-venv" / "bin" / "python")
    found = shutil.which("sofit")
    if found:
        cands.append(Path(found).resolve().parent / "python")
    cands.append(Path(sys.executable))
    return cands


def sofit_python() -> Path:
    """First existing interpreter from `sofit_py_candidates`."""
    cands = sofit_py_candidates()
    for cand in cands:
        if cand.is_file():
            return cand
    return cands[-1]


def _find_spec(plan: dict, plan_path: Path) -> str | None:
    """publog needs the clips.json to read the hook text."""
    if plan.get("spec"):
        return plan["spec"]
    # the batch keeps renders and the spec side by side in ~/Downloads
    for post in plan.get("posts", []):
        cover = post.get("cover")
        if cover:
            found = sorted(Path(cover).parent.glob("*.clips.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if found:
                return str(found[0])
    found = sorted(plan_path.parent.glob("*.clips.json"))
    return str(found[0]) if found else None


def episode_log_hook(clip: str, platform: str, post: dict) -> str | None:
    """Hook text for a full-episode post, or None for a regular clip.

    publish-log looks the hook up in clips.json. A full episode is not in
    that file, so the uploader passes the plan title instead of letting the
    log call fail after the upload has already succeeded.
    """
    cid = clip or ""
    if cid != "episode" and not cid.endswith("_episode"):
        return None
    if platform == "spotify":
        return post.get("spotify_title") or post.get("youtube") or "full episode"
    if platform == "youtube":
        return post.get("youtube") or post.get("spotify_title") or "full episode"
    return post.get("youtube") or post.get("spotify_title") or "full episode"


def log(plan_path, clip: str, platform: str, url: str | None) -> dict:
    """Log one posted clip. Returns a small dict to merge into the caller's
    output. Never raises."""
    try:
        if not url:
            # scheduled Instagram reels and LinkedIn posts have no URL yet
            return {"autolog": "skipped_no_url"}
        plan_path = Path(plan_path)
        plan = json.loads(plan_path.read_text())
        episode = plan.get("episode")
        if not episode:
            return {"autolog": "skipped_no_episode"}
        post = next((p for p in plan.get("posts", []) if p.get("clip") == clip), {})
        speaker = post.get("speaker")

        cmd = [str(sofit_python()), "-m", "sofit.cli", "publish-log", clip,
               "--episode", str(episode), "--platform", platform, "--url", url]
        spec = _find_spec(plan, plan_path)
        if spec:
            cmd += ["--spec", spec]
        if speaker:
            cmd += ["--speaker", speaker]
        hook = episode_log_hook(clip, platform, post)
        if hook:
            cmd += ["--hook", hook]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {"autolog": "failed",
                    "error": (r.stderr or r.stdout).strip().splitlines()[-1][:120]}
        return {"autolog": "logged" if speaker else "logged_no_speaker"}
    except Exception as exc:  # noqa: BLE001 - logging must never fail an upload
        return {"autolog": "failed", "error": f"{type(exc).__name__}: {exc}"[:120]}


def demo() -> None:
    """Self-check: the paths that must not touch the log or raise."""
    assert log("/nope/plan.json", "clip-1", "tiktok", None) == {"autolog": "skipped_no_url"}
    assert log("/nope/plan.json", "clip-1", "tiktok", "u")["autolog"] == "failed"
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "plan.json"
        p.write_text(json.dumps({"posts": [{"clip": "clip-1"}]}))
        assert log(p, "clip-1", "tiktok", "u") == {"autolog": "skipped_no_episode"}
    print("autolog demo ok")


if __name__ == "__main__":
    demo()
