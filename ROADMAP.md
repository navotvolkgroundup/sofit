# sofit roadmap

Feature ideas, grounded in what's actually working for short-form social video
(hooks, retention, captions). Research basis: `/last30days` run 2026-07-25 on
short-form hooks/retention/captions (raw: `~/Documents/Last30Days/short-form-video-hooks-retention-and-captions-raw-v3.md`).

The through-line from the research: **the first 1-3 seconds decide the clip, most
viewers watch on mute, and word-by-word captions are the retention tool.** sofit
already does the caption part; the gap is the hook.

## Already shipping (keeps us on the current meta)
- **Word-by-word / karaoke captions** - Rubik-Black, white + outline, lower-middle
  third, per-word highlight. This is the most-used 2026 style; no change needed.
- **Hook-aware clip selection** - `make_quotes` gates on a hook score and a length
  floor. Foundation for the upgrade below.
- **Speaker-tracking face crop + logo** - clean, un-flashy visual layer. Working
  editors say story + hard cuts beat flashy VFX, so this stays deliberately simple.

## Done (2026-07-25)

### 1. Caption-first hook card ✅
Each clip's `hook` is burned large, bold, high-contrast in the upper third for the
opening ~1.8s, so a muted scroller (~85% of short-form viewers) is stopped by the frame
rather than a spoken line. Rides the existing per-frame Pillow pass (no extra encode),
RTL-correct, wraps and caps at 12 words. On by default; `--no-hook-card` / `hook_card=False`
turns it off. Verified on a real WS203 render: card at t=0.6s, gone by t=3.5s with the
karaoke caption resuming.
- `render.py` (`_burn_captions_pillow`, `extract_clip`, `render_clips`), `cli.py`, `mcp_server.py`

### 2. Hook scorer: reward the unanswered question ✅
`make_quotes` now explicitly instructs and scores for a **curiosity gap** - a hook that
raises a question the clip has not yet answered - and scores highest when the hook is the
very first thing said rather than buried after throat-clearing. Payoff must close the gap.
- `generate.py` (`make_quotes` system prompt)

### 3. Default clip length 20-45s ✅
`max_sec` default 90s → 45s, matching the researched sweet spot (21-34s highest
engagement, 20-45s for podcast clips). Still overridable per call.
- `generate.py` (`make_quotes`)

### 4. Hook is a first-class, editable field ✅
Falls out of #1: the card renders from the clip spec's `hook`, so testing a different hook
is a one-line edit to the clips.json plus `--render-from ... --only clip-N` - no
re-selection, no re-transcription.

### 5. Retention-shaped trimming: second zero IS the hook ✅
`make_quotes` located the hook's *segment* and started the clip at that segment's first
word, so any throat-clearing in the same segment ("אז... כן, אה,") played before the hook -
burning a chunk of the decisive first seconds. New `_hook_word_start` matches the hook
phrase against the segment's punctuation-stripped word concatenation and snaps `start` to
the hook's own first word. Forward-only, so it never pulls in earlier speech.
Measured on the real WS203 transcript (676 matching segments, simulating a hook at word 4):
median **1.12s** of filler avoided, p90 1.9s, max 5.6s. Verified captions stay in sync -
`_clip_words` re-bases at the snapped word (`t = 0.0`).
- `generate.py` (`_hook_word_start`, `make_quotes`)

### 6. Hook variants (A/B the opener) ✅
Selection now also returns 2 ALTERNATE hook lines per clip, each deliberately a different
angle from the primary (question vs bold claim vs surprising number), stored as
`hook_variants` in the spec. `--hook-variant N` (1-based; 0 = primary) renders with an
alternate and writes `<id>.hookN.mp4`, so A/B renders sit side by side instead of
overwriting. Out-of-range variants warn and fall back to the primary. This closes the
research's core improvement loop: same moment, different openers, let the retention curve
decide.
- `generate.py` (`Quote.variants`, `make_quotes`, `make_clips`), `render.py`, `cli.py`,
  `mcp_server.py`, `skills/sofit/clips.py`

All six roadmap items from the 2026-07-25 research pass are shipped.

### Cleanup: one selection path, one prompt contract ✅
Building the above exposed the reason the pool had drifted: `make_quotes` and the skill's
candidate-pool generator were parallel copies of the same selection logic *and* the same
prompt contract. Both are now single-sourced in `generate.py`:
- `resolve_clip_item(item, segments, audio_end, ...) -> Quote | None` — the score gate,
  quote→segment location, hook-word snap, length gates, and variant cleanup, in one place.
  Returns the existing `Quote` type, so no new type was needed. Pool logic: 28 lines → 12.
- `CLIP_RULES` / `CLIP_FIELDS` — the clip-quality contract and JSON field meanings, composed
  into both prompts. This is the exact drift that let the pool ask for 20-60s clips while
  the code clamped at 45.

## Real web footage cutaways

Opt-in `--web-cutaways` extends the existing visual planner and renderer with
YouTube/Commons discovery and explicit HTTPS video links, provenance, bounded
coarse-to-fine frame review and cached silent excerpts. Recent-upload preferences
and an explicit date cutoff support current topics. `--web-cutaways-safe-only` adds a conservative metadata
allowlist. Search/selection failures retain the recording or use generated art
when enabled. Audio-only web mode targets 85% moving footage, with explicit
coverage controls and measured render reports. Episode context, exact subject/version
filters and publisher preferences keep discovery specific. See
[the architecture and limits](docs/web-footage.md).

## Proposed (next)

Nothing queued. The natural next input is real performance data: once clips with different
hooks have run on TikTok/Reels/Shorts, feed the retention numbers back in and let the
scorer learn from what actually held viewers, rather than from research priors.

## Explicitly NOT doing
- Flashy transitions / VFX / animated backgrounds. Multiple senior editors flag these
  as dating fast and distracting from story. Keep the visual layer minimal.
- **Background music bed under clips.** Researched (2026-08-06) and then actually
  tested on five WS205 clips with the show's own beat, sidechain-ducked at -10 dB and
  again at -14 dB. Verdict after listening: it does not work for this content.
  Two independent reasons, so don't revisit without new information:
  - *No reach upside, structurally.* The trending-audio boost comes from the sound ID
    the platform assigns when audio is picked IN-APP. Music burned into an mp4 has no
    sound ID and joins no sound graph. A file renderer cannot buy that benefit.
    Weekly Sync is also a business account, limited to the Meta Sound Collection, so
    trending commercial tracks are off the table regardless, and 2026 fingerprinting
    catches short beds - a false positive mutes the WHOLE track, killing a
    talking-head clip.
  - *No craft upside here.* These are dense Hebrew talking-head arguments with
    word-by-word captions; the viewer is already reading and listening at once. The
    usual pro-bed case (smoothing hard cuts) was measured on the WS204 combo and did
    not apply: the two sides of a 4-minute splice were -21.4 vs -22.2 dB with
    identical peaks, so there was nothing to mask.
  No code was written for this - the test ran as a throwaway ffmpeg pass, which is why
  there is nothing to revert.

Batch footage acquisition now shares source indexes and versioned visual evidence,
combines multiple verified excerpts per beat, and renders clips as ready. Progress
and cache lifecycle commands support small cold/warm validation; see
[performance validation](docs/footage-performance.md). Broad real-source quality
and throughput evaluation remains necessary; fixture coverage is not a guarantee
of equivalent coverage on arbitrary topics.
