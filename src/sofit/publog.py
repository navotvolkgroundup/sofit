"""`sofit publish-log` - record a posted clip at publish time.

Creates the attribution join key (clip <-> hook variant <-> platform <-> post
URL) that the metrics scraper fills in later. Rows append to the same
performance log the clip selector learns from (`generate.PERF_LOG`); a row
written here carries no metrics yet, which is safe by construction -
`generate._perf_rows()` only counts rows that already have views/retention.

Usage:
    sofit publish-log clips/clip-5.mp4 --episode WS205 --platform tiktok \
        --url https://www.tiktok.com/@show/video/123
    sofit publish-log clip-5.hook1.mp4 --episode WS205 --platform instagram \
        --url https://www.instagram.com/reel/abc/ --spec ep.clips.json

The hook variant is inferred from the rendered filename (the renderer encodes
it): `clip-5.mp4` = primary hook (variant 0), `clip-5.hookN.mp4` = variant N.
The hook TEXT comes from the clips.json spec (--spec, or auto-discovered next
to the file), or --hook as an override.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _ab_tags(name: str) -> list[str]:
    """Arbitrary A/B arms carried in the filename as `.ab-<slug>` segments.

    Navot's standing rule (2026-09-23): every non-bug comment becomes a test.
    Only two knobs used to be measurable - the hook variant number and the
    persistent-card style - because those were the only things the filename
    encoded, so ideas like "smaller captions" or "no karaoke highlight" could
    be shipped but never settled. A free-form tag costs nothing here and the
    scraper's _cells() already groups by any row key.
    """
    return re.findall(r"\.ab-([a-z0-9]+(?:-[a-z0-9]+)*)", Path(name).name.lower())


def _clip_and_variant(name: str) -> tuple[str, int]:
    """`clip-5` -> (clip-5, 0); `clip-5.hook2.mp4` -> (clip-5, 2).
    Renamed copies keep working: `WS205_clip-5.hook1.mp4` -> (clip-5, 1)."""
    stem = Path(name).name
    stem = re.sub(r"\.mp4$", "", stem)
    stem = re.sub(r"\.pers$", "", stem)  # persistent-card style marker
    stem = re.sub(r"\.ab-[a-z0-9-]+", "", stem, flags=re.I)  # A/B arm markers
    m = re.search(r"(clip-\d+)(?:\.hook(\d+))?$", stem)
    if m:
        return m.group(1), int(m.group(2) or 0)
    m = re.match(r"^(.*?)\.hook(\d+)$", stem)
    return (m.group(1), int(m.group(2))) if m else (stem, 0)


def _duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15)
        return round(float(out.stdout.strip()), 2)
    except Exception:  # noqa: BLE001 - duration is best-effort
        return None


def _clip_from_spec(spec_path: Path, clip_id: str) -> dict | None:
    try:
        doc = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for c in doc.get("clips", []):
        if str(c.get("id")) == clip_id:
            return c
    return None


def _hook_text(clip: dict, variant: int) -> str | None:
    if variant == 0:
        return clip.get("hook")
    alts = clip.get("hook_variants") or []
    return alts[variant - 1] if variant <= len(alts) else clip.get("hook")


def _hook_type_text(clip: dict, variant: int) -> str | None:
    """Opening label for the line that was actually posted.

    Variant 0 is the primary hook. A later variant uses hook_variant_types
    when that list was stored, and otherwise stays unlabeled — copying the
    primary's type onto a different line would pollute the grouping.
    """
    if variant == 0:
        raw = clip.get("hook_type")
    else:
        types = clip.get("hook_variant_types") or []
        raw = types[variant - 1] if variant <= len(types) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _spec_hook_type(clip: dict | None, variant: int, posted_hook: str | None) -> str | None:
    """The spec's label, but only when the line being logged IS the labelled line.

    --hook replaces the text without replacing the label, so a hand-written
    opening would otherwise be filed under the type of the line it replaced
    and land in the wrong by_hook_type bucket. Same reasoning as
    :func:`_hook_type_text` applies to variants.
    """
    from . import generate

    if not clip:
        return None
    raw = _hook_type_text(clip, variant)
    if not raw:
        return None
    if posted_hook is not None:
        labelled = (_hook_text(clip, variant) or "").strip()
        if labelled and posted_hook.strip() != labelled:
            return None
    return generate.normalize_hook_type(raw)


def hook_type_from_spec(spec_path, clip_id: str, variant: int = 0,
                        hook: str | None = None) -> str | None:
    """Canonical hook_type from a clips.json entry, or None if it has none.

    Old specs without the field return None so a log row can omit it. Pass
    `hook` to refuse the label when that is not the line the spec labelled.
    """
    return _spec_hook_type(_clip_from_spec(Path(spec_path), clip_id), variant, hook)


def _hook_from_spec(spec_path: Path, clip_id: str, variant: int) -> str | None:
    clip = _clip_from_spec(spec_path, clip_id)
    if not clip:
        return None
    return _hook_text(clip, variant)


def _clip_near_file(clip_file: Path, clip_id: str, variant: int) -> dict | None:
    """Search every *.clips.json near the rendered file; the spec that actually
    CONTAINS this clip id wins (a directory can hold several batches' specs)."""
    for d in (clip_file.parent, clip_file.parent.parent):
        if not d.exists():
            continue
        # *.clips*.json also catches versioned specs like ep.clips2.json.
        # Newest spec first: the same clip id can exist in an older batch's
        # spec with a DIFFERENT hook (bit us on WS_13.08 clips 1/5/6).
        for spec in sorted(d.glob("*.clips*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            clip = _clip_from_spec(spec, clip_id)
            if clip and _hook_text(clip, variant):
                return clip
    return None


def _hook_near_file(clip_file: Path, clip_id: str, variant: int) -> str | None:
    clip = _clip_near_file(clip_file, clip_id, variant)
    if not clip:
        return None
    return _hook_text(clip, variant)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="sofit publish-log",
        description="Record a posted clip so metrics can be attributed to it.")
    p.add_argument("clip", help="rendered clip file (or bare clip id like clip-5 / clip-5.hook1)")
    p.add_argument("--episode", required=True, help="episode label, e.g. WS205")
    p.add_argument("--platform", required=True,
                   choices=["tiktok", "instagram", "youtube", "linkedin", "spotify"])
    p.add_argument("--url", required=True, help="the live post URL")
    p.add_argument("--spec", help="clips.json spec (default: auto-discover next to the file)")
    p.add_argument("--hook", help="hook text override (else read from the spec)")
    p.add_argument("--hook-type", dest="hook_type",
                   help="opening type override (else read from the spec; "
                        "omitted when the spec has none, so old rows stay valid)")
    p.add_argument("--speaker", help="who fronts the clip (first face/voice on "
                   "screen) - lets the log answer 'who should open clips' with "
                   "data instead of memory")
    p.add_argument("--log", help="performance log path (default: sofit's PERF_LOG)")
    p.add_argument("--time", dest="time_local",
                   help="local posting time HH:MM (e.g. 13:00) - without it a "
                        "posting-time A/B is unmeasurable after the fact")
    args = p.parse_args(argv)

    from . import generate  # lazy: keeps --help fast

    clip_id, variant = _clip_and_variant(args.clip)
    clip_path = Path(args.clip)

    entry = _clip_from_spec(Path(args.spec), clip_id) if args.spec else None
    # A spec that names the clip but carries no hook text must not stop the
    # search; the sibling specs beside the rendered file may still have it.
    if (entry is None or not _hook_text(entry, variant)) and clip_path.exists():
        entry = _clip_near_file(clip_path, clip_id, variant) or entry

    hook = args.hook or (_hook_text(entry, variant) if entry else None)
    if not hook:
        print("error: hook text not found - pass --spec or --hook", file=sys.stderr)
        return 1

    if args.hook_type:
        try:
            hook_type = generate.parse_hook_type(args.hook_type)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    else:
        hook_type = _spec_hook_type(entry, variant, args.hook)

    row: dict = {
        "date": datetime.date.today().isoformat(),
        "episode": args.episode, "clip": clip_id, "variant": variant,
        "platform": args.platform, "hook": hook, "post_url": args.url,
    }
    if hook_type:
        row["hook_type"] = hook_type
    if ".pers" in Path(args.clip).name:
        row["hook_style"] = "persistent"
    for tag in _ab_tags(args.clip):
        # ab-<dimension>-<arm>, e.g. ab-accent-off -> row["ab_accent"] = "off".
        # A bare tag records itself, so ab-coldopen groups against untagged rows.
        dim, _, arm = tag.partition("-")
        row[f"ab_{dim}"] = arm or "on"
    if args.speaker:
        row["speaker"] = args.speaker
    if args.time_local:
        row["time_local"] = args.time_local
    if clip_path.exists():
        dur = _duration(clip_path)
        if dur:
            row["duration"] = dur

    log_path = args.log or generate.PERF_LOG
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # Duplicate guard: same URL already logged -> refuse (re-posting is a new URL).
    try:
        for line in Path(log_path).read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("post_url") == args.url:
                print(f"error: {args.url} already logged", file=sys.stderr)
                return 1
    except FileNotFoundError:
        pass
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    rows = generate._perf_rows(log_path)
    need = max(0, generate.MIN_PERF_ROWS - len(rows))
    typed = f", {hook_type}" if hook_type else ""
    print(f"logged {clip_id} (variant {variant}, {args.platform}{typed}) -> {log_path}")
    print(f"metrics pending (the scraper fills them at T+3d); "
          f"{need} more attributed row(s) until pool generation learns from data.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
