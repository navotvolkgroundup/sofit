#!/usr/bin/env python
"""Candidate-clip pool + pick→clips.json helper for the sofit skill.

Run with the sofit venv python so `import sofit` resolves:
    HC=/Users/navotv/src/hebrew-chapters
    "$HC/.venv/bin/python" clips.py pool  "<episode>"
    "$HC/.venv/bin/python" clips.py build "<episode>" <pool.json> --pick 2,4,7

`pool`  : from the CACHED transcript, ask Claude (via `claude -p`, no API key) for a
          ranked pool of scroll-stopping candidate clips; writes <episode>.pool.json
          next to the episode and prints a numbered table. Candidates are NARRATIVE
          EDITS: 1-4 `beats` (kept spans — hook, escalation, payoff) whose gaps
          (banter/filler) are cut at render time, not one baggy window.
`build` : from picked candidate numbers, build a clips.json (ranges + hooks + per-word
          timings; multi-beat picks become `segments`) ready for
          `sofit --render-from`. Writes <episode>.clips.json.

Everything else (transcribe, chapters/shownotes/quotes, render, --logo, correct_clip)
is in the CLI / MCP — see SKILL.md.
"""
import argparse
import json
import os
import sys

from sofit.transcribe import cached_segments
from sofit import generate as gen

POOL_SYSTEM = (
    "You are picking a POOL of candidate short-form clips from a Hebrew podcast "
    "(Reels/TikTok/Shorts) for a human to choose from. Find the {n} strongest, most "
    "DISTINCT moments across the whole episode. " + gen.CLIP_RULES + " Return ONLY a JSON array "
    '[{"title":str,"hook_type":str,"hook_reason":str,"score":int,"reason":str,'
    '"beats":[{"quote_start":str,"quote_end":str}],"hook_variants":[str,str],'
    '"hook_variant_types":[str,str],"alt_opening":str,"alt_hook_type":str}]. '
    + gen.CLIP_FIELDS +
    " reason = one short English line on why it would perform. "
    "Cover different topics. Only include clips you would score 7 or higher."
)


def _segments(video):
    segs = cached_segments(video)
    if not segs:
        sys.exit(f"error: no cached transcript for {video} — transcribe it first "
                 f"(see SKILL.md step 1)")
    return segs


def cmd_pool(video, out, n, titler):
    segs = _segments(video)
    audio_end = segs[-1].end
    # .replace, not .format: CLIP_FIELDS legitimately contains JSON braces.
    system = POOL_SYSTEM.replace("{n}", str(n)) + gen.performance_hint()
    user = f"Transcript segments:\n{gen._numbered(segs)}"

    def validate(obj):
        if not isinstance(obj, list):
            raise gen.GenerationError("expected an array")
        out_rows = []
        for it in obj:
            # Score gate, hook snap, length bar and variant cleanup all live in
            # resolve_clip_item — shared with make_quotes so they can't drift.
            q = gen.resolve_clip_item(it, segs, audio_end)
            if q is None:
                continue
            row = {
                "start": round(q.start, 3), "end": round(q.end, 3),
                "beats": [[round(s, 3), round(e, 3)] for s, e in q.beats],
                "score": int(it.get("score", 0)), "hook": q.text,
                # `type` is the pre-existing key; `hook_type` is the same label
                # under the name the rest of the pipeline stores.
                "type": q.hook_type,
                "hook_type": q.hook_type,
                "reason": (it.get("reason") or "").strip(),
                "hook_variants": list(q.variants),
                "flagged": q.hook_type in gen.FLAGGED_HOOK_TYPES,
            }
            if q.hook_reason:
                row["hook_reason"] = q.hook_reason
            if q.alt_opening:
                row["alt_opening"] = q.alt_opening
            if q.alt_hook_type:
                row["alt_hook_type"] = q.alt_hook_type
            if any(q.variant_types):
                row["hook_variant_types"] = list(q.variant_types)
            out_rows.append(row)
        if not out_rows:
            raise gen.GenerationError("no candidates met the bar")
        return out_rows

    cands = gen.call_claude_json(system, user, validate, titler=titler)
    # Higher model score still wins. Equal scores lean toward the preferred
    # opening types. Nothing is dropped for its hook_type.
    cands.sort(key=lambda c: gen.hook_rank_key(c["score"], c.get("hook_type") or ""))
    kept = []
    for c in cands:
        if any(not (c["end"] <= k["start"] or c["start"] >= k["end"]) for k in kept):
            continue
        kept.append(c)
    kept.sort(key=lambda c: c["start"])
    out = out or os.path.splitext(video)[0] + ".pool.json"
    json.dump({"video": os.path.abspath(video), "candidates": kept},
              open(out, "w"), ensure_ascii=False, indent=2)
    print(f"wrote {out} ({len(kept)} candidates)\n")
    for i, c in enumerate(kept, 1):
        print(gen.format_pool_candidate(i, c))


def cmd_build(video, pool_path, pick, out):
    segs = _segments(video)
    pool = json.load(open(pool_path))
    cands = pool["candidates"]
    nums = [int(x) for x in pick.split(",") if x.strip()]
    clips = []
    for n in nums:
        c = cands[n - 1]  # 1-based, matches the pool table
        beats = tuple((float(s), float(e)) for s, e in
                      (c.get("beats") or [[c["start"], c["end"]]]))
        raw_type = c.get("hook_type") or c.get("type") or ""
        raw_alt_type = c.get("alt_hook_type") or ""
        q = gen.Quote(start=float(c["start"]), end=float(c["end"]),
                      text=c["hook"], variants=tuple(c.get("hook_variants") or []),
                      variant_types=tuple(
                          gen.normalize_hook_type(t) if isinstance(t, str) and t.strip() else ""
                          for t in (c.get("hook_variant_types") or [])),
                      beats=beats,
                      hook_type=gen.normalize_hook_type(raw_type) if raw_type else "",
                      hook_reason=(c.get("hook_reason") or "").strip(),
                      alt_opening=(c.get("alt_opening") or "").strip(),
                      alt_hook_type=(gen.normalize_hook_type(raw_alt_type)
                                     if raw_alt_type else ""))
        clips.append(gen.clip_spec(q, segs, f"clip-{n}"))
    out = out or os.path.splitext(video)[0] + ".clips.json"
    json.dump({"schema_version": 1, "source": {"video": os.path.abspath(video)},
               "clips": clips}, open(out, "w"), ensure_ascii=False, indent=2)
    print(f"wrote {out} ({len(clips)} clips: {nums})")


def _hook_type_for_log(hook_type, spec, clip, variant):
    """Optional label. --hook-type wins; otherwise copy it off the clip spec.

    Missing on both is fine: rows written before hook_type existed stay valid.
    """
    if hook_type:
        try:
            return gen.parse_hook_type(hook_type)
        except ValueError as e:
            sys.exit(f"error: {e}")
    if not spec:
        return None
    from sofit.publog import hook_type_from_spec
    return hook_type_from_spec(spec, clip, variant)


def cmd_log(episode, clip, hook, platform, views, retention, variant, path,
            hook_type="", spec=None):
    """Append one posted clip's numbers to the performance log."""
    row = {"date": __import__("datetime").date.today().isoformat(), "episode": episode,
           "clip": clip, "variant": variant, "platform": platform, "hook": hook}
    labeled = _hook_type_for_log(hook_type, spec, clip, variant)
    if labeled:
        row["hook_type"] = labeled
    if views is not None:
        row["views"] = views
    if retention is not None:
        row["retention"] = retention
    p = path or gen.PERF_LOG
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    rows = gen._perf_rows(p)
    need = max(0, gen.MIN_PERF_ROWS - len(rows))
    print(f"logged -> {p} ({len(rows)} rows)")
    print(f"{need} more row(s) until pool generation starts learning from these."
          if need else "pool generation is now learning from these.")


def main():
    p = argparse.ArgumentParser(prog="clips.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("pool")
    pp.add_argument("video")
    pp.add_argument("--out")
    pp.add_argument("--n", type=int, default=12)
    pp.add_argument("--titler", default="claude-cli", choices=["api", "claude-cli"])
    bp = sub.add_parser("build")
    bp.add_argument("video")
    bp.add_argument("pool")
    bp.add_argument("--pick", required=True, help="comma-separated candidate numbers, e.g. 2,4,7")
    bp.add_argument("--out")
    lp = sub.add_parser("log", help="record how a posted clip performed")
    lp.add_argument("episode")
    lp.add_argument("clip", help="clip id, e.g. clip-5")
    lp.add_argument("hook", help="the hook line that was actually posted")
    lp.add_argument("--platform", default="")
    lp.add_argument("--views", type=int)
    lp.add_argument("--retention", type=float, help="percent watched (the better signal)")
    lp.add_argument("--variant", type=int, default=0)
    lp.add_argument("--hook-type", default="",
                    help="opening type of the posted hook (optional; "
                         "else copied from --spec)")
    lp.add_argument("--spec", help="clips.json to copy hook_type from")
    lp.add_argument("--path")
    a = p.parse_args()
    if a.cmd == "pool":
        cmd_pool(a.video, a.out, a.n, a.titler)
    elif a.cmd == "log":
        cmd_log(a.episode, a.clip, a.hook, a.platform, a.views, a.retention,
                a.variant, a.path, a.hook_type, a.spec)
    else:
        cmd_build(a.video, a.pool, a.pick, a.out)


if __name__ == "__main__":
    main()
