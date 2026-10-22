# sofit scripts

- `pool.py` — candidate-pool generator + spec builder (suggest → pick → build)
- `quotecards.py` — branded IG carousel quote-cards from episode pull-quotes
- `publish/` — semi-automated publish + measure pipeline:
  uploaders for TikTok / Instagram / LinkedIn / YouTube Shorts (Playwright
  over a logged-in Chrome profile at ~/.ws-scraper/profile), a daily metrics
  scraper (retention curves, traffic sources, reach splits → the
  performance log at ~/.sofit/performance.jsonl), and a WhatsApp digest.
  `--dry` fills the form and screenshots without posting; only `--submit`
  posts. TikTok picks Asia/Jerusalem and refuses to submit unless the
  upload finished and the date, time, and zone read back. Instagram skips
  disabled days from the adjacent month; `--trial` exits unless the Trial
  Reel toggle reads back on. LinkedIn types the time, picks the matching
  dropdown option, and checks the composer "Posting … at" line.

Account/brand specifics are machine-local, never in this repo:
`~/.sofit/publish.json` (profile, collaborators, dirs), `~/.sofit/plans/`
(publish plans), `~/.sofit/assets/` (brand kit), `~/.sofit/brand.json`.
