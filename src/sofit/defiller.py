"""Cut hesitation sounds out of a clip at render time.

Detection runs on the word-level transcript (faster-whisper tokens, times
relative to the span). A token is a filler only when the WHOLE token matches
the list — never a substring — so "אמא", "אהבה", "המון", "umbrella" stay.

Two lists, both stretch-matched (a letter may be held: אממממ, uhhh, hmmmm):

* Clear fillers are cut whenever they match. They are not Hebrew words:
  אמ, אממ, אהה, אהמ, הממ, ממ / מממ, and English um, uh, hmm.
  אמ (alef + medial mem מ) is in this list on purpose. A real Hebrew word
  cannot end in medial mem; "if" is אם, alef + FINAL mem ם. Those are
  different characters, so a stretch of one never matches the other.

* Short ambiguous tokens — אה (a real interjection as well as "uh") and
  אם ("if") — are cut only when they are isolated: the gap on each side is
  at least `pause` seconds (a span edge counts as a pause), or the
  transcriber's word probability is below `low_confidence`. A stretched
  spelling (אההה, אםםם) is no longer the dictionary word, so it is treated
  as a clear filler.

Cuts that would leave an internal island of speech shorter than `min_gap`
are dropped (that jump reads as a glitch, not an edit). Silence-only slivers
shorter than `min_gap` are absorbed into the cut instead. Each cut is padded
by `padding` seconds, clamped so it never eats a neighbouring word, and the
render joins the kept pieces with a short audio/video crossfade.

The lists are `CLEAR_FILLERS` and `SHORT_FILLERS`. `DefillerConfig` replaces
them; `SOFIT_FILLERS` / `SOFIT_SHORT_FILLERS` (comma-separated) append to
them when `config_from_env` builds the config the CLI and MCP use.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, replace

# Clear hesitations. Matching is stretch-based, so "אמ" also covers "אממממ"
# and "um" covers "ummm". "מ" alone is the preposition "from" and is absent;
# "ממ" (two medial mems) is already not a word.
CLEAR_FILLERS: tuple[str, ...] = (
    "אמ",
    "אממ",
    "אהה",
    "אהמ",
    "הממ",
    "ממ",
    "מממ",
    "um",
    "uh",
    "hmm",
)

# Real words that only count as a filler when they are isolated or the
# transcriber was unsure. Exact form is ambiguous; a stretched form is not.
SHORT_FILLERS: tuple[str, ...] = (
    "אה",
    "אם",
    "hm",
)

# Niqqud, cantillation, maqaf, and other Hebrew marks — not letters.
_HEBREW_MARKS = re.compile(r"[\u0591-\u05C7]")
_BIDI_MARKS = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")


@dataclass(frozen=True)
class DefillerConfig:
    """Knobs for detection and for the cut itself.

    `min_gap` is the minimum speech that must remain BETWEEN two filler cuts.
    A cut that would leave a shorter internal island is skipped. It is not a
    minimum length for the filler itself — hesitations are short, and the
    padding below is what keeps the edit from clipping the next consonant.
    """

    padding: float = 0.04
    crossfade: float = 0.02
    min_gap: float = 0.40
    pause: float = 0.22
    low_confidence: float = 0.45
    min_cut: float = 0.06
    clear: tuple[str, ...] = CLEAR_FILLERS
    short: tuple[str, ...] = SHORT_FILLERS


def config_from_env(min_gap: float | None = None) -> DefillerConfig:
    """Default config, plus comma-separated extras from the environment.

    `SOFIT_FILLERS` appends to the clear list, `SOFIT_SHORT_FILLERS` to the
    ambiguous list. Tests build `DefillerConfig()` directly so they stay
    hermetic; the CLI and MCP call this.
    """
    cfg = DefillerConfig()
    extra_clear = _split_env("SOFIT_FILLERS")
    extra_short = _split_env("SOFIT_SHORT_FILLERS")
    if extra_clear:
        cfg = replace(cfg, clear=cfg.clear + extra_clear)
    if extra_short:
        cfg = replace(cfg, short=cfg.short + extra_short)
    if min_gap is not None:
        cfg = replace(cfg, min_gap=min_gap)
    return cfg


def _split_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class FillerHit:
    """One filler-shaped token. `applied` is false when we detected it and
    deliberately left it in (ambiguous shape, or the cut would jump)."""

    text: str
    start: float
    end: float
    reason: str
    detail: str
    applied: bool
    cut_start: float | None = None
    cut_end: float | None = None


@dataclass
class DefillPlan:
    hits: list[FillerHit]
    cuts: list[tuple[float, float]]
    words: list[dict]
    # (source_start, source_end, output_start) for each kept piece.
    layout: list[tuple[float, float, float]]
    duration: float
    source_duration: float
    crossfade: float

    @property
    def applied_hits(self) -> list[FillerHit]:
        return [h for h in self.hits if h.applied]


def normalize_token(text: str) -> str:
    """Strip niqqud, bidi marks, and edge punctuation. Keep final mem distinct
    from medial mem — that distinction is what saves אם ("if")."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _BIDI_MARKS.sub("", text)
    text = _HEBREW_MARKS.sub("", text)
    text = text.strip().lower()
    # Drop anything that is not a letter from the edges (commas, quotes, ellipsis).
    while text and not text[0].isalpha():
        text = text[1:]
    while text and not text[-1].isalpha():
        text = text[:-1]
    return text


def is_stretch(token: str, base: str) -> bool:
    """True when `token` is `base` with some letters held longer.

    "אמממ" is a stretch of "אמ". "אם" is not (final mem ם ≠ medial mem מ).
    "אמא" is not a stretch of "אמ". "umbrella" is not a stretch of "um".
    """
    if not token or not base:
        return False
    i = j = 0
    while j < len(base):
        if i >= len(token) or token[i] != base[j]:
            return False
        i += 1
        j += 1
        # Soak extra repeats of this letter only once the base moves on to a
        # different letter (or ends). Otherwise "הממ" could not match itself:
        # the two mems in the base are two steps, not one run to collapse.
        if j == len(base) or base[j] != token[i - 1]:
            while i < len(token) and token[i] == token[i - 1]:
                i += 1
    return i == len(token)


def classify_token(token: str, cfg: DefillerConfig) -> str | None:
    """'clear', 'short', or None. `token` must already be normalized.

    A stretched ambiguous token ("אההה", "אםם") is classified clear: the extra
    letters mean it is no longer the dictionary word.
    """
    if not token or any(ch.isspace() for ch in token) or len(token) > 24:
        return None
    for base in cfg.clear:
        b = normalize_token(base)
        if b and is_stretch(token, b):
            return "clear"
    for base in cfg.short:
        b = normalize_token(base)
        if not b:
            continue
        if token == b:
            return "short"
        if len(token) > len(b) and is_stretch(token, b):
            return "clear"
    return None


def coerce_word(word: dict) -> dict:
    """Accept a clips.json word ({t, d, w, p?}) or a transcript word
    ({start, end, text, probability?}). Times stay in whatever clock the
    caller used; planning is relative to zero of that clock."""
    prob = word.get("p", word.get("probability", word.get("prob")))
    if "t" in word and ("w" in word or "text" in word):
        text = word.get("w", word.get("text", ""))
        out = {"t": float(word["t"]), "d": float(word.get("d") or 0.0), "w": str(text)}
    else:
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") if word.get("end") is not None else start)
        text = word.get("text", word.get("word", ""))
        out = {"t": start, "d": max(0.0, end - start), "w": str(text)}
    if prob is not None and prob != "":
        out["p"] = float(prob)
    return out


def _word_prob(word: dict) -> float | None:
    p = word.get("p")
    return None if p is None else float(p)


def _fmt_gap(gap: float | None) -> str:
    return "edge" if gap is None else f"{gap:.2f}s"


def merge_cuts(cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort and merge overlapping or touching cut intervals."""
    if not cuts:
        return []
    ordered = sorted((s, e) for s, e in cuts if e - s > 1e-4)
    if not ordered:
        return []
    merged: list[list[float]] = [[ordered[0][0], ordered[0][1]]]
    for s, e in ordered[1:]:
        if s <= merged[-1][1] + 1e-3:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def keep_intervals(duration: float, cuts: list[tuple[float, float]]
                   ) -> list[tuple[float, float]]:
    """Complement of `cuts` inside [0, duration]."""
    if duration <= 0:
        return []
    cursor = 0.0
    keeps: list[tuple[float, float]] = []
    for s, e in merge_cuts(cuts):
        s = min(duration, max(0.0, s))
        e = min(duration, max(0.0, e))
        if e < s:
            continue
        if s > cursor + 1e-3:
            keeps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration - 1e-3:
        keeps.append((cursor, duration))
    return [(s, e) for s, e in keeps if e - s > 1e-3]


def splice_layout(keeps: list[tuple[float, float]], crossfade: float
                  ) -> tuple[list[tuple[float, float, float]], float, float]:
    """Place kept pieces on the output timeline.

    Returns (layout, output_duration, crossfade_used). Each layout item is
    (source_start, source_end, output_start). Later pieces start `crossfade`
    early so the audio crossfade and the video dissolve overlap by the same
    amount and the captions, which use this timeline, stay in sync.

    A piece shorter than about three crossfades disables the overlap (hard
    join). One piece never crossfades — there is nothing to join.
    """
    clean = [(s, e) for s, e in keeps if e - s > 1e-3]
    if not clean:
        return [], 0.0, 0.0
    xf = 0.0 if len(clean) == 1 else max(0.0, crossfade)
    if xf:
        shortest = min(e - s for s, e in clean)
        xf = min(xf, shortest / 3.0)
        if xf < 0.005:
            xf = 0.0
    layout: list[tuple[float, float, float]] = []
    cursor = 0.0
    for i, (s, e) in enumerate(clean):
        out_start = cursor if i == 0 else cursor - xf
        layout.append((s, e, out_start))
        cursor = out_start + (e - s)
    return layout, cursor, xf


def shift_point(t: float, layout: list[tuple[float, float, float]]) -> float:
    """Map a source-span time onto the output timeline.

    A time that lands inside a cut snaps to the join (the start of the next
    kept piece). Times past the last piece clamp to the output end.
    """
    if not layout:
        return t
    if t <= layout[0][0]:
        return layout[0][2]
    for s, e, out in layout:
        if t < s:
            return out
        if t <= e:
            return out + (t - s)
    s, e, out = layout[-1]
    return out + (e - s)


def shift_window(start: float, end: float,
                 layout: list[tuple[float, float, float]],
                 min_dur: float = 0.12) -> tuple[float, float] | None:
    """Map a source interval. None when the cut ate essentially all of it."""
    ns, ne = shift_point(start, layout), shift_point(end, layout)
    if ne - ns < min_dur:
        return None
    return ns, ne


def time_in_layout(t: float, layout: list[tuple[float, float, float]]) -> bool:
    """True when `t` falls inside a kept piece (not inside a cut)."""
    return any(s - 1e-6 <= t <= e + 1e-6 for s, e, _out in layout)


def remap_face_samples(samples: list, layout: list[tuple[float, float, float]]) -> list:
    """Drop face samples that fall inside a cut and retime the rest.

    Speaker-tracking keyframes are evaluated against the shortened output,
    so a sample taken during an "אממ" must not still be on the old clock.
    """
    out = []
    for item in samples:
        t, cx = item[0], item[1]
        if not time_in_layout(float(t), layout):
            continue
        out.append((shift_point(float(t), layout), cx))
    return out


def _gaps(words: list[dict], index: int) -> tuple[float | None, float | None]:
    """Silence before and after word `index`. None means the span edge, which
    counts as a pause: a leading "אה" only has to be separated from what follows."""
    word = words[index]
    start = word["t"]
    end = start + word["d"]
    if index == 0:
        before: float | None = None
    else:
        prev = words[index - 1]
        before = start - (prev["t"] + prev["d"])
    if index == len(words) - 1:
        after: float | None = None
    else:
        after = words[index + 1]["t"] - end
    return before, after


def _isolated(before: float | None, after: float | None, pause: float) -> bool:
    before_ok = before is None or before >= pause
    after_ok = after is None or after >= pause
    return before_ok and after_ok


def _cut_bounds(words: list[dict], index: int, duration: float, pad: float
                ) -> tuple[float, float] | None:
    """Word span expanded by `pad`, but never into a neighbour word."""
    word = words[index]
    start = word["t"]
    end = start + max(word["d"], 0.0)
    left = max(0.0, start - pad)
    right = min(duration, end + pad)
    if index > 0:
        prev_end = words[index - 1]["t"] + words[index - 1]["d"]
        left = max(left, min(prev_end, start))
    if index + 1 < len(words):
        next_start = words[index + 1]["t"]
        right = min(right, max(next_start, end))
    left = min(left, start)
    right = max(right, end)
    right = min(right, duration)
    left = max(0.0, left)
    if right - left < 0.02:
        return None
    return left, right


def _midpoint_inside(words: list[dict], index: int, cuts: list[tuple[float, float]]) -> bool:
    mid = words[index]["t"] + words[index]["d"] / 2.0
    return any(s - 1e-6 <= mid <= e + 1e-6 for s, e in cuts)


def _has_speech(start: float, end: float, words: list[dict],
                cuts: list[tuple[float, float]]) -> bool:
    """True when a non-cut word overlaps [start, end]."""
    for i, word in enumerate(words):
        if _midpoint_inside(words, i, cuts):
            continue
        ws, we = word["t"], word["t"] + word["d"]
        if we > start + 1e-3 and ws < end - 1e-3:
            return True
    return False


def _absorb_silence(duration: float, cuts: list[tuple[float, float]],
                    words: list[dict], min_gap: float) -> list[tuple[float, float]]:
    """Swallow kept slivers that contain no speech and are shorter than min_gap.

    Those are the padding leftovers at the edge of a cut, not a word we meant
    to keep. Absorbing them avoids a one-frame blip before the join.
    """
    changed = True
    while changed:
        changed = False
        for ks, ke in keep_intervals(duration, cuts):
            if ke - ks >= min_gap or ke - ks <= 1e-3:
                continue
            if _has_speech(ks, ke, words, cuts):
                continue
            cuts = merge_cuts(list(cuts) + [(ks, ke)])
            changed = True
            break
    return cuts


def _suppress_short_islands(duration: float, cuts: list[tuple[float, float]],
                            words: list[dict], min_gap: float
                            ) -> list[tuple[float, float]]:
    """Drop a cut when the speech BETWEEN two cuts would be shorter than min_gap.

    An edge trim (filler at the very start or end) is allowed: nothing is
    left flashing between two joins. The internal case is the glitchy one —
    a 0.2s "כן" sandwiched between two removed "אממ"s.
    """
    cuts = list(cuts)
    while True:
        victim: tuple[float, float] | None = None
        for ks, ke in keep_intervals(duration, cuts):
            if ke - ks >= min_gap - 1e-6:
                continue
            if ks <= 0.02 or ke >= duration - 0.02:
                continue
            if not _has_speech(ks, ke, words, cuts):
                continue
            left = next((c for c in cuts if abs(c[1] - ks) <= 0.04), None)
            right = next((c for c in cuts if abs(c[0] - ke) <= 0.04), None)
            if left is None or right is None:
                continue
            victim = left if (left[1] - left[0]) <= (right[1] - right[0]) else right
            break
        if victim is None:
            break
        cuts = [c for c in cuts if c != victim]
    return cuts


def plan_defill(words: list[dict], duration: float,
                cfg: DefillerConfig | None = None) -> DefillPlan:
    """Plan filler cuts for one span.

    `words` are clip-relative ({t, d, w}) or transcript-shaped ({start, end,
    text}). `duration` is the span length in the same clock. The returned
    `words` have filler tokens removed and the survivors shifted onto the
    output timeline (crossfade overlap included).
    """
    cfg = cfg or DefillerConfig()
    src = [coerce_word(w) for w in words]
    if duration <= 0 and src:
        duration = max(w["t"] + w["d"] for w in src)
    duration = max(0.0, float(duration))

    detected: list[dict] = []
    raw_cuts: list[tuple[float, float]] = []
    for i, word in enumerate(src):
        kind = classify_token(normalize_token(word["w"]), cfg)
        if kind is None:
            continue
        before, after = _gaps(src, i)
        prob = _word_prob(word)
        if kind == "clear":
            reason = "pattern"
            detail = "clear filler"
            eligible = True
        else:
            low = prob is not None and prob < cfg.low_confidence
            paused = _isolated(before, after, cfg.pause)
            conf = "n/a" if prob is None else f"{prob:.2f}"
            gaps = f"gaps {_fmt_gap(before)}/{_fmt_gap(after)}"
            if low:
                reason = "low_confidence"
                detail = f"ambiguous short token, confidence {conf} < {cfg.low_confidence:.2f}"
                eligible = True
            elif paused:
                reason = "pause"
                detail = f"ambiguous short token isolated by pauses ({gaps})"
                eligible = True
            else:
                reason = "ambiguous"
                detail = (
                    f"ambiguous short token left in; {gaps}, need ≥{cfg.pause:.2f}s "
                    f"both sides, confidence {conf} (cut below {cfg.low_confidence:.2f})"
                )
                eligible = False
        bounds = _cut_bounds(src, i, duration, cfg.padding) if eligible else None
        if eligible and (bounds is None or bounds[1] - bounds[0] < cfg.min_cut):
            eligible = False
            reason = "too_short"
            detail = f"filler span under {cfg.min_cut:.2f}s — left in"
            bounds = None
        if bounds is not None:
            raw_cuts.append(bounds)
        detected.append({
            "index": i,
            "reason": reason,
            "detail": detail,
            "eligible": eligible,
            "bounds": bounds,
        })

    cuts = merge_cuts(raw_cuts)
    cuts = _absorb_silence(duration, cuts, src, cfg.min_gap)
    cuts = _suppress_short_islands(duration, cuts, src, cfg.min_gap)
    cuts = _absorb_silence(duration, cuts, src, cfg.min_gap)

    # Don't hollow the span out. A clip that is mostly hesitation stays intact.
    removed = sum(e - s for s, e in cuts)
    hollow = bool(cuts) and duration - removed < 0.45
    if hollow:
        cuts = []

    hits: list[FillerHit] = []
    removed_indexes: set[int] = set()
    for item in detected:
        word = src[item["index"]]
        applied = bool(item["eligible"] and cuts and _midpoint_inside(src, item["index"], cuts))
        detail = item["detail"]
        if item["eligible"] and not applied:
            if hollow:
                detail = f"{detail}; skipped — cutting it would leave under 0.45s of clip"
            else:
                detail = (
                    f"{detail}; skipped — cutting it would leave under "
                    f"{cfg.min_gap:.2f}s of speech between cuts"
                )
        if applied:
            removed_indexes.add(item["index"])
        bounds = item["bounds"]
        reason = item["reason"]
        if item["eligible"] and not applied:
            reason = "hollow" if hollow else "min_gap"
        hits.append(FillerHit(
            text=word["w"],
            start=word["t"],
            end=word["t"] + word["d"],
            reason=reason,
            detail=detail,
            applied=applied,
            cut_start=bounds[0] if bounds else None,
            cut_end=bounds[1] if bounds else None,
        ))

    keeps = keep_intervals(duration, cuts)
    layout, out_dur, xf = splice_layout(keeps, cfg.crossfade if cuts else 0.0)
    if not cuts:
        # No edit: hand the original coerced words back on the source clock.
        return DefillPlan(
            hits=hits, cuts=[], words=[dict(w) for w in src],
            layout=layout or ([(0.0, duration, 0.0)] if duration > 0 else []),
            duration=duration, source_duration=duration, crossfade=0.0,
        )

    shifted: list[dict] = []
    for i, word in enumerate(src):
        if i in removed_indexes:
            continue
        mapped = shift_point(word["t"], layout)
        out = dict(word)
        out["t"] = round(mapped, 3)
        out["d"] = round(word["d"], 3)
        shifted.append(out)
    return DefillPlan(
        hits=hits, cuts=list(cuts), words=shifted, layout=layout,
        duration=out_dur, source_duration=duration, crossfade=xf,
    )


def iter_clip_spans(clip: dict) -> list[tuple[int, float, float, list[dict]]]:
    """(span index, absolute start, absolute end, words) for a clip spec.

    Narrative edits store words per kept segment, relative to that segment.
    A plain clip stores them on the clip, relative to clip start.
    """
    segments = clip.get("segments")
    if segments:
        out = []
        for i, seg in enumerate(segments):
            start = float(seg.get("start") or 0.0)
            end = float(seg.get("end") or start)
            out.append((i, start, end, list(seg.get("words") or [])))
        return out
    start = float(clip.get("start") or 0.0)
    end = float(clip.get("end") or start)
    return [(0, start, end, list(clip.get("words") or []))]


def analyze_clips(clips: list[dict], cfg: DefillerConfig | None = None) -> list[dict]:
    """Structured dry-run: one entry per span, hits in source-absolute seconds."""
    cfg = cfg or DefillerConfig()
    rows = []
    for clip in clips:
        clip_id = str(clip.get("id") or "clip")
        for span_i, start, end, words in iter_clip_spans(clip):
            dur = end - start
            if dur <= 0 and words:
                coerced = [coerce_word(w) for w in words]
                dur = max(w["t"] + w["d"] for w in coerced)
            plan = plan_defill(words, dur, cfg)
            hits = []
            for hit in plan.hits:
                hits.append({
                    "text": hit.text,
                    "start": round(start + hit.start, 3),
                    "end": round(start + hit.end, 3),
                    "applied": hit.applied,
                    "reason": hit.reason,
                    "detail": hit.detail,
                })
            rows.append({
                "clip": clip_id,
                "span": span_i,
                "span_start": round(start, 3),
                "span_end": round(start + dur, 3),
                "cuts": len(plan.applied_hits),
                "hits": hits,
            })
    return rows


def format_report(clips: list[dict], cfg: DefillerConfig | None = None) -> str:
    """Human-readable dry-run. Times are absolute on the source clock."""
    cfg = cfg or DefillerConfig()
    rows = analyze_clips(clips, cfg)
    if not rows:
        return "defiller: no clips"
    lines: list[str] = []
    cut_n = keep_n = 0
    for row in rows:
        lines.append(
            f"{row['clip']}  span {row['span']}  "
            f"source {row['span_start']:.3f}–{row['span_end']:.3f}s"
        )
        if not row["hits"]:
            lines.append("  no filler-shaped tokens")
            continue
        for hit in row["hits"]:
            flag = "cut" if hit["applied"] else "keep"
            if hit["applied"]:
                cut_n += 1
            else:
                keep_n += 1
            lines.append(
                f"  {flag:<4}  {hit['start']:8.3f} – {hit['end']:8.3f}  "
                f"{hit['text']}  {hit['detail']}"
            )
    lines.append(
        f"defiller: {cut_n} cut, {keep_n} kept, of {cut_n + keep_n} filler-shaped tokens"
    )
    return "\n".join(lines)
