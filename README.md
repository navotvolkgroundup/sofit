# sofit

Auto-generate **chapters**, **show notes**, and **pull-quotes** for Hebrew
podcasts (mp3 or mp4) — locally transcribed, so your audio never leaves your
machine.

Transcription runs on [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
(no file-size limit, free, private), defaulting to the Hebrew-tuned
[`ivrit-ai/whisper-large-v3-turbo-ct2`](https://huggingface.co/ivrit-ai) model —
which transcribes Hebrew far better than stock Whisper. A small text transcript then
goes to Claude, which writes the chapter titles and notes in natural Hebrew for
pennies per episode.

## Install

```bash
pip install sofit-cli
export ANTHROPIC_API_KEY=sk-ant-...
```

**No API key?** If you have Claude Code (a Pro/Max subscription) installed and
logged in, pass `--titler claude-cli` to generate chapters through `claude -p`
using your subscription instead of a key. Slower per run and subject to your
Claude Code usage limits, but no per-episode API cost. The default (`--titler api`)
uses the Anthropic API and produces cleaner structured output.

`ffmpeg` is used as a fallback decoder for exotic containers — install it if you
hit a decode error (`brew install ffmpeg` / `apt install ffmpeg`).

## Usage

```bash
# Chapters to stdout
sofit episode.mp3

# From an RSS feed — processes the latest episode (item 1)
sofit https://feeds.example.com/show.xml --shownotes --out latest
sofit https://feeds.example.com/show.xml --episode 3      # a specific episode
sofit --list-episodes https://feeds.example.com/show.xml  # see the feed first
# From a YouTube URL (needs the youtube extra: pip install 'sofit-cli[youtube]')
sofit https://www.youtube.com/watch?v=VIDEO_ID --shownotes --out episode
# (a direct audio URL works too: sofit https://.../episode.mp3)

# Video podcast + show notes + pull-quotes
sofit episode.mp4 --shownotes --quotes

# YouTube: paste the .txt into your video description (0:00-first, no bidi marks)
sofit episode.mp4 --format youtube --out episode

# Podcast apps via RSS: Podcasting 2.0 chapters JSON to host + reference in your feed
#   <podcast:chapters url="…/episode.chapters.json" type="application/json+chapters" />
sofit episode.mp4 --format podcast --out episode

# Podcast apps via the file (Apple Podcasts etc.): embed markers into the audio
sofit episode.mp4 --embed-into episode.m4a   # writes episode.chapters.m4a
```

### Making chapters show up everywhere
- **YouTube** — `--format youtube`, paste into the description. First chapter at
  `0:00`, ≥3 chapters, ≥10s apart (enforced).
- **Spotify (and Megaphone-hosted shows)** — `--format spotify`, paste into the
  episode description. Spotify parses description timestamps into chapters; it needs
  `0:00` first, ≥3 chapters, and **≥30s apart** (enforced — stricter than YouTube).
  Plain text, no emoji/HTML. Note: if the show uses dynamic ad insertion, mid-roll
  ads shift later timestamps out of sync.
- **Modern podcast apps** (Overcast, Fountain, Podcast Addict) — `--format podcast`
  produces a Podcasting 2.0 JSON; host it and add `<podcast:chapters>` to the RSS item.
  (Doesn't work through Megaphone, which controls its own feed.)
- **Apple Podcasts and file-based players** — `--embed-into audio.m4a|.mp3` writes
  chapter markers directly into a copy of the audio (stream copy, no re-encode).

Example output:

```
‎0:00 — פתיחה וברוכים הבאים
‎3:42 — האורח מספר על ההתחלה
‎18:05 — הטעות הכי גדולה שעשינו
```

### Animated storytime clips (`--storyboard`)

Render a clip as an AI-illustrated "storytime" reel — generated comic-style
scenes instead of the recording, Ken-Burns motion, same karaoke captions,
hook card, and logo:

```bash
export GEMINI_API_KEY=...   # scene images (Nano Banana)
sofit --render-from ep.clips.json --storyboard --only clip-3 \
      --char-ref "Navot=navot.jpg" --char-ref "Guy=guy.jpg" --logo logo.png
```

Claude splits each beat into ~4-9s scenes with English image prompts; Gemini
renders one vertical still per scene, kept consistent by a character sheet
generated once from your `--char-ref` photos (cached as
`characters.sheet.png`). Stills and prompts land in `<clip>.scenes/` so you
can inspect or tweak; re-runs reuse existing stills. `--style` overrides the
default comic-book look (or set `SOFIT_STYLE`).

Add `--animate` (needs `FAL_KEY` from fal.ai) to turn each still into a real
image-to-video shot via Kling (~$0.25-0.50 per scene); failed scenes fall back
to Ken Burns stills, and generated shots cache in `<clip>.scenes/*.mp4`.

Prefer the real footage? `--cutaways` keeps the recording and splices 1-2
short AI-illustrated scenes over it at concrete visual moments ("a Trojan
horse", "a warehouse of goods") while the audio and captions run uninterrupted
- combine with `--animate` for moving shots. Cutaways persist in the clip spec
and cache in `<spec dir>/cutaways/`, so corrected re-renders keep them.

### Close the loop: log every post (`sofit publish-log`)

The clip selector can learn from real performance, but only if posts are
attributed. Record each post the moment you publish it:

```bash
sofit publish-log clips/clip-5.mp4 --episode WS205 --platform tiktok \
    --url https://www.tiktok.com/@show/video/123 --speaker "Tor"
```

The hook variant is inferred from the rendered filename (`clip-5.hook1.mp4` =
variant 1), the hook text is auto-discovered from the newest `*.clips*.json`
spec near the file, duration comes from ffprobe, and a duplicate-URL guard
keeps the log clean. `--speaker` (optional) records who fronts the clip, so
"who should open clips" becomes a measurable question — the selector's
performance hint shows it next to each real hook. Rows land in
`~/.sofit/performance.jsonl` (legacy `~/Documents/sofit-performance.jsonl`
still honored if it's the only log; override with `SOFIT_PERF_LOG` — the
default moved because macOS TCC blocks launchd/cron from Documents) with
metrics left empty for your
analytics-scraper of choice to fill; once 8+ rows carry views/retention,
pool generation starts weighting the hooks that actually held viewers over
its own priors.

## How it works

```
media ─▶ faster-whisper (local, cached) ─▶ transcript
                                              │
              ┌───────────────────────────────┼───────────────┐
              ▼                                ▼               ▼
        Claude: chapters         Claude: show notes   Claude: quotes
```

The transcript is cached (keyed by file hash + model + version), so re-runs and
prompt tweaks skip re-transcribing.

> First run downloads the model (~1.6 GB for the ivrit-ai turbo default). On CPU,
> transcription is roughly real-time; use a smaller `--model` (e.g. `base`) or a GPU
> to go faster. Pass any faster-whisper size name or HF ct2 repo id to `--model`.

## Drive it in natural language (Claude Code skills)

Ready-made [Claude Code](https://claude.com/claude-code) skills run sofit from any directory —
one per service, plus a `sofit` router that walks the full pipeline. Install by copying the
folders into `~/.claude/skills/`, then invoke by name (e.g. `/sofit-clips`):

```bash
cp -R skills/sofit* ~/.claude/skills/
```

| Skill | Does |
|---|---|
| `/sofit` | full pipeline + router to the sub-skills below |
| `/sofit-transcribe` | local faster-whisper transcription (cached, one-time) |
| `/sofit-kit` | chapters + Hebrew show notes + pull-quotes for descriptions |
| `/sofit-clips` | suggest → pick → render captioned 9:16 social clips |
| `/sofit-captions` | fix caption typos, timing-preserving, then re-render |
| `/sofit-trim` | cut a moment out of a finished clip |

The skills live in [`skills/`](skills/); each `SKILL.md` has a `Home` block with paths to
personalize for your machine. See [`skills/README.md`](skills/README.md) for details.

## Run it from an AI app (MCP)

An MCP server lets any MCP-capable client (Claude Desktop, Claude Code, Cursor…)
drive the tool in natural language.

```bash
pip install "sofit-cli[mcp]"     # adds the MCP server
```

Because transcription takes tens of minutes, it's split across three tools so no
single call blocks: **`transcribe_episode(path)`** starts it in the background,
**`transcription_status(path)`** reports `ready`/`running`, and
**`generate_kit(path, chapter_format, shownotes, quotes, …)`** returns the results
once the transcript is cached.

**Claude Desktop** — add to `claude_desktop_config.json` (use the absolute path to
the `sofit-mcp` binary if it's in a venv):

```json
{
  "mcpServers": {
    "sofit": {
      "command": "/path/to/.venv/bin/sofit-mcp",
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    }
  }
}
```

**Claude Code** — `claude mcp add sofit -e ANTHROPIC_API_KEY=sk-ant-... -- /path/to/.venv/bin/sofit-mcp`

Then just ask: *"Transcribe ~/Downloads/ep.mp4"* → wait → *"Now give me Spotify
chapters and Hebrew show notes for it."*

## Notes

- Input: a local mp3/mp4 file, an RSS feed URL (add `--episode N`, default latest;
  `--list-episodes` to inspect), a YouTube URL (needs `pip install 'sofit-cli[youtube]'`),
  or a direct audio URL — all cached after first fetch.
- `--model` (default: ivrit-ai turbo), `--lang` (default `he`), `--max-chapters`,
  `--format {md,txt,youtube,spotify,podcast}`, `--embed-into AUDIO`,
  `--titler {api,claude-cli}`, `--titler-model MODEL`, `--shownotes`, `--quotes`,
  `--clips-json PATH`, `--render-clips DIR`, `--render-from PATH`, `--only ID`,
  `--aspect`, `--out`, `--no-cache`.

### Caption tuning (env vars)

The defaults are tuned for social clips from speech (heavy font, karaoke
highlight, short chunks). For other content — screencasts, static labels,
calmer decks — dial them without code changes (all defaults unchanged;
thanks @zohar for these):

| Variable | Default | Effect |
|---|---|---|
| `SOFIT_MAX_WORDS` | `6` | max words per caption chunk |
| `SOFIT_MAX_SPAN` | `2.6` | max seconds per chunk — raise it so a hand-written static label stays up for its whole span |
| `SOFIT_CAPTION_DIV` | `22` | `font_size = height // DIV`; higher = smaller text |
| `SOFIT_CAPTION_WEIGHT` | `Black` | variable-font weight name (`Bold`, `Medium`, ...) |
| `SOFIT_CAPTION_OUTLINE` | `font_size // 8` | outline thickness in px |
| `SOFIT_CAPTION_ACCENT` | karaoke yellow | `none` disables the active-word highlight |

Other env vars: `SOFIT_LOGO`, `SOFIT_CTA`, `SOFIT_MUSIC`, `SOFIT_COVER` (branding),
`SOFIT_STYLE` / `SOFIT_IMAGE_MODEL` / `SOFIT_VIDEO_MODEL` + `GEMINI_API_KEY` /
`FAL_KEY` (AI visuals), `SOFIT_TITLER_MODEL` (generation model),
`SOFIT_PERF_LOG` (performance log path), `SOFIT_CLI_TIMEOUT` (claude-cli wall clock).

### Render social clips directly (no external tool)
`--render-clips DIR` turns each pull-quote into a vertical (9:16) clip with burned-in
Hebrew captions, written to `DIR`. Crop-to-fill (no letterbox); captions come from the
per-word timings; RTL rendered correctly. Needs the render extra + ffmpeg:
```bash
pip install 'sofit-cli[render]'
sofit episode.mp4 --render-clips clips_out --aspect 9:16
```
Also exposed as the MCP `render_clips` tool for the Claude-app interface. (Rendering
logic is self-contained here — no dependency on any other tool.)

**Audio-only podcasts (audiograms).** An mp3 with no video track renders automatically
as an audiogram: blurred cover art background with a slow push-in, a rounded art card,
a subtle waveform strip — the karaoke captions stay the hero element. Cover art comes
from `--cover PATH`, else the `SOFIT_COVER` env var, else the mp3's embedded art, else
a plain dark gradient. Everything else (hook card, logo, captions, trims) works the same.
```bash
sofit episode.mp3 --render-clips clips_out   # audiogram mode, auto-detected
```

**Closing CTA.** `--cta "טקסט"` (or the `SOFIT_CTA` env var) draws a small
call-to-action line in the upper zone for the final ~2.5s of each clip — a
"where to listen" nudge over the still-playing audio, instead of a dead end
card that kills retention and breaks loops.

**Theme-music bed (audiogram clips).** `--music theme.mp3` (or `SOFIT_MUSIC`)
mixes the show's own theme quietly under the voice: looped, loudness-normalized
(so any mastering level lands ~10dB under speech), and sidechain-ducked so words
stay clear. Use the show's own theme — it's rights-cleared by definition; for
trending audio, add it in-app when posting instead.

**Framing.** By default the crop is centered. With the optional `crop` extra
(`pip install 'sofit-cli[crop]'`, adds OpenCV) each clip is auto-cropped to
center on a detected face — so a speaker sitting off to one side is framed instead
of the empty background beside them. When there's no clear face (a wide two-shot,
backs of heads) it stays centered. To override, set a clip's `focus` to a value in
`[0,1]` (`0`=left, `0.5`=center, `1`=right) in the clips JSON.

**Logo / watermark.** `--logo PATH` overlays a logo (a transparent PNG) on every
rendered clip, fixed in a corner (it doesn't pan with the face tracking). It's trimmed
to its opaque bounds and sized to the frame, so a source PNG with big transparent margins
still sits tight. Default corner is top-left (the safest spot on 9:16 — clear of the face,
the captions, the bottom platform UI, and TikTok's right-side buttons); change with
`--logo-pos {top-left,top-right,bottom-left,bottom-right}`. Also a `logo` param on the
`render_clips` / `correct_clip` MCP tools.
```bash
sofit episode.mp4 --render-clips out --logo weeklysync.png
```
To brand every render without passing the flag, set it once:
`export SOFIT_LOGO=/path/to/weeklysync.png` (an explicit `--logo` still wins).

**Opening hook card.** Most short-form viewers scroll on mute, so each clip opens with
its `hook` burned large and high-contrast in the upper third for the first ~1.8s — the
frame itself stops the scroll, not a spoken line. It's on by default (the hook text comes
from the clip spec, so editing `hook` in the clips.json and re-rendering changes the card);
turn it off with `--no-hook-card`, or the `hook_card` param on the MCP render tools.

**A/B-testing hooks.** Each clip also carries `hook_variants` — 2 alternate opener lines
taking a different angle from the primary (if the hook is a question, an alternate might be
a bold claim or a surprising number). Render one with `--hook-variant N` (1-based; `0` is the
primary). Variant renders are written to `<id>.hookN.mp4` so they sit alongside the original
instead of overwriting it — post them and let the retention curve pick the winner.
```bash
sofit --render-from clips.json --render-clips out --only clip-3                   # primary hook
sofit --render-from clips.json --render-clips out --only clip-3 --hook-variant 1  # -> clip-3.hook1.mp4
```

**A/B-testing the hook STYLE.** `--hook-style persistent` swaps the flash card for a
smaller boxed quote that stays on screen for the whole clip — a viewer landing
mid-scroll always has context. Writes `<id>.pers.mp4` alongside the flash render, and
`sofit publish-log` tags the row `hook_style=persistent` so retention can be compared
per style once the metrics land.

**Speaker name tags.** Add `speaker_tags` to a clip's spec entry to burn a lower-third
the first time a person appears — a white rounded card on the right with the name bold,
the title smaller in gray, and an accent bar:
```json
"speaker_tags": [{"name": "קארינה רובינשטיין", "title": "שותפה, גראונד-אפ ונצ׳רס",
                  "at": 2.0, "dur": 3.0, "span": 0}]
```
`at` is seconds within its span (`span` defaults to 0, like `cutaways`); `dur` defaults
to 3.0. Multiple tags per clip are fine — stagger their `at` windows.

**Fixing caption typos.** Transcription isn't perfect — it occasionally mangles a
word or an English brand name (e.g. `OpenAI` → `אופן-איי-איי`). Correct captions
*without re-transcribing* and re-render, keeping the karaoke timing aligned:

1. Render once — the spec is saved automatically. `sofit episode.mp4 --render-clips out`
   drops `out/episode.clips.json` (named after the media, next to the `clip-N.mp4` files it
   just rendered) so the spec and its clips travel together. (Pass `--clips-json PATH` to
   choose your own path. Re-running `--render-clips` won't overwrite a spec you've corrected.)
2. Correct it. From the Claude app, the MCP tool `correct_clip(clips.json, find, replace)`
   fixes every clip by default (recurring names appear in many) — pass `clip_id` to
   scope to one — and re-renders the affected clips. A multi-token find collapses to the
   replacement, merging the tokens' time span so the highlight stays in sync.
3. Or render a corrected clips.json yourself: `sofit --render-from clips.json --render-clips out`
   (add `--only clip-3` for a single clip). This is the **only** render path that honors
   corrections — plain `--render-clips` regenerates from the transcript and will warn you
   if a clips.json is sitting nearby.

### Feeding a social-clip renderer
`--clips-json PATH` writes a clip spec for a downstream vertical-clip renderer (e.g. a
Remotion app): each pull-quote becomes `{id, start, end, hook, focus, words}` where cut
times (`start`/`end`) are absolute episode seconds and caption `words[].t` are relative
to the clip start. The renderer crops to 9:16, burns Hebrew captions from `words`, and
adds a hook/branding card. `sofit` finds and times the moments; it does not
render video.
```bash
sofit episode.mp4 --clips-json clips.json
```
- Chapter timestamps come from Whisper, never the LLM — Claude only picks which
  segment a chapter starts on, and that choice is validated.

**Learning from what actually worked.** Clip selection starts from short-form research
priors, but the honest signal is your own numbers. Record each posted clip:
```bash
python skills/sofit/clips.py log WS203 clip-5 "<hook posted>" --platform tiktok --retention 47
```
Once the log has 8+ rows, pool generation includes the best- and worst-performing real
hooks in its prompt and weights them over the priors. Below 8 it stays quiet — "what
worked" over three posts is noise. The log lives at `~/Documents/sofit-performance.jsonl`
(outside the repo; override with `SOFIT_PERF_LOG`).

## Roadmap

Planned work, grounded in what's currently working for short-form social video, is
tracked in [ROADMAP.md](ROADMAP.md).

MIT licensed.

### Model transports for library integrations

Claude remains the default. `--titler api` uses Anthropic's API and
`--titler claude-cli` uses the authenticated Claude CLI. The existing
`call_claude_json` API keeps JSON validation and one retry on invalid output.
Its optional `images` argument accepts local JPEG paths; text-only calls keep
the existing transport behavior. Image calls use `SOFIT_VISUAL_TIMEOUT`
(default 180 seconds), capped by `SOFIT_CLI_TIMEOUT` for CLI calls.

Library integrations can register a process-local text/image transport:

```python
from sofit.model_backends import register_backend
from sofit.generate import call_claude_json

def transport(system, user, model, images=None):
    # Call your provider and return its response text.
    return '{"ok": true}'

register_backend("example", transport, cache_key="example-v1")
result = call_claude_json("Return JSON", "Check", lambda obj: obj,
                          titler="example", model="your-model")
```

Use a distinct, versioned cache key when provider behavior changes. Registration
is explicit in Python; clip specifications cannot load or register transports.
The CLI continues to offer only its built-in providers. Unknown provider names
raise `ValueError`.
