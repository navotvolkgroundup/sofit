"""Claude-backed generators over a cached transcript.

One shared helper (`call_claude_json`) does the model call + JSON parse + a
single retry + validation. Each generator supplies only its prompt and a
validator, so the parse/retry/error logic lives in exactly one place.

Chapter/quote timestamps come from Whisper, never the LLM: Claude returns a
segment INDEX and code maps it to that segment's start time. Because LLMs drift
on long numbered lists, we guard the returned indices (in-range + strictly
increasing + a min gap) and cross-check that Claude's echoed text prefix
actually matches the segment it selected.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import languages
from .transcribe import Segment

# Titling is cheap and quality-sensitive for Hebrew; tune this to taste.
# (Sonnet 5 is a good cost/quality default; bump to Opus for hardest cases.)
CLAUDE_MODEL = "claude-sonnet-5"

# `claude -p` wall clock. Generous by default: a 66-min episode asking for 8
# candidate clips with 2 hook variants each ran past the old 300s and died with a
# traceback mid-pipeline. Raised as TimeoutError (not GenerationError) so it fails
# fast instead of burning a second attempt on the retry path.
CLI_TIMEOUT = int(os.environ.get("SOFIT_CLI_TIMEOUT") or 900)


# Opening types for a short-form clip, from the Ames Create study of 1,239 top
# creator videos (Oct 2026). The label describes the FIRST line the viewer hears
# or reads, not the topic of the clip. Preference is a lean: the human still
# picks, and a higher model score still beats a preferred type.
# Lift vs each creator's own typical top video, for the note in the prompt:
#   relatable_callout +19%, high_stakes +5%, curiosity_gap +3% (also 28% of
#   top openings), visual_spectacle +33% but only 2% of videos, middle_of_action
#   -1%, pattern_interrupt -2%, bold_claim -10%, question -14%, controversy -30%.
HOOK_TYPES: tuple[str, ...] = (
    "curiosity_gap",
    "relatable_callout",
    "high_stakes",
    "pattern_interrupt",
    "bold_claim",
    "question",
    "controversy",
    "middle_of_action",
    "visual_spectacle",
    "other",
)
PREFERRED_HOOK_TYPES = frozenset({
    "relatable_callout",
    "curiosity_gap",
    "high_stakes",
})
# Flag these in the shortlist and, when the transcript has one, suggest a
# different opening line. Never drop the candidate because of them.
FLAGGED_HOOK_TYPES = frozenset({
    "question",
    "controversy",
    "bold_claim",
})
# Labels the previous prompt asked for. Mapped so an old model reply still parses.
_LEGACY_HOOK_TYPES = {
    "surprise": "curiosity_gap",
    "emotion": "other",
    "story": "middle_of_action",
}


def normalize_hook_type(value) -> str:
    """Canonical hook_type, or ``other`` when the label is missing or unknown.

    Case, spaces, and hyphens are ignored (``Bold Claim`` -> ``bold_claim``).
    A bad label must not drop the candidate, so this never raises.
    """
    if not isinstance(value, str):
        return "other"
    key = re.sub(r"[\s\-]+", "_", value.strip().lower())
    if key in HOOK_TYPES:
        return key
    return _LEGACY_HOOK_TYPES.get(key, "other")


def parse_hook_type(value: str) -> str:
    """Hook type from a person or a flag. Unknown labels raise ValueError.

    Legacy aliases are accepted. Use :func:`normalize_hook_type` for model
    output, where a bad label should become ``other`` instead of failing.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("hook_type is empty")
    key = re.sub(r"[\s\-]+", "_", value.strip().lower())
    mapped = key if key in HOOK_TYPES else _LEGACY_HOOK_TYPES.get(key)
    if mapped not in HOOK_TYPES:
        allowed = ", ".join(HOOK_TYPES)
        raise ValueError(f"unknown hook_type {value!r}; expected one of {allowed}")
    return mapped


def hook_rank_key(score: int, hook_type: str) -> tuple[int, int]:
    """Sort key for the shortlist (lower sorts first).

    Soft preference only. The stored score is the model's score, unchanged.
    Equal scores lean toward relatable callout, curiosity gap, and high stakes,
    and away from question, controversy, and bold claim. A higher score still
    wins, and no type is removed.
    """
    if hook_type in PREFERRED_HOOK_TYPES:
        bias = 1
    elif hook_type in FLAGGED_HOOK_TYPES:
        bias = -1
    else:
        bias = 0
    return (-int(score), -bias)


def opening_in_transcript(line: str, segments: list[Segment]) -> bool:
    """True when `line` is a real stretch of this transcript, not a paraphrase.

    Same match as chapter location: the first few normalized words must occur
    in some segment. Under three words is too easy to false-match, so those
    suggestions are dropped and the candidate itself stays.
    """
    if len(_norm(line).split()) < 3:
        return False
    return _locate(line, segments, 0) is not None


def format_pool_candidate(index: int, cand: dict) -> str:
    """Numbered shortlist lines. Every candidate shows its hook type.

    Flagged openings (question, controversy, bold claim) are marked, and an
    alternate line is printed when the transcript had one.
    """
    m, s = divmod(int(cand["start"]), 60)
    beats = cand.get("beats") or [[cand["start"], cand["end"]]]
    kept_sec = int(sum(e - b for b, e in beats))
    cuts = "" if len(beats) == 1 else f", {len(beats)} beats ({len(beats) - 1} cut)"
    hook_type = cand.get("hook_type") or cand.get("type") or "other"
    flag = " flag" if hook_type in FLAGGED_HOOK_TYPES else ""
    lines = [
        f"{index:2d}. [{m}:{s:02d}] ({kept_sec}s{cuts}, {hook_type}{flag}, "
        f"score {cand['score']}) {cand['hook']}"
    ]
    why_type = (cand.get("hook_reason") or "").strip()
    if why_type:
        lines.append(f"      type: {why_type}")
    lines.append(f"      why: {cand.get('reason', '')}")
    alt = (cand.get("alt_opening") or "").strip()
    if alt and hook_type in FLAGGED_HOOK_TYPES:
        label = cand.get("alt_hook_type") or ""
        suffix = f" ({label})" if label else ""
        lines.append(f"      alt opening{suffix}: {alt}")
    return "\n".join(lines)


# What makes a good short-form clip, and what each JSON field means. Shared by the
# one-shot selector (`make_quotes`) and the skill's candidate-pool generator so the
# two prompts can't drift on the contract — they already did once, with the pool
# asking for 20-60s clips while the code clamped them at 45.
# Navot, 2026-09-24: the show's name is always written in Hebrew in anything we
# publish. Whisper transcribes it as "Weekly Sink"/"Weekly Sync", so without this
# the Latin spelling leaks straight from the transcript into hooks and titles.
BRAND_RULE = (
    " Always write the show's name in Hebrew as 'וויקלי סינק' - never 'Weekly Sync', "
    "'Weekly Sink' or any other Latin spelling, even when the transcript spells it "
    "that way."
)

CLIP_RULES = (
    "Each clip MUST: (1) OPEN with a hook in its VERY FIRST sentence that stops the "
    "scroll within ~3 seconds. Completion rate is what triggers viral distribution "
    "and it is decided in those first seconds, so open on the sharpest true line, "
    "never the wind-up ('today we will talk about...'). "
    "SOFT PREFERENCE, not a filter: when several moments are close, prefer an "
    "opening that is a relatable callout (the viewer's own situation), a curiosity "
    "gap (a question the clip has not answered yet), or high stakes (money, status, "
    "career, a consequence on someone). A direct question, a controversy, or a bold "
    "claim can still be the moment — do not discard a strong story because of its "
    "opening — but when the same transcript can start on the scene or the stake "
    "instead of the provocation, start there. "
    "The hook must NEVER overpromise what the clip delivers: platforms now "
    "punish bait (early-exit is a negative ranking signal). Score highest when the "
    "hook is the first thing said, not buried after throat-clearing, and when the "
    "moment carries STRONG FIRST-PERSON OPINION LANGUAGE or an EMOTIONAL PEAK — "
    "those are the transcript signals that travel. (2) be a COMPLETE, self-contained "
    "STORY with a payoff that closes that gap, not a fragment; (3) total about 20-45 "
    "seconds of kept material — shorter clips complete more, so when in doubt cut "
    "shorter; anything over ~40s needs a payoff that earns every second. EDIT, "
    "don't just cut a window: a clip is 1-4 beats "
    "(quote spans in chronological order) that together tell one story — hook, "
    "escalation, payoff. When the strongest hook and its payoff are separated by "
    "banter, tangents or filler, DROP the filler by making each kept part its own "
    "beat. Prefer 2-3 tight beats over one baggy window. Never reorder beats, never "
    "cut mid-sentence, and keep every beat at least ~4 seconds so the edit doesn't "
    "feel jumpy."
)

CLIP_FIELDS = (
    "title = a punchy Hebrew hook line for the clip. It MUST be SELF-CONTAINED for "
    "a cold viewer with zero episode context: name the subject explicitly (the "
    "product, company, movie or claim — 'אינסטינקט', not 'הכלי שלי'; 'הסרט אובססיה', "
    "not 'הסרט') and never leave an unresolved pronoun. "
    # Navot, 2026-09-24: "name the subject" was being satisfied by bolting the
    # SPEAKER's name on the front - every hook came out "נבות וולק: <claim>".
    # That is a byline, not a hook: it spends the scroll-stopping first words on
    # attribution and states the conclusion instead of opening a gap.
    "But the subject is the TOPIC, never the speaker. Do NOT open with a host's "
    "name or use the 'שם הדובר: ציטוט' template - the caption credits them, the "
    "hook does not. Put the sharpest words first, and where you can, address the "
    "viewer's own situation so the gap is theirs to close.\n"
    "- Weak: 'נבות וולק: \"אקס גוגל\" בלינקדאין תמיד אומר לי דבר אחד'. "
    "Strong: 'כתבת \"אקס גוגל\" בלינקדאין? זה משדר בדיוק הפוך ממה שרצית'.\n"
    "- Weak: 'נציג התמיכה לנבות וולק: \"אתה מטמטם אותי עם מילים\"'. "
    "Strong: 'נציג התמיכה התקשר להתחנן שאפסיק - הוא לא ידע שהוא מתכתב עם AI'.\n"
    "hook_variants = exactly 2 "
    "ALTERNATE Hebrew hook lines for the same moment, each taking a DIFFERENT angle "
    "from title. Prefer a relatable callout, a curiosity gap, or high stakes for "
    "those alternates too — do not spend a variant on a direct question, a "
    "controversy, or a bold claim when a story-first line from this moment exists. "
    "They are for A/B testing which opener holds viewers. "
    "hook_variant_types = the hook_type of each hook_variant, same order, same "
    "allowed labels. "
    "hook_type = exactly one of " + "|".join(HOOK_TYPES) + ". "
    "Label the OPENING line, not the topic. "
    "curiosity_gap = the opener withholds a payoff the clip then delivers. "
    "relatable_callout = it names the viewer's own situation. "
    "high_stakes = money, status, career, or a consequence that lands on someone. "
    "pattern_interrupt = the first line breaks the expected rhythm. "
    "bold_claim = a flat assertion with no open question. "
    "question = it asks the viewer something directly. "
    "controversy = the first line picks a fight or a villain. "
    "middle_of_action = it starts mid-story, already inside the scene. "
    "visual_spectacle = the picture, not the sentence, is the hook — rare in a "
    "talking-head podcast; do not invent it. "
    "other = none of those. "
    "hook_reason = one short English line on why that label fits THIS opening. "
    "When hook_type is question, controversy, or bold_claim, also set alt_opening "
    "to a DIFFERENT Hebrew line copied VERBATIM from this transcript that could "
    "open the same moment on the story or the stake instead of the provocation, "
    "and alt_hook_type for that line (prefer relatable_callout, curiosity_gap, or "
    "high_stakes). alt_opening is a suggestion for the human, never a replacement "
    "for title. If the transcript has no such line, leave alt_opening empty. "
    "Otherwise leave alt_opening empty. "
    "score = 1-10 for how strongly the OPENING stops the scroll — score the "
    "moment, and do not inflate or sink the score just to enforce the preference. "
    "beats = the story's kept spans, in "
    'order: [{"quote_start": str, "quote_end": str}, ...] where quote_start is the '
    "first ~4 words of that span copied VERBATIM from the transcript and quote_end "
    "is its last ~4 words, VERBATIM. A single-span clip has one beat. For "
    "backwards compatibility you may instead give top-level quote_start/quote_end "
    "for a one-beat clip." + BRAND_RULE
)


# Feedback loop: which posted clips actually held viewers.
# Kept OUTSIDE the repo — these are real business numbers and the repo is public.
# Default lives under ~/.sofit: macOS TCC blocks launchd/cron jobs from
# ~/Documents, which silently killed scheduled scrapers. The legacy Documents
# path is still honored when it's the only log present.
_LEGACY_PERF_LOG = Path.home() / "Documents" / "sofit-performance.jsonl"
_DEFAULT_PERF_LOG = Path.home() / ".sofit" / "performance.jsonl"
PERF_LOG = os.environ.get("SOFIT_PERF_LOG") or str(
    _LEGACY_PERF_LOG if _LEGACY_PERF_LOG.exists()
    and not _DEFAULT_PERF_LOG.exists() else _DEFAULT_PERF_LOG)
MIN_PERF_ROWS = 8  # below this, "what worked" is noise, not signal


def _perf_rows(path: str | None = None) -> list[dict]:
    """Rows from the performance log that carry a hook and at least one metric."""
    p = Path(path or PERF_LOG)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip a malformed line rather than lose the whole log
        if r.get("hook") and (r.get("retention") is not None or r.get("views") is not None):
            rows.append(r)
    return rows


def performance_hint(path: str | None = None, n: int = 3,
                     min_rows: int = MIN_PERF_ROWS) -> str:
    """Prompt block naming the hooks that over- and under-performed on real posts,
    or "" when there isn't enough logged data for that to mean anything.

    Ranked by retention when logged, falling back to views — retention is the
    honest signal, since views are confounded by posting time and follower count.

    Deliberately dumb: the model just sees real examples. No scoring model, no
    weights to maintain. If the log grows enough that per-hook-type stats would
    beat few-shot examples, that's the time to build something smarter.
    """
    rows = _perf_rows(path)
    if len(rows) < min_rows:
        return ""
    rows.sort(key=lambda r: (r.get("retention") if r.get("retention") is not None else -1,
                             r.get("views") or 0), reverse=True)

    def fmt(r):
        m = f"{r['retention']}% retention" if r.get("retention") is not None \
            else f"{r['views']} views"
        if r.get("speaker"):
            m += f"; opens on {r['speaker']}"
        if r.get("hook_type"):
            m += f"; {r['hook_type']}"
        return f'  - "{r["hook"]}" ({m})'

    best = "\n".join(fmt(r) for r in rows[:n])
    worst = "\n".join(fmt(r) for r in reversed(rows[-n:]))
    return (
        f"\n\nREAL PERFORMANCE from {len(rows)} posted clips of this show — weight "
        "these over your priors.\nHooks that held viewers:\n" + best +
        "\nHooks that lost them:\n" + worst +
        "\nFavour what the winners have in common; avoid what the losers "
        "share. When judging what works, do NOT generalize from social-media "
        "best practices - use ONLY this show's data above."
    )


@dataclass
class Chapter:
    start: float
    title: str


@dataclass
class Quote:
    start: float
    end: float
    text: str
    # Alternate hook lines for the same moment, to A/B against the retention
    # curve. `text` stays the primary. Tuple so the default is safely immutable.
    variants: tuple[str, ...] = ()
    # Parallel to `variants`: the hook_type of each alternate line. Empty when
    # the model didn't label them, so an old reply still parses.
    variant_types: tuple[str, ...] = ()
    # Opening label for `text` (the primary hook). "" when this Quote was built
    # without a classification (a hand-made range, an old pool file).
    hook_type: str = ""
    hook_reason: str = ""
    # Suggested replacement opening for a flagged type, copied from the
    # transcript. Empty when there isn't one, or the type isn't flagged.
    alt_opening: str = ""
    alt_hook_type: str = ""
    # The narrative edit: kept (start, end) spans in chronological order whose
    # union is the clip. One beat == a plain contiguous clip; 2+ beats mean the
    # filler between them is cut out at render time. start/end above stay the
    # envelope (first beat's start, last beat's end).
    beats: tuple[tuple[float, float], ...] = ()


class GenerationError(RuntimeError):
    pass


def _client():
    import anthropic  # lazy import so tests / --help don't require the SDK

    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment


def _call_api(system: str, user: str, model: str) -> str:
    """Transport: Anthropic API (per-token billing; needs ANTHROPIC_API_KEY)."""
    msg = _client().messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _call_claude_cli(system: str, user: str, model: str) -> str:
    """Transport: the `claude -p` CLI (uses your Claude Code / Pro/Max subscription,
    no API key). The large user text goes on stdin to dodge argv limits; the small
    system prompt rides on --append-system-prompt. By default whatever model
    Claude Code is configured with is used; an explicit --titler-model /
    SOFIT_TITLER_MODEL override is forwarded via `--model`."""
    import shutil
    import subprocess

    if not shutil.which("claude"):
        raise GenerationError("claude CLI not found — install Claude Code or use --titler api")
    cmd = ["claude", "-p", "--append-system-prompt", system, "--output-format", "text"]
    if os.environ.get("SOFIT_TITLER_MODEL"):
        cmd += ["--model", model]
    # Running sofit from INSIDE Claude Code leaks that session's environment to
    # the nested CLI - ANTHROPIC_BASE_URL points at an internal proxy and the
    # CLAUDE_CODE_* vars describe the parent session - and the child then dies
    # with "OAuth session expired and could not be refreshed" even though the
    # keychain credentials are fine (2026-09-10: killed a whole kit+pool run and
    # looked exactly like a logged-out user). Hand the child a clean slate.
    env = {k: v for k, v in os.environ.items()
           if k != "ANTHROPIC_BASE_URL"
           and not k.startswith(("CLAUDE_CODE_", "CLAUDE_"))
           and k not in ("CLAUDECODE", "AI_AGENT")}
    try:
        proc = subprocess.run(
            cmd, env=env,
            input=user, capture_output=True, text=True, timeout=CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # 300s was too tight for real work: a 66-min episode (981 segments) asking
        # for 8 clips x 3 hook lines each blew through it. Raise SOFIT_CLI_TIMEOUT
        # for longer episodes, or ask for fewer candidates.
        raise TimeoutError(
            f"claude CLI exceeded {CLI_TIMEOUT}s. Raise SOFIT_CLI_TIMEOUT, ask for "
            "fewer candidates (--n), or use --titler api."
        ) from None
    if proc.returncode != 0:
        raise GenerationError(f"claude CLI failed: {(proc.stderr or '').strip()[:200]}")
    return proc.stdout.strip()


def call_claude_json(system: str, user: str, validate, model: str | None = None, titler: str = "api"):
    """Call Claude, parse a JSON body, validate it, retry once on failure.

    `titler`: "api" (Anthropic API + key) or "claude-cli" (`claude -p`, subscription).
    `model`: explicit model id; falls back to the SOFIT_TITLER_MODEL env var
    (set by the --titler-model CLI flag), then the CLAUDE_MODEL default.
    `validate(obj)` must return the accepted value or raise GenerationError.
    Raises GenerationError after the retry is exhausted.
    """
    model = model or os.environ.get("SOFIT_TITLER_MODEL") or CLAUDE_MODEL
    # Prompts are written for Hebrew; SOFIT_LANG (set by --lang) swaps in the
    # episode language at the one place every generator routes through. The brand
    # rule is a Hebrew-spelling rule, so it drops out entirely in another language
    # rather than becoming "write the show's name in English as 'וויקלי סינק'".
    lang = os.environ.get("SOFIT_LANG", languages.DEFAULT_LANG)
    if lang != languages.DEFAULT_LANG:
        system = system.replace(BRAND_RULE, "").replace("Hebrew", languages.name(lang))
    transport = _call_claude_cli if titler == "claude-cli" else _call_api
    last_err: Exception | None = None
    for _ in range(2):
        text = transport(system, user, model)
        try:
            return validate(json.loads(_strip_fences(text)))
        except (json.JSONDecodeError, GenerationError) as e:
            last_err = e
    raise GenerationError(f"Claude returned unusable output after retry: {last_err}")


def _strip_fences(text: str) -> str:
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _numbered(segments: list[Segment]) -> str:
    return "\n".join(f"[{s.index}] {s.text}" for s in segments)


def _norm(s: str) -> str:
    """Lowercase-ish normalize for matching: keep word chars (incl. Hebrew) and
    spaces, collapse whitespace. Punctuation and niqqud differences don't matter."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s)).strip()


def _locate(quote: str, segments: list[Segment], start_from: int) -> Segment | None:
    """Find the segment where `quote` occurs, at or after index `start_from`.

    LLMs drift badly when asked for a segment index over a 1000-line list, but
    they quote transcript text accurately. So we match on the quote's first few
    words instead of trusting any index Claude returns.
    """
    words = _norm(quote).split()
    if not words:
        return None
    phrase = " ".join(words[:4])
    for s in segments:
        if s.index < start_from:
            continue
        if phrase in _norm(s.text):
            return s
    return None


def _hook_word_start(seg: Segment, quote: str) -> float | None:
    """Start time of the word where `quote` begins inside `seg`, or None.

    `_locate` resolves the segment; a segment often opens with throat-clearing
    ("אז...", "כן, אה") before the actual hook, so starting the clip at the
    segment start puts the hook a beat late — exactly what kills retention in the
    first seconds. Matching on the punctuation-stripped concatenation (same
    normalization as `_locate`) finds the hook's own first word.
    """
    needle = _norm(quote).replace(" ", "")
    if not needle or not seg.words:
        return None
    flat = ""
    owner: list[int] = []  # flat char position -> index in seg.words
    for i, w in enumerate(seg.words):
        t = _norm(w.text).replace(" ", "")
        flat += t
        owner.extend([i] * len(t))
    pos = flat.find(needle[:24])  # first few words are enough to place it
    if pos < 0:
        return None
    return seg.words[owner[pos]].start


def _resolve_span(qs: str, qe: str, segments: list[Segment], audio_end: float,
                  start_from: int, snap_hook: bool) -> tuple[float, float, int] | None:
    """Resolve one quoted span to (start, end, end_segment_index), or None.

    `snap_hook` starts the span on the quoted words themselves (skipping
    throat-clearing earlier in the segment) — wanted for the first beat, where
    second zero must be the hook.
    """
    start_seg = _locate(qs, segments, start_from)
    if start_seg is None:
        return None
    end_seg = _locate(qe, segments, start_seg.index) or start_seg

    start = start_seg.words[0].start if start_seg.words else start_seg.start
    if snap_hook:
        hooked = _hook_word_start(start_seg, qs)
        if hooked is not None and hooked > start:
            start = hooked
    end = min(end_seg.words[-1].end if end_seg.words else end_seg.end, audio_end)
    if end <= start:
        return None
    return start, end, end_seg.index


def resolve_clip_item(item: dict, segments: list[Segment], audio_end: float,
                      min_sec: float = 18.0, max_sec: float = 45.0,
                      min_score: int = 7, min_beat_sec: float = 3.0) -> Quote | None:
    """Turn one model-proposed clip into a timed `Quote`, or None if it misses the bar.

    Takes `{title, score, beats|[quote_start, quote_end], hook_variants}` and
    applies, in order: the hook-strength gate, per-beat quote->segment location
    (chronological, non-overlapping), the hook-word snap on the FIRST beat (so
    second zero is the hook, not the throat-clearing before it), the length gates
    on the KEPT total, and the variant cleanup.

    A clip is a narrative edit: 1-4 beats whose union tells the story and whose
    gaps (banter, tangents) are cut at render time. Beats that touch or nearly
    touch (<1s gap) merge — a sub-second cut reads as a glitch, not an edit.
    Beats shorter than `min_beat_sec` after merging drop the whole clip: the
    model is chasing confetti, not editing.

    Shared by `make_quotes` and the skill's candidate-pool generator. Those two
    grew as parallel copies and silently drifted apart (the pool kept a 90s cap and
    never got the hook snap), so the selection rules live here, once.
    """
    try:
        score = int(item.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    if score < min_score:
        return None  # weak hook — drop

    raw_beats = item.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        raw_beats = [{"quote_start": item.get("quote_start", ""),
                      "quote_end": item.get("quote_end", "")}]

    spans: list[tuple[float, float]] = []
    cursor = 0
    for i, b in enumerate(raw_beats[:4]):
        if not isinstance(b, dict):
            return None
        r = _resolve_span(b.get("quote_start", ""), b.get("quote_end", ""),
                          segments, audio_end, cursor, snap_hook=(i == 0))
        if r is None:
            return None  # an unlocatable beat means an untrustworthy edit; drop
        s, e, end_idx = r
        if spans and s < spans[-1][1]:
            return None  # out of order / overlapping — never reorder speech
        spans.append((s, e))
        cursor = end_idx  # next beat starts at or after this one's end segment

    # Merge beats separated by a blink: a <1s cut reads as a stutter.
    merged: list[list[float]] = [list(spans[0])]
    for s, e in spans[1:]:
        if s - merged[-1][1] < 1.0:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    if any(e - s < min_beat_sec for s, e in merged):
        return None
    beats = tuple((s, e) for s, e in merged)

    kept = sum(e - s for s, e in beats)
    if kept < min_sec:
        return None  # too short for a hook + payoff
    if kept > max_sec:
        # DROP it, don't clamp. Clamping kept the hook and cut the payoff off the
        # end, producing exactly what the prompt forbids: an opener with no
        # punchline. On WS204 three of four clips were clamped — clip-2 promised
        # "Fiverr is collapsing" and ended 40s before the $350M-vs-$600M-cash line
        # that made it interesting. If hook and payoff don't fit in max_sec, this
        # moment is not a short-form clip.
        return None

    title = (item.get("title") or "").strip()
    # Alternate hooks are optional and model-supplied: keep only non-empty strings
    # that differ from the primary, cap at 2. Types stay aligned with the lines
    # that survived that cleanup; a missing list leaves them unlabeled.
    variants_in = item.get("hook_variants") or []
    types_in = item.get("hook_variant_types") or []
    if not isinstance(types_in, list):
        types_in = []
    variants: list[str] = []
    variant_types: list[str] = []
    saw_type = False
    if isinstance(variants_in, list):
        for i, v in enumerate(variants_in):
            if not isinstance(v, str) or not v.strip() or v.strip() == title:
                continue
            variants.append(v.strip())
            raw_t = types_in[i] if i < len(types_in) else ""
            if isinstance(raw_t, str) and raw_t.strip():
                variant_types.append(normalize_hook_type(raw_t))
                saw_type = True
            else:
                variant_types.append("")
            if len(variants) == 2:
                break
    else:
        variants = [
            v.strip() for v in variants_in
            if isinstance(v, str) and v.strip() and v.strip() != title
        ][:2]
    if not saw_type:
        variant_types = []

    hook_type = normalize_hook_type(item.get("hook_type"))
    raw_why = item.get("hook_reason")
    hook_reason = ""
    if isinstance(raw_why, str) and raw_why.strip():
        hook_reason = raw_why.strip().splitlines()[0].strip()[:200]

    # Flagged openings keep a transcript-backed alternative when the model
    # found one. A line that isn't actually in the episode is dropped; the
    # candidate stays, and the human still picks.
    alt_opening = ""
    alt_hook_type = ""
    if hook_type in FLAGGED_HOOK_TYPES:
        raw_alt = item.get("alt_opening") or ""
        if isinstance(raw_alt, str):
            raw_alt = raw_alt.strip()
        else:
            raw_alt = ""
        if raw_alt and raw_alt != title and opening_in_transcript(raw_alt, segments):
            alt_opening = raw_alt
            if item.get("alt_hook_type"):
                alt_hook_type = normalize_hook_type(item.get("alt_hook_type"))

    return Quote(start=beats[0][0], end=beats[-1][1], text=title,
                 variants=tuple(variants), variant_types=tuple(variant_types),
                 hook_type=hook_type, hook_reason=hook_reason,
                 alt_opening=alt_opening, alt_hook_type=alt_hook_type,
                 beats=beats)


def make_chapters(segments: list[Segment], max_chapters: int = 12, titler: str = "api") -> list[Chapter]:
    if not segments:
        return []
    system = (
        "You split a Hebrew podcast transcript into chapters. Return ONLY a JSON "
        'array: [{"title": str, "quote": str}]. quote is the first 4-8 words of the '
        "transcript where that chapter begins, copied VERBATIM so it can be found in "
        "the text. Chapters must be in chronological order. Return at most "
        f"{max_chapters}.\n"
        # Navot, 2026-09-24: the old prompt asked for "concise, natural" titles and
        # got a table of contents - "דעות על השוק", "שאלות על AI". A chapter list is
        # a menu a viewer scans to decide where to jump, so every line has to earn
        # the click on its own.
        "TITLE RULES. Each title is a specific claim, question or reveal that tells "
        "a story - never a topic label.\n"
        "- Name the concrete subject: the person, company, product or number the "
        "chapter is actually about.\n"
        "- BANNED openings and fillers: 'דעות על', 'שאלות על', 'מה קורה ב', 'דיון על', "
        "'הכל על', 'קצת על'. A title that would still fit a different episode is wrong.\n"
        "- Weak: 'דעות על השוק: וונדרפול, מודלים והייפ'. Strong: 'וונדרפול גייסה מיליארד "
        "- ומה זה אומר על השוק'.\n"
        "- Weak: 'אקס גוגל וטייטלים בלינקדאין'. Strong: 'למה \"אקס גוגל\" בלינקדאין "
        "עובד נגדך'.\n"
        "- Under 50 characters, or YouTube truncates it.\n"
        "- Never promise something the chapter does not deliver, and never invent a "
        "fact, name or number that is not said in that chapter.\n"
        # Navot, 2026-09-24: the hook rules made titles finer-grained, and the
        # model spent its whole budget on the first half - a 67-min episode came
        # back with nothing after 43:00, losing one of its strongest segments.
        "COVERAGE. The chapters must span the WHOLE episode, from the first "
        "segment to the last. Never leave a stretch longer than about 8 minutes "
        "without a chapter, and do not cluster them in the opening half - budget "
        "your chapters across the full transcript BEFORE writing them. The final "
        "chapter should sit near the end of the transcript, not in the middle."
        + BRAND_RULE
    )
    user = f"Transcript segments:\n{_numbered(segments)}"

    def validate(obj):
        if not isinstance(obj, list) or not obj:
            raise GenerationError("expected a non-empty array")
        chapters: list[Chapter] = []
        cursor = 0
        for item in obj:
            seg = _locate(item.get("quote", ""), segments, cursor)
            if seg is None:
                continue  # drop unlocatable chapter rather than fail the batch
            chapters.append(Chapter(start=seg.start, title=item["title"].strip()))
            cursor = seg.index + 1
        if not chapters:
            raise GenerationError("no chapters could be located in the transcript")
        return chapters[:max_chapters]  # enforce the cap in code; the prompt alone isn't reliable

    return call_claude_json(system, user, validate, titler=titler)


def make_shownotes(segments: list[Segment], titler: str = "api") -> dict:
    if not segments:
        return {"summary": "", "bullets": []}
    system = (
        "You write Hebrew show notes for a podcast episode. Return ONLY JSON: "
        '{"summary": str, "bullets": [str, ...]}. summary is one Hebrew paragraph; '
        "bullets are 3-6 short Hebrew highlights." + BRAND_RULE
    )
    user = " ".join(s.text for s in segments)

    def validate(obj):
        if not isinstance(obj, dict) or "summary" not in obj:
            raise GenerationError("expected {summary, bullets}")
        obj.setdefault("bullets", [])
        return obj

    return call_claude_json(system, user, validate, titler=titler)


def make_youtube_tags(segments: list[Segment], shownotes: dict | None = None,
                      titler: str = "api") -> dict:
    """YouTube description hashtags + the comma-separated tags field.

    Navot, 2026-09-24. Two different surfaces with different rules: the
    DESCRIPTION takes a handful of hashtags (YouTube only renders the first
    three above the title, so order matters), while the TAGS field is a
    search-recall tool - it is never shown to a viewer, so it should carry the
    spellings people actually type, not the correct ones.

    That is why misspellings and Latin/Hebrew variants of every name belong in
    tags and never in hashtags: a hashtag is public and a typo there looks
    illiterate, while a typo in tags is the whole point.
    """
    if not segments:
        return {"hashtags": [], "tags": []}
    notes = ""
    if shownotes:
        notes = (shownotes.get("summary") or "") + "\n" + "\n".join(shownotes.get("bullets") or [])
    system = (
        "You prepare YouTube metadata for a Hebrew tech podcast (Weekly Sync / "
        "וויקלי סינק, hosts נבות וולק and תור צוק). Return ONLY JSON: "
        '{"hashtags": [str, ...], "tags": [str, ...]}.\n'
        "hashtags: 7-8 items for the DESCRIPTION. Each starts with '#'. Join a "
        "multi-word hashtag with underscores (#בינה_מלאכותית). Hebrew unless the "
        "term is normally Latin (#AI, #startup). Put the most specific first - "
        "YouTube shows only the first three above the title. No spaces, no typos.\n"
        "tags: 25-40 items for the video's TAGS field, plain text, no '#'. This "
        "field is invisible to viewers and exists for search recall, so include "
        "for EVERY person, company, product and show named in the episode: the "
        "Hebrew spelling, the Latin spelling, and the misspellings a viewer "
        "would plausibly TYPE (missing yud, swapped vav/vav-vav, phonetic "
        "guesses). Also include the episode's topics as search phrases. Never "
        "invent a name that is not in the material."
    )
    user = (notes + "\n\n" + " ".join(s.text for s in segments))[:60000]

    def validate(obj):
        if not isinstance(obj, dict):
            raise GenerationError("expected an object")
        hs = [h if h.startswith("#") else "#" + h for h in (obj.get("hashtags") or [])]
        hs = [h.replace(" ", "_") for h in hs]
        tags = [t.strip() for t in (obj.get("tags") or []) if t and t.strip()]
        if not hs or not tags:
            raise GenerationError("expected non-empty hashtags and tags")
        # YouTube rejects a tags field over 500 characters including commas.
        kept, total = [], 0
        for t_ in dict.fromkeys(tags):
            if total + len(t_) + 2 > 500:
                break
            kept.append(t_); total += len(t_) + 2
        return {"hashtags": list(dict.fromkeys(hs))[:8], "tags": kept}

    return call_claude_json(system, user, validate, titler=titler)


def make_quotes(
    segments: list[Segment],
    titler: str = "api",
    min_sec: float = 18.0,
    max_sec: float = 45.0,
    min_score: int = 7,
) -> list[Quote]:
    """Select scroll-stopping short-form clips. Each must open with a hook and be a
    complete, self-contained thought — the two things (per short-form research) that
    decide whether a clip holds past the first 3 seconds. Enforces a hook-strength
    score gate and a minimum length in code, so weak/fragment clips are dropped even
    if the model proposes them."""
    if not segments:
        return []
    audio_end = segments[-1].end
    system = (
        "You select and EDIT scroll-stopping short-form clips from a Hebrew podcast "
        "(for Reels / TikTok / Shorts). " + CLIP_RULES + " Return ONLY a JSON array: "
        '[{"title": str, "hook_type": str, "hook_reason": str, "score": int, "beats": '
        '[{"quote_start": str, "quote_end": str}], "hook_variants": [str, str], '
        '"hook_variant_types": [str, str], "alt_opening": str, "alt_hook_type": str}]. '
        + CLIP_FIELDS +
        " Only include clips you would score 7 or higher." + performance_hint()
    )
    user = f"Transcript segments:\n{_numbered(segments)}"

    def validate(obj):
        if not isinstance(obj, list):
            raise GenerationError("expected an array")
        quotes = [q for q in (
            resolve_clip_item(item, segments, audio_end, min_sec, max_sec, min_score)
            for item in obj
        ) if q is not None]
        if not quotes:
            raise GenerationError("no clips met the hook-strength / length bar")
        return quotes

    return call_claude_json(system, user, validate, titler=titler)


def _clip_words(segments: list[Segment], start: float, end: float) -> list[dict]:
    """Per-word caption timing for one clip, times RELATIVE to the clip start (t=0 at
    `start`). If a segment in range has no word timestamps, distribute its text evenly
    across the segment so the karaoke never gets null gaps (clips.json contract)."""
    out: list[dict] = []
    for s in segments:
        if s.end < start or s.start > end:  # segment fully outside the clip
            continue
        if s.words:
            for w in s.words:
                if start <= w.start <= end:
                    item = {
                        "t": round(w.start - start, 3),
                        "d": round(max(w.end - w.start, 0.01), 3),
                        "w": w.text.strip(),
                    }
                    # Only present when the transcript stored it, so older
                    # clips.json files and the equality tests stay unchanged.
                    prob = getattr(w, "probability", None)
                    if prob is not None:
                        item["p"] = round(float(prob), 3)
                    out.append(item)
        else:
            toks = s.text.split()
            if not toks:
                continue
            seg_start, seg_end = max(s.start, start), min(s.end, end)
            step = max(seg_end - seg_start, 0.01) / len(toks)
            for i, tok in enumerate(toks):
                out.append({"t": round(seg_start - start + i * step, 3), "d": round(step, 3), "w": tok})
    return out


def clip_spec(q: Quote, segments: list[Segment], clip_id: str) -> dict:
    """One clips.json entry from a resolved Quote. A multi-beat quote becomes a
    `segments` list (each beat with its own beat-relative words) — the renderer
    cuts the gaps; a single beat stays the flat legacy shape."""
    spec = {
        "id": clip_id,
        "start": round(q.start, 3),
        "end": round(q.end, 3),
        "hook": q.text,
        "hook_variants": list(q.variants),
        "focus": None,
    }
    # Optional on purpose: a Quote built from an old pool has no label, and
    # readers that predate hook_type must keep working.
    if q.hook_type:
        spec["hook_type"] = q.hook_type
    if q.hook_reason:
        spec["hook_reason"] = q.hook_reason
    if q.alt_opening:
        spec["alt_opening"] = q.alt_opening
    if q.alt_hook_type:
        spec["alt_hook_type"] = q.alt_hook_type
    if any(q.variant_types):
        spec["hook_variant_types"] = list(q.variant_types)
    beats = q.beats or ((q.start, q.end),)
    if len(beats) > 1:
        spec["segments"] = [
            {"start": round(s, 3), "end": round(e, 3),
             "words": _clip_words(segments, s, e)}
            for s, e in beats
        ]
    else:
        spec["words"] = _clip_words(segments, beats[0][0], beats[0][1])
    return spec


def make_clips(segments: list[Segment], titler: str = "api") -> list[dict]:
    """Clip specs for the social-clipper: reuse the pull-quote ranges + hooks, attach
    clip-relative per-word timings. Returns the `clips` array of the clips.json contract."""
    return [clip_spec(q, segments, f"clip-{i}")
            for i, q in enumerate(make_quotes(segments, titler=titler), 1)]


# --- Brand pops: entities said aloud -> a 1s logo card at the exact word. ---

BRANDS_DIR = Path.home() / ".sofit" / "assets" / "brands"


def _brand_slug(term: str) -> str:
    return term.strip().replace(" ", "-").replace("\u05f4", "").lower()


def find_brand_image(term: str) -> str | None:
    """Curated machine-local library lookup (~/.sofit/assets/brands/<slug>.png).
    Logos must be REAL - never generated - so the library grows by hand:
    detection reports misses and the user drops files in."""
    if not BRANDS_DIR.exists():
        return None
    slug = _brand_slug(term)
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        q = BRANDS_DIR / f"{slug}{ext}"
        if q.exists():
            return str(q)
    # loose match: slug contained in a filename (multi-word Hebrew names)
    for q in BRANDS_DIR.iterdir():
        if slug in q.stem.lower():
            return str(q)
    return None


def detect_brand_mentions(clip: dict, titler: str = "api",
                          max_pops: int = 2) -> list[dict]:
    """Find brand/place/product mentions inside one clip spec and time them.

    Returns [{"term", "at", "dur", "span", "image"|None}] - "at" is span-local
    seconds of the first word of the mention. Zero results is a legit outcome
    (same philosophy as cutaways: only CONCRETE named entities, never themes).
    """
    spans = clip.get("segments") or [{"start": clip.get("start"),
                                      "words": clip.get("words") or []}]
    numbered = []
    for si, sp in enumerate(spans):
        text = " ".join(w["w"] for w in sp.get("words") or [])
        numbered.append(f"[span {si}] {text}")
    system = (
        "You extract CONCRETE named entities - brands, businesses, places, "
        "products - that are SAID ALOUD in a Hebrew podcast clip transcript. "
        "Only names a listener would recognize as a specific entity "
        "(e.g. וואטסאפ, שימורי איכות, חולון); never themes, people, or vague "
        "references. Return JSON: {\"mentions\": [{\"span\": int, "
        "\"term\": str, \"quote\": str}]} where quote is the mention's "
        "exact first word copied VERBATIM from the transcript. At most "
        f"{max_pops} mentions; an empty list is a perfectly good answer."
    )

    def validate(obj):
        if not isinstance(obj, dict) or not isinstance(obj.get("mentions"), list):
            raise GenerationError("bad mentions shape")
        return obj

    obj = call_claude_json(system, "\n".join(numbered), validate, titler=titler)
    out = []
    for m in obj["mentions"][:max_pops]:
        si = int(m.get("span", 0))
        if si >= len(spans):
            continue
        words = spans[si].get("words") or []
        qnorm = _norm(str(m.get("quote", "")))
        at = None
        for w in words:
            if qnorm and _norm(w["w"]).startswith(qnorm[:4]):
                at = w["t"]
                break
        if at is None:
            continue
        out.append({"term": m["term"], "at": round(float(at), 2), "dur": 1.3,
                    "span": si, "image": find_brand_image(m["term"])})
    return out
