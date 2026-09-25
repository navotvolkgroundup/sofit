"""Reusable source frame index and intent-scoped visual evidence, independent of beat length."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .footage import (
    FootageError,
    VisualIntent,
    _hash,
    probe_video,
    write_json,
    relevance,
    candidate_from_dict,
)
from .footage_progress import analyzed, count, stage
from .footage_selection import Frame, MIN_CONFIDENCE, _scores

VERSION = 1
EVIDENCE_VERSION = 2


def semantic_key(intent: VisualIntent):
    # Wording of the visual action and identity matters. Query, URL ordering,
    # narrative timing, duration and spoken statistics do not change visible evidence.
    return json.dumps(
        {
            "intent": intent.intent.strip().casefold(),
            "required_terms": sorted(t.casefold() for t in intent.required_terms),
        },
        sort_keys=True,
    )


def frame_index(media: Path, media_hash: str) -> list[Frame]:
    directory = media.parent / "index"
    manifest = media.parent / "index.json"
    try:
        saved = json.loads(manifest.read_text())
        if (
            saved["version"] == VERSION
            and saved["sha256"] == media_hash
            and 0 < len(saved["frames"]) <= 600
            and all(
                re.fullmatch(r"\d{6}\.jpg", item["name"])
                and math.isfinite(item["at"])
                and 0 <= item["at"] <= 600
                for item in saved["frames"]
            )
        ):
            frames = [
                Frame(item["at"], directory / item["name"]) for item in saved["frames"]
            ]
            if frames and all(
                f.path.is_file() and _hash(f.path) == item["sha256"]
                for f, item in zip(frames, saved["frames"])
            ):
                count("frame_cache_hit")
                count("cache_hit")
                return frames
    except (OSError, ValueError, KeyError, TypeError):
        pass
    count("frame_cache_miss")
    duration = probe_video(media).duration
    # One decoder process per source, bounded by the source duration/size policy.
    with (
        tempfile.TemporaryDirectory(prefix="frames-", dir=media.parent) as td,
        stage("frames"),
    ):
        temp = Path(td)
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "mov,matroska,webm,ogg,avi",
                "-i",
                str(media),
                "-an",
                "-sn",
                "-vf",
                "fps=1,scale=trunc(iw*sar/2)*2:ih,setsar=1,"
                "scale=640:640:force_original_aspect_ratio=decrease",
                "-frames:v",
                "600",
                "-q:v",
                "3",
                "-start_number",
                "0",
                str(temp / "%06d.jpg"),
            ],
            capture_output=True,
            timeout=180,
        )
        if result.returncode:
            raise FootageError("cannot index footage frames")
        items = [
            {"at": i + 0.5, "name": p.name, "sha256": _hash(p)}
            for i, p in enumerate(sorted(temp.glob("*.jpg")))
            if i + 0.5 < duration
        ]
        if not items:
            raise FootageError("source has no usable frames")
        directory.mkdir(exist_ok=True)
        for item in items:
            os.replace(temp / item["name"], directory / item["name"])
        write_json(
            manifest, {"version": VERSION, "sha256": media_hash, "frames": items}
        )
        count("frames_extracted", len(items))
    return [Frame(item["at"], directory / item["name"]) for item in items]


def evidence(
    media: Path, metadata: dict, intents: list[VisualIntent], judge, cues=None
) -> dict:
    """Batch equivalent source requests. Persist positive AND negative observations.

    Evidence stays action/identity-specific; unseen intents require new judgments,
    never lexical similarity to a caption or reuse of an unrelated confidence score.
    """
    unique = {semantic_key(i): i for i in intents}
    results, pending = {}, {}
    for key, intent in unique.items():
        identity = {
            "version": EVIDENCE_VERSION,
            "media": metadata["sha256"],
            "semantic": key,
            "judge": judge.cache_key,
        }
        filename = (
            hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            + ".json"
        )
        path = media.parent / filename
        try:
            saved = json.loads(path.read_text())
            if saved["identity"] == identity and isinstance(saved["ranges"], list):
                ranges = saved["ranges"]
                if all(
                    len(r) == 4
                    and all(math.isfinite(v) for v in r[:3])
                    and 0 <= r[0] < r[1] <= metadata["video"]["duration"]
                    and r[1] - r[0] >= 2
                    and MIN_CONFIDENCE <= r[2] <= 1
                    and isinstance(r[3], str)
                    for r in ranges
                ):
                    results[key] = ranges
                    count("analysis_cache_hit")
                    count("cache_hit")
                    continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        pending[key] = (intent, path, identity)
    if not pending or getattr(judge, "blocked", None):
        return results
    analyzed(metadata["sha256"])
    frames = frame_index(media, metadata["sha256"])
    coarse_indices = sorted({round(i * (len(frames) - 1) / 23) for i in range(24)})
    if cues:
        timed = cues() if callable(cues) else cues
        guided = []
        for intent, _, _ in pending.values():
            matches = sorted(
                (
                    c
                    for c in timed
                    if math.isfinite(c.start)
                    and math.isfinite(c.end)
                    and 0 <= c.start < c.end <= len(frames)
                ),
                key=lambda c: -relevance(intent.intent, c.text),
            )
            for cue in matches[:3]:
                if relevance(intent.intent, cue.text) >= 0.5:
                    guided.extend(
                        [
                            max(0, min(len(frames) - 1, round(t - 0.5)))
                            for t in (cue.start, (cue.start + cue.end) / 2, cue.end)
                        ]
                    )
        if guided:
            coarse_indices = sorted(
                set(coarse_indices[::2] + list(dict.fromkeys(guided))[:12])
            )
    # Four distinct actions per model call keep JSON responses bounded as well as images.
    keys = list(pending)
    for group_start in range(0, len(keys), 4):
        group = keys[group_start : group_start + 4]
        requested = [pending[k][0] for k in group]
        observations = {k: {} for k in group}

        def score(indices):
            for start in range(0, len(indices), 25):
                selected = indices[start : start + 25]
                batch = [frames[i] for i in selected]
                with stage("judge"):
                    if metadata.get("candidate") and hasattr(judge, "score_source"):
                        scores = judge.score_source(
                            batch, requested, candidate_from_dict(metadata["candidate"])
                        )
                    elif hasattr(judge, "score_many"):
                        scores = judge.score_many(batch, requested)
                    else:
                        scores = [_scores(judge, batch, intent) for intent in requested]
                if len(scores) != len(group):
                    raise FootageError("missing source judgments")
                for k, values in zip(group, scores):
                    if len(values) != len(selected) or any(
                        not math.isfinite(s) or not 0 <= s <= 1 or not r
                        for s, r in values
                    ):
                        raise FootageError("invalid source judgments")
                    observations[k].update(zip(selected, values))

        score(coarse_indices)
        fine = set()
        windows = []
        radius = 15 if len(group) == 1 else 10
        for k in group:
            anchors = []
            for i in sorted(coarse_indices, key=lambda i: (-observations[k][i][0], i)):
                if observations[k][i][0] >= MIN_CONFIDENCE and all(
                    abs(i - a) >= 25 for a in anchors
                ):
                    anchors.append(i)
                    if len(anchors) == 3:
                        break
            windows.append(
                [
                    set(range(max(0, i - radius), min(len(frames), i + radius + 1)))
                    for i in anchors
                ]
            )
        # Keep contiguous observations. Uniformly thinning a union of distant
        # windows would leave no consecutive evidence for any action at all.
        for n in range(3):
            for candidates in windows:
                if n < len(candidates) and len(fine | candidates[n]) <= 96:
                    fine.update(candidates[n])
        missing = sorted(fine - set(coarse_indices))
        score(missing)
        for k in group:
            ranges, run = [], []
            for i in sorted(observations[k]):
                value = observations[k][i]
                if run and (i != run[-1] + 1 or value[0] < MIN_CONFIDENCE):
                    _append_range(ranges, run, frames, observations[k])
                    run = []
                if value[0] >= MIN_CONFIDENCE:
                    run.append(i)
            _append_range(ranges, run, frames, observations[k])
            _, path, identity = pending[k]
            write_json(
                path,
                {
                    "identity": identity,
                    "ranges": ranges,
                    "observations": {str(i): v for i, v in observations[k].items()},
                },
            )
            results[k] = ranges
    return results


def _append_range(ranges, run, frames, observations):
    if len(run) >= 3:
        ranges.append(
            [
                frames[run[0]].at,
                frames[run[-1]].at,
                min(observations[i][0] for i in run),
                observations[run[len(run) // 2]][1],
            ]
        )


def unused_ranges(ranges, used):
    available = []
    for start, end, confidence, reason in ranges:
        intervals = [(start, end)]
        for a, b in used:
            intervals = [
                (x, y)
                for s, e in intervals
                for x, y in [(s, min(e, a)), (max(s, b), e)]
                if y > x
            ]
        available.extend(
            (s, e, confidence, reason) for s, e in intervals if e - s >= 2 - 1e-6
        )
    return sorted(available, key=lambda r: (-(r[1] - r[0]), r[0]))
