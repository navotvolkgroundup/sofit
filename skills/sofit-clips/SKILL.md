---
name: sofit-clips
description: Make captioned vertical (9:16) social clips from a Hebrew podcast episode (Weekly Sync) with sofit — suggest candidate moments, let the user pick, then render with speaker-tracking face crop, word-by-word Hebrew captions, and the Weekly Sync logo. Use when the user wants social clips / reels / shorts / TikToks from an episode. Part of the sofit tool (see /sofit for the full pipeline).
---

# sofit-clips

Episode → picked moments → captioned 9:16 clips. Suggest → user picks → render (never auto-render the whole pool).

## Home
```
HC=/Users/navotv/src/hebrew-chapters          # repo dir (name kept; brand is "sofit")
PY="$HC/.venv/bin/python"                       # venv python
SOFIT="$HC/.venv/bin/sofit"                     # CLI
SKILL=~/.claude/skills/sofit                    # holds clips.py (shared with /sofit)
LOGO="/Users/navotv/Downloads/logo weekly-01.png"   # Weekly Sync wordmark (transparent PNG)
```
<!-- If the repo/venv moves, update HC. -->

Needs the transcript cached (run `/sofit-transcribe` first if not).

## Per-show recipes

**הקרנף (audio-only, daily)** — feed `https://feeds.megaphone.fm/POLTD2316968013`:
```bash
export SOFIT_MUSIC=~/Downloads/karnaf-theme.mp3   # the show's outro theme (user-confirmed)
export SOFIT_CTA="הקרנף — בכל אפליקציות הפודקאסטים"
# no logo; cover art + brand-amber captions come from the mp3's embedded art automatically
```
mp3-only ⇒ audiogram mode auto-triggers. If the theme file is missing, re-extract:
whisper skips music, so the episode's untranscribed-but-loud transcript gaps
(intro / mid-breaks / outro) are the music candidates — cut them with ffmpeg and
let the user pick by ear (the outro is the confirmed theme).

**Weekly Sync (video)** — `LOGO` above, no music bed (video path), face-crop active.

## 0. Refresh the trend playbook — ONLY if stale

Short-form technique moves slowly (the 2026-07-25 pass found the best playbooks were
evergreen 2024-25 videos; the last-7-day layer was hiring posts and generic tips), so
this is a staleness check, not a per-episode ritual. Run it:

```bash
ls -l ~/Documents/Last30Days/short-form-video-hooks-retention-and-captions-raw-v3.md
```

- **Newer than ~30 days** → skip the research, apply the playbook below. Say one line:
  "playbook is N days old, skipping research."
- **Older than ~30 days, or missing** → run
  `/last30days short form video hooks retention and captions`, then update the playbook
  below with anything that actually CHANGED. Do not rewrite it to restate the same rules
  in new words — the point is catching a shift, not regenerating prose.

If the research contradicts a **code-enforced** rule below, that is a tool change (say so
and open it as work), not something to fix by hand at cut time.

## Current playbook (from /last30days, 2026-07-25)

Enforced in code — nothing to do at cut time, they happen automatically:
| Rule | Where |
|---|---|
| Clip opens ON the hook (no throat-clearing before it) | `generate.py` word-snap |
| 20-45s clip length | `resolve_clip_item` length bar |
| Caption-first hook card, first ~1.8s, auto-fit to 2 lines | `render.py` `_fit_hook_card` |
| Word-by-word karaoke captions, white + outline, lower-middle third | `render.py` |
| 2 alternate hooks per clip for A/B | `--hook-variant N` |

Needs judgment when picking from the pool — the tool can't decide these:
- **Prefer a curiosity gap**: an opener posing a question the clip hasn't answered yet.
  Hard numbers ("44% צועקים נציג") and contrarian claims ("זה בולשיט") both work.
- **Keep the visual layer plain.** Senior editors consistently say flashy transitions and
  VFX date fast; story and hard cuts win. Don't add motion for its own sake.
- **A/B the opener when a clip matters** rather than agonizing over one line — render 2
  variants and let the retention curve decide.

## 1. Candidate pool → user picks
```bash
"$PY" "$SKILL/clips.py" pool "<episode.mp4>"          # writes <episode>.pool.json + a numbered table
```
Present the table; ask which numbers to render. Build the spec from the picks:
```bash
"$PY" "$SKILL/clips.py" build "<episode.mp4>" "<episode>.pool.json" --pick 2,4,7
```

## 2. Render the picked clips (slow — background)
```bash
"$SOFIT" --render-from "<episode>.clips.json" --render-clips "<out_dir>" --logo "$LOGO"
```
- Crop-to-fill 9:16, speaker-tracking face crop, bold Hebrew word-highlight captions, logo top-left.
- Audio-only source (mp3, no video track) ⇒ audiogram mode automatically: blurred
  cover-art background + art card + subtle waveform; captions stay the hero. Cover:
  `--cover PATH` / `SOFIT_COVER` env / the mp3's embedded art / gradient fallback.
- `--cta "טקסט"` / `SOFIT_CTA` env ⇒ small where-to-listen line, upper zone, final
  ~2.5s (last span only). `--music theme.mp3` / `SOFIT_MUSIC` ⇒ show's own theme
  looped + loudness-normalized + ducked under speech (audiogram clips only).
- Narrative edits: a clip with `segments` (2+ kept spans) renders each span and
  losslessly concats them — banter between beats is cut automatically. The
  hook card goes on the first span only; captions/face-crop run per span.
- Hesitation sounds are cut out of the clip itself (picture, audio, and the
  karaoke word), on by default. Clear fillers: אמ, אממ, אהה, אהמ, הממ, מממ,
  English um / uh / hmm, and stretched spellings (אממממ, uhhh). אמ uses medial
  mem מ, so it is not the word אם ("if", final mem ם). אה and אם are cut only
  when the token sits between pauses (~0.22s each side, a clip edge counts) or
  the word confidence is low — in a normal sentence they stay.
  `--no-defiller` keeps every hesitation. `--defiller-dry-run` prints each
  token with its source timestamp and does not render. `--defiller-min-gap 0.40`
  (the default) skips a cut that would leave less than 0.40s of speech between
  two cuts. Extra tokens: `SOFIT_FILLERS` / `SOFIT_SHORT_FILLERS` (comma-separated).
  The pass runs on the footage render. `--storyboard` is not defilled.
- Output `<out_dir>/clip-N.mp4` (ids match the pool numbers). Copy to ~/Downloads as `WS<episode>_clip-N.mp4`.
- Per-frame Pillow caption pass ⇒ seconds+ per clip; run in background.
- Set-once logo: `export SOFIT_LOGO="$LOGO"` (then `--logo` optional). `--logo-pos {top-left,top-right,bottom-left,bottom-right}`.
- Each clip opens with its `hook` burned large in the upper third for ~1.8s
  (caption-first hook for muted viewers, since most scroll on mute). Off with
  `--no-hook-card`; to change it, edit `hook` in the clips.json and re-render.
- A/B the opener: each clip carries `hook_variants` (2 alternates). Render one with
  `--hook-variant N` (1-based) — it writes `<id>.hookN.mp4` alongside the original.
- This `--render-from` path is the ONLY one that honors caption fixes (`/sofit-captions`); plain `--render-clips` regenerates from the transcript.

## 3. Log how they performed (closes the loop)
After posting, record the numbers — this is the ONLY step that turns priors into real
signal, and the data is perishable (unrecorded, which hook won is gone).
```bash
"$PY" "$SKILL/clips.py" log WS203 clip-5 "<the hook that was posted>" \
  --platform tiktok --views 12400 --retention 47
```
- `--retention` (percent watched) is the signal that matters; views are confounded by
  posting time and follower count. Log it when the platform gives it to you.
- Use `--variant N` when you posted an alternate hook, so A/B results stay attributable.
- Appends to `~/Documents/sofit-performance.jsonl` (outside the repo — it is public).
  Override with `SOFIT_PERF_LOG`.
- At **8+ rows** pool generation starts including the best/worst real hooks in its prompt
  and weighting them over research priors. Below that it stays silent on purpose — "what
  worked" over 3 posts is noise. Each `log` prints how many rows are still needed.

## Gotchas (learned the hard way)
- **Spot-check every NUMBER against what was actually said.** Hebrew number words
  confuse the transcriber (שלושה/שישה), and numbers are exactly what makes a clip
  credible. WS204 rendered "3 מיליארד" where Navot said 6, and the wrong figure had
  already spread to the hook variant and the show notes. When a number is the point
  of the clip, confirm it before publishing — and fix it in the caption, the
  hook_variants, the pool file and the show notes, not just the caption.
- **Verify with a real rendered frame** — `ffmpeg -ss T -i clip.mp4 -frames:v 1 f.png` and look. Especially Hebrew RTL.
- **Face crop** needs the `crop` extra (opencv, installed); holds through rapid cuts (won't chase every camera cut); falls back to center if no face.
- **Two-person / wide shots**: only one person fits a 9:16 crop — set a clip's `focus` [0,1] in the clips.json to override.
- To fix a caption typo → `/sofit-captions`. To cut a moment out of a finished clip → `/sofit-trim`.
- Write the social copy in Navot's voice via `/nabot` (not here).
