"""Offline 3-clip cold/warm benchmark. Real FFmpeg; replay network and pixel judge.

Run with the local editable Python. No account, internet or episode media needed.
The fixture deliberately has several short relevant scenes, separated by irrelevant
ones: a long beat must combine verified excerpts, never bridge the red gaps.
"""

import argparse
import copy
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from sofit import footage as f, footage_pipeline as pipeline
from sofit import footage_progress as progress, storyboard as sb, render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.out
    if root.exists() and list(root.iterdir()):
        parser.error("--out must be empty")
    root.mkdir(parents=True, exist_ok=True)
    media = root / "source.mp4"
    audio = root / "episode.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=320x240:r=30:d=60",
            "-vf",
            "drawbox=color=green:t=fill:enable='between(t,2,10)+between(t,14,22)+between(t,26,38)+between(t,42,58)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(media),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=16",
            "-y",
            str(audio),
        ],
        check=True,
    )
    candidate = f.Candidate(
        "fixture",
        "https://example.org/robot",
        "https://example.org/robot.mp4",
        "Unitree robot running",
        60,
        320,
        240,
        creator="Unitree",
    )

    class Provider:
        name = "fixture"

        def search(self, query, limit=8):
            return [candidate]

        def subtitles(self, c):
            return []

    class Judge:
        cache_key = "benchmark-green-v1"

        def score(self, frames, intent):
            progress.count("judge_batches")
            result = []
            for frame in frames:
                with Image.open(frame.path) as image:
                    r, g, b = image.getpixel((10, 10))
                result.append(
                    (
                        0.95 if g > r else 0.1,
                        "green action" if g > r else "unrelated red",
                    )
                )
            return result

        def score_many(self, frames, intents):
            result = self.score(frames, intents)
            return [result for _ in intents]

    def download(url, timeout=30):
        data = io.BytesIO(media.read_bytes())
        data.headers = {}
        return data

    clips = []
    for name, timings in [
        ("zero", [(0, 12)]),
        ("partial", [(0, 4), (4, 16)]),
        ("shared", [(0, 12)]),
    ]:
        clips.append(
            {
                "id": name,
                "start": 0,
                "end": timings[-1][1],
                "footage_coverage": 90,
                "words": [
                    {"t": i, "d": 0.9, "w": "בדיקה"} for i in range(timings[-1][1])
                ],
                "visual_plan": [
                    {
                        "span": 0,
                        "start": s,
                        "end": e,
                        "source": "web",
                        "query": "Unitree robot running",
                        "intent": "Unitree robot running",
                    }
                    for s, e in timings
                ],
            }
        )
    reports = []
    for engine in ["legacy", "source"]:
        cache = f.FootageCache(root / engine / "cache")
        for temperature in ["cold", "warm"]:
            run = root / engine / temperature
            run.mkdir(parents=True)
            doc = {"source": {"video": str(audio)}, "clips": copy.deepcopy(clips)}
            telemetry = progress.Progress(path=run / "events.jsonl")

            def ready(clip):
                render.render_clips(
                    str(audio), [clip], str(run / "out"), hook_card=False
                )
                progress.count("rendered")
                progress.count("clips_done")

            with (
                patch.object(f, "open_url", download),
                patch.object(render, "_target_resolution", lambda *a: (180, 320)),
                telemetry.active(),
            ):
                progress.event("start", clips_total=3, beats_total=4)
                if engine == "legacy":
                    sb.add_web_cutaways(
                        doc,
                        str(run / "clips.json"),
                        providers=[Provider()],
                        cache=cache,
                        judge=Judge(),
                        on_clip=ready,
                    )
                else:
                    with pipeline.FootageSession(
                        cache, Judge(), providers=[Provider()]
                    ) as session:
                        sb.add_web_cutaways(
                            doc, str(run / "clips.json"), session=session, on_clip=ready
                        )
                progress.event("complete")
            report = {
                "engine": engine,
                "temperature": temperature,
                **telemetry.snapshot(),
                "coverage": [
                    c["footage_coverage_report"]["achieved_percent"]
                    for c in doc["clips"]
                ],
                "note": "Offline replay; real FFmpeg. judge_batches are fixture calls, not live Claude calls.",
            }
            (run / "report.json").write_text(json.dumps(report, indent=2))
            reports.append(report)
    (root / "results.json").write_text(json.dumps(reports, indent=2))
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
