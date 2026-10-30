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
<!-- If the repo or the venv moves, update HC here. The commands below use
     these variables; don't hardcode a /Users/navotv path into a one-liner. -->

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
   `"$SOFIT" "<EP video>" --format youtube --max-chapters 10 --titler claude-cli`
   and `"$SOFIT" "<EP video>" --format spotify --max-chapters 10 --shownotes`.
   The CLI default is 12; this skill caps at 10 (house rule, and the upload
   gate refuses a YouTube description with more than 10 chapter lines).
   Chapters come from Whisper timestamps, never invent them.
2. **"Writing the SEO metadata to this month's playbook."** Fill `$PLAN`:
   ```json
   {"episode": "WS214", "time_local": "10:00", "posts": [{"clip": "episode",
     "date": "YYYY-MM-DD", "speaker": "both",
     "youtube": "<title, 100 chars max, no angle brackets>", "youtube_description": "<hook, notes, 3-10 chapter lines from 0:00, 3-5 hashtags, 5000 UTF-8 bytes max>",
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
   check them yourself: the tags counter is near the planned length (not just
   above 0), the thumbnail is the cover, each chapter is on its own line, and
   the schedule date and time are the Asia/Jerusalem ones in the plan. A status
   other than `dry_ok` is a refusal. Report what you saw.
6. **"Scheduling on YouTube / Spotify."** After the yes, run the same commands
   with `--submit`. A dry run whose status is not `dry_ok` is a refusal — do
   not submit. YouTube long-form refuses (exit 7) on SEO problems or when the
   tags counter is under ~80% of the planned tag text, and refuses (exit 6)
   when the schedule readback fails, the thumbnail did not land, or the upload
   and checks are not finished (it will not close the browser on a multi-GB
   file that is still uploading). Spotify refuses (exit 7) on SEO problems and
   (exit 6) when the readback fails, the review step would publish now, or
   that title is already on the Episodes list. The Spotify commit clicks only
   Next or Schedule, never Publish. Never work around a refusal; fix the plan.
   Autolog records each post with its URL, including the full `episode` clip.
7. **"Done."** Give both links and where to see them scheduled (YT Studio
   Content, Spotify for Creators Episodes).

## Gotchas
- Spotify login: `$PY $PUB/upload_spotify.py --login` opens a headed window.
  **Navot logs in himself**: never type credentials.
- The Spotify wizard selectors were not probed before the first login. The
  first `--dry` is the probe: if it reports `dry_readback_failed`, fix the
  selector in upload_spotify.py from the screenshot and the DOM, then rerun.
- 15 hashtags in a description is a house rule, not a YouTube limit. YouTube
  allows more than 60 (Help answer 6390658) and ignores only the extras past
  that. The gate still refuses above 15, and it says so as a house rule.
- YouTube chapters: at least 3 lines, the first at 0:00, and at most 10
  (the 10 is a house cap). Pass `--max-chapters 10`; the CLI default is 12.
- `<` and `>` in a YouTube title or description are refused.
- A YouTube description is capped at 5000 UTF-8 bytes, which is stricter
  than 5000 characters when the text is Hebrew.
- Spotify's 200-character title cap is unverified; the gate enforces it
  anyway and the refusal says so. Hashtags on Spotify are a house rule
  (there is no tags field).
- `time_local` is an Asia/Jerusalem wall clock. The Spotify uploader does
  not convert it to UTC.
