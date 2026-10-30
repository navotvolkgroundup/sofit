---
name: sofit-seo
description: Monthly podcast + YouTube SEO refresh (two /last30days runs distilled into ~/.sofit/seo-playbook.md) and the full-episode upload to YouTube and Spotify for Creators with that playbook's titles, descriptions, chapters, tags and hashtags. Use when the user says "sofit-seo", "refresh the SEO playbook", "upload the episode to YouTube/Spotify", or a monthly SEO run fires. Part of the sofit tool (see /sofit for the full pipeline).
---

# sofit-seo

Two jobs: `refresh` (once a month) and `upload <episode>` (every episode).
**Narrate every step.** Before each numbered step, tell the user in one line
what you are about to do and why; after it, one line on what came out. The
user follows this from their phone - silence reads as stuck.

## Home
```
HC=/Users/navotv/src/hebrew-chapters          # repo dir
SOFIT="$HC/.venv/bin/sofit"
PUB="$HC/scripts/publish"; PY=~/.ws-scraper/venv/bin/python   # playwright venv
PLAYBOOK=~/.sofit/seo-playbook.md             # machine-local, never in the repo
PLAN=~/.sofit/plans/publish-plan-<EP>-episode.json
```

## refresh (monthly; also run it first if `upload` finds the playbook >31 days old)
1. **"Researching podcast SEO from the last 30 days."** Run the skill
   `/last30days:last30days podcast SEO`. Keep its raw file path.
2. **"Researching YouTube SEO from the last 30 days."** Run
   `/last30days:last30days youtube seo`.
3. **"Distilling both into this month's playbook."** Overwrite `$PLAYBOOK`
   with: the date, both raw-file paths, and a short checklist of concrete,
   checkable rules, for YouTube (title pattern and length, where the keyword
   goes, description first 150 chars, chapters, how many tags and which kinds,
   3-5 hashtags, thumbnail text) and for Spotify (title, first description
   lines, hashtags, episode number). Drop anything the research only hints
   at. At the top, list **what changed vs last month**.
4. **"Done - here's what changed."** Show the user the diff summary.

## upload <EP> (each full episode)
Needs `{clips_dir}/<EP>_episode.mp4` (the full video) and the cached transcript.
0. **Check the playbook age** (`$PLAYBOOK` mtime). If it's missing or older than
   31 days, say so and run `refresh` first.
1. **"Generating chapters and show notes from the transcript."**
   `"$SOFIT" "<EP video>" --format youtube --titler claude-cli` and
   `--format spotify --shownotes`. Chapters come from Whisper timestamps,
   never invent them.
2. **"Writing the SEO metadata to this month's playbook."** Fill `$PLAN`:
   ```json
   {"episode": "WS214", "time_local": "10:00", "posts": [{"clip": "episode",
     "date": "YYYY-MM-DD", "speaker": "both",
     "youtube": "<title ≤100>", "youtube_description": "<hook, notes, 0:00 chapters, 3-5 #hashtags>",
     "youtube_tags": "<comma list ≤500 chars: show name in Hebrew+Latin+misspellings, hosts, every named person/company, topic phrases>",
     "thumbnail": "~/Downloads/WS214_episode-cover.jpg",
     "spotify_title": "<title>", "spotify_description": "<hook, notes, chapters, 3-5 #hashtags>"}]}
   ```
   House rules still apply: «וויקלי סינק» in Hebrew in titles and prose (the
   Latin spelling is allowed only as a search tag); hashtags are in Hebrew
   where people search in Hebrew. Spotify has no tags field, so its hashtags
   and keywords go in the description.
3. **"Checking the SEO gate."** `cd $PUB && $PY -c "import seo,json; p=json.load(open('$PLAN'))['posts'][0]; print(seo.check('youtube', p['youtube'], p['youtube_description'], p['youtube_tags']), seo.check('spotify', p['spotify_title'], p['spotify_description']))"`.
   Fix every problem. **No tags or no hashtags means no upload.**
4. **Approval gate.** Show Navot the title, description, tags and the
   thumbnail for both platforms. Wait for a yes.
5. **"Dry run on YouTube / Spotify."**
   `$PY $PUB/upload_youtube.py --clip episode --plan $PLAN --dry` and
   `$PY $PUB/upload_spotify.py --plan $PLAN --dry`. Open both screenshots and
   check them yourself: tags counter >0/500, the thumbnail is the cover, the
   schedule date and time are right, the hashtags made it into the description.
   Report what you saw.
6. **"Scheduling on YouTube / Spotify."** After the yes, run the same commands
   with `--submit`. Both refuse (exit 7) on SEO problems or 0 committed tags,
   and refuse (exit 6) when the Spotify readback fails. Never work around a
   refusal; fix the plan. Autolog records each post with its URL.
7. **"Done."** Give both links and where to see them scheduled (YT Studio
   Content, Spotify for Creators Episodes).

## Gotchas
- Spotify login: `$PY $PUB/upload_spotify.py --login` opens a headed window.
  **Navot logs in himself**: never type credentials.
- The Spotify wizard selectors were not probed before the first login. The
  first `--dry` is the probe: if it reports `dry_readback_failed`, fix the
  selector in upload_spotify.py from the screenshot and the DOM, then rerun.
- YouTube drops ALL hashtags if a description has more than 15. The gate
  enforces this.
