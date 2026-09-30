---
name: sofit
description: Turn a Hebrew podcast episode (Weekly Sync) into chapters, show notes, pull-quotes, and captioned vertical (9:16) social clips — then fix caption typos conversationally, add the show logo, and cut segments. Use when the user wants to process a Hebrew podcast / Weekly Sync episode, make social clips, generate chapters or show notes, fix a caption, add a logo/watermark to clips, or trim a moment out of a clip. Works from ANY directory by pointing at the sofit repo.
---

# sofit (portable entry point)

Runs the sofit tool from anywhere. The package is installed in its venv,
so `import sofit` and the `sofit` CLI work regardless of cwd.

## Services (sub-skills)

Each service is also its own focused skill — invoke directly for a single job, or use
this skill end-to-end. All share the same Home/env block below.

| Service | Sub-skill | Does |
|---|---|---|
| Transcribe | `/sofit-transcribe` | local faster-whisper transcription (cached, one-time) |
| Text kit | `/sofit-kit` | chapters + Hebrew show notes + pull-quotes for descriptions |
| Social clips | `/sofit-clips` | suggest → pick → render captioned 9:16 clips (face crop + logo) |
| Fix captions | `/sofit-captions` | correct caption typos, timing-preserving, re-render |
| Trim a clip | `/sofit-trim` | cut a moment out of a finished mp4 |

Typical flow: `/sofit-transcribe` → then `/sofit-kit` and/or `/sofit-clips` → `/sofit-captions`
/ `/sofit-trim` to polish. The full pipeline and gotchas are documented below.

## Home

```
HC=/Users/navotv/src/hebrew-chapters
PY="$HC/.venv/bin/python"          # venv python (has faster-whisper, anthropic, render+crop extras, mcp)
SOFIT="$HC/.venv/bin/sofit"       # CLI
SKILL=~/.claude/skills/sofit   # this dir; holds clips.py
LOGO="/Users/navotv/Downloads/logo weekly-01.png"   # Weekly Sync wordmark (transparent PNG)
```
<!-- If the repo/venv moves, update HC. Repo: github.com/navotvolkgroundup/sofit -->

Generation uses `--titler claude-cli` (shells out to `claude -p`, uses the Claude Code
subscription, no API key). Everything transcribes LOCALLY — audio never leaves the machine.

## The pipeline

```
episode.mp4 ─▶ transcribe (local, cached, LONG) ─▶ transcript
                    │
     ┌──────────────┼───────────────────────────┐
     ▼              ▼                             ▼
  chapters      show notes / quotes        candidate pool ──▶ user picks
  (paste)                                        │
                                                 ▼
                                     clips.json ──▶ render (9:16, face-track crop,
                                                    word-highlight captions, logo)
                                                        │
                                    correct captions ◀──┤──▶ cut a segment
```

## Steps

### 1. Transcribe (long — always background)
~real-time (a 60-min episode ≈ 20-35 min on CPU). Cached by file hash, so it's one-time.
```bash
nohup "$PY" -c "from sofit.transcribe import transcribe; import sys; print(len(transcribe(sys.argv[1])))" "<episode.mp4>" > /tmp/hc_tx.log 2>&1 &
```
Poll `transcription_status` / re-run is instant once cached. Do NOT block on it — set a
background wait and continue when it lands.

### 2. Text kit (chapters + show notes + quotes)
Fast (cached transcript). Paste chapters into the episode description.
```bash
"$SOFIT" "<episode.mp4>" --format spotify --shownotes --quotes --titler claude-cli   # Spotify/Megaphone
"$SOFIT" "<episode.mp4>" --format youtube --titler claude-cli                          # YouTube
```

### 2.5 Trend playbook — staleness check before cutting clips
Short-form technique moves slowly, so this is a check, not a ritual. If
`~/Documents/Last30Days/short-form-video-hooks-retention-and-captions-raw-v3.md` is newer
than ~30 days, skip the research and apply the playbook in `/sofit-clips`. If it's older or
missing, run `/last30days short form video hooks retention and captions` and update that
playbook with what CHANGED. Most current rules are already enforced in code (hook snap,
20-45s, hook card, karaoke captions, hook variants) — see `/sofit-clips` for which.

### 3. Candidate pool → user picks (suggest → pick → render — the standing flow)
NEVER auto-render the whole pool. Generate candidates, show the table, let the user pick.
```bash
"$PY" "$SKILL/clips.py" pool "<episode.mp4>"          # writes <episode>.pool.json + prints a numbered table
```
Present the table; ask which numbers to render. Then build the spec from the picks:
```bash
"$PY" "$SKILL/clips.py" build "<episode.mp4>" "<episode>.pool.json" --pick 2,4,7
```

### 4. Render the picked clips (9:16 + captions + logo)
```bash
"$SOFIT" --render-from "<episode>.clips.json" --render-clips "<out_dir>" --logo "$LOGO"
```
- Crop-to-fill 9:16, speaker-tracking face crop, bold Hebrew word-highlight captions, logo top-left.
- Narrative edits: a clip with `segments` (2+ kept spans) renders each span and
  losslessly concats them — the filler between beats is cut automatically. The
  hook card goes on the first span only; captions/face-crop run per span.
- Output is `<out_dir>/clip-N.mp4` (ids match the pool numbers). Copy to ~/Downloads as
  `WS<episode>_clip-N.mp4` for the user. This is the ONLY render path that honors caption
  corrections; `--render-clips` on the episode regenerates from the transcript.
- Rendering is slow (per-frame Pillow caption pass, seconds+ per clip) — run in background.
- Logo is set-once via `export SOFIT_LOGO="$LOGO"` (then `--logo` is optional).
- Each clip opens with its `hook` burned large in the upper third for ~1.8s
  (caption-first hook for muted viewers). Off with `--no-hook-card`; edit `hook`
  in the clips.json and re-render to change it.
- A/B the opener: each clip carries `hook_variants` (2 alternates). Render one with
  `--hook-variant N` (1-based) — it writes `<id>.hookN.mp4` alongside the original.

### 5. Fix a caption typo (conversational, timing-preserving)
Edits word TEXT only, never the karaoke timing. Episode-wide by default (recurring names
fix everywhere); pass a clip_id to scope. Multi-token find (e.g. OpenAI→אופן-איי-איי) merges
the token span. Use the MCP `correct_clip` tool, or the library:
```bash
"$PY" - <<'PY'
import json
from sofit import corrections
P="<episode>.clips.json"; d=json.load(open(P)); clips=d["clips"]
n,aff=corrections.correct_clips(clips,"<wrong text>","<right text>")   # +clip_id="clip-2" to scope
print("replaced",n,"in",aff)
if n: json.dump(d,open(P,"w"),ensure_ascii=False,indent=2)
PY
```
Then re-render just the affected clip: `--render-from <clips.json> --only clip-N`.
The find matches punctuation/whitespace-insensitively; verify with a rendered frame.

### 6. Cut a segment out of a clip (post-render trim)
Not in the spec (a clip is one start/end) — do it on the rendered mp4. Captions/audio/logo
stay in sync (all baked into frames). Find the exact window by extracting frames first.
```bash
ffmpeg -v error -i in.mp4 -filter_complex \
"[0:v]trim=0:START,setpts=PTS-STARTPTS[v1];[0:v]trim=END,setpts=PTS-STARTPTS[v2];\
[0:a]atrim=0:START,asetpts=PTS-STARTPTS[a1];[0:a]atrim=END,asetpts=PTS-STARTPTS[a2];\
[v1][v2]concat=n=2:v=1:a=0[v];[a1][a2]concat=n=2:v=0:a=1[a]" \
-map "[v]" -map "[a]" -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
-c:a aac -b:a 128k -movflags +faststart -y out.mp4
```
Caveat: a re-render (step 4) brings the full length back — re-apply the cut after.

## Optional real web footage

For requests to use authentic footage or real B-roll, follow the Real web footage
section in [`../sofit-clips/SKILL.md`](../sofit-clips/SKILL.md). After picking clips,
run the **local editable CLI** with `--render-from <spec> --web-cutaways`.
Audio-only clips target 85% moving footage; use `--footage-coverage 90` for an
explicit target. Supply episode context and exact subject/version, prefer primary
publishers, and inspect the rendered `.coverage.json` gaps as well as the video.
Install the `youtube` extra and Deno or Node 22+ to search YouTube alongside
Commons. Use repeatable `--footage-url URL` for provided YouTube/direct-video
links, and `--footage-after YYYY-MM-DD` for an explicit upload-date cutoff.
`--web-cutaways-safe-only` enables the conservative metadata filter; `--cutaways`
allows generated fallback. Keep the saved plan/provenance, inspect frames and
continuous audio/captions, and use normal `--render-from` for corrected rerenders.
The provider and selection logic belong to Sofit itself; do not recreate them
as ad hoc agent downloads.

## Conventions & gotchas (learned the hard way)

- **Always verify with a real rendered frame**, don't trust code reads — extract with
  `ffmpeg -ss T -i clip.mp4 -frames:v 1 f.png` and look. Especially for Hebrew RTL captions.
- **RTL/bidi**: captions render in visual order; a multi-word English brand (e.g. "Thoma
  Bravo") stays in order (fixed). Single tokens (OpenAI) always fine.
- **Face crop** needs the `crop` extra (opencv, installed); falls back to center if no face.
  It tracks the speaker and holds through rapid cuts (won't chase every camera cut).
- **Two-person / wide shots**: only one person fits a 9:16 crop — a clip's `focus` [0,1]
  overrides the auto crop per clip if it picks wrong.
- **Naming**: one `<episode>.clips.json` spec (episode-level) + `clip-N.mp4` renders
  (clip-level), joined by the `clip-N` id. Keep them together.
- **Backgrounding**: transcribe and render are long. Launch with a background wait; don't
  block. Keep the un-cut/pre-fix versions so edits are reversible.
- **Social copy**: write posts in Navot's voice via the `nabot` skill (not here).
