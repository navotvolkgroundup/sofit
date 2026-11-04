---
name: sofit-kit
description: Generate the text kit — chapters, Hebrew show notes, and pull-quotes — for a Hebrew podcast episode (Weekly Sync) with sofit, formatted for YouTube / Spotify / podcast-app descriptions. Use when the user wants chapters, show notes, or quotes for an episode (not the video clips). When the same job includes the YouTube episode title or 16:9 cover, follow docs/youtube-packaging.md. Part of the sofit tool (see /sofit for the full pipeline).
---

# sofit-kit

Turns a (cached) transcript into text you paste into the episode description.

## Home
```
HC=/Users/navotv/src/hebrew-chapters          # repo dir (name kept; brand is "sofit")
SOFIT="$HC/.venv/bin/sofit"                     # CLI
```
<!-- If the repo/venv moves, update HC. -->

Needs the transcript cached first — run `/sofit-transcribe` if it isn't (this step is fast).
Generation uses `--titler claude-cli` (shells out to `claude -p`, uses the Claude Code
subscription, no API key).

## Run
```bash
"$SOFIT" "<episode.mp4>" --format spotify --shownotes --quotes --titler claude-cli   # Spotify/Megaphone
"$SOFIT" "<episode.mp4>" --format youtube --titler claude-cli                         # YouTube
```
Formats: `youtube` (≥10s gaps), `spotify` (≥30s gaps, for Spotify/Megaphone episode
descriptions), `podcast` (Podcasting 2.0 JSON for RSS), `md`/`txt` (read), plus
`--embed-into AUDIO` (markers into a copy of the audio, Apple Podcasts).

Gotchas:
- **Megaphone** controls its own feed → PC2.0 JSON won't attach; the only path is pasting
  the `spotify` timestamp block into the episode description. Dynamic ad insertion desyncs later timestamps.
- **YouTube** output must NOT contain LRM/bidi marks (breaks YouTube's parser — the format handles this).
- Chapter timestamps come from Whisper, never the LLM (Claude picks the boundary quote; code locates + validates it).

## YouTube title and cover

Chapters and show notes are not the episode title. When this job also writes the YouTube title or makes the 16:9 cover (`<EP>_episode-cover.jpg`), read and follow [`docs/youtube-packaging.md`](../../docs/youtube-packaging.md) first (`$HC/docs/youtube-packaging.md` once this skill is copied out of the repo). `/sofit-seo` checks the same file at upload. Show Navot the cover before it ships.
