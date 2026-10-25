# sofit scripts

- `pool.py` — candidate-pool generator + spec builder (suggest → pick → build).
  Each candidate is labeled with a `hook_type` and a one-line reason; the label
  is copied into clips.json and, from there, into the performance log.
- `quotecards.py` — branded IG carousel quote-cards from episode pull-quotes
- `publish/` — semi-automated publish + measure pipeline:
  uploaders for TikTok / Instagram / LinkedIn / YouTube Shorts (Playwright
  over a logged-in Chrome profile at ~/.ws-scraper/profile), a daily metrics
  scraper (retention curves, traffic sources, reach splits → the
  performance log at ~/.sofit/performance.jsonl), and a WhatsApp digest.
  `--dry` fills the form and screenshots without posting; only `--submit`
  posts. TikTok picks Asia/Jerusalem and refuses to submit unless the
  upload finished, the date, time, and zone read back, every planned
  hashtag committed, and the caption body is in the editor. The footer
  Schedule button is matched by its visible text when the content check
  has not exposed the accessible name, and the scheduled list is checked
  for the expected date afterwards. Chrome's `--disable-quic` flag (on
  unless `SOFIT_TT_NOQUIC=0`) is a modest help for "Couldn't upload"
  failures, about 31% to 42% success per attempt, not a fix. Instagram skips
  disabled days from the adjacent month; `--trial` exits unless the Trial
  Reel toggle reads back on. LinkedIn types the time, picks the matching
  dropdown option, and checks the composer "Posting … at" line.

Account/brand specifics are machine-local, never in this repo:
`~/.sofit/publish.json` (profile, collaborators, dirs), `~/.sofit/plans/`
(publish plans), `~/.sofit/assets/` (brand kit), `~/.sofit/brand.json`.
