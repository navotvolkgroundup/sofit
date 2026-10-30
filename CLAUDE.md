# Working in this repo

sofit is a public OSS repo. Business content NEVER goes here: account config
(~/.sofit/publish.json), publish plans (~/.sofit/plans/), brand kit
(~/.sofit/assets/ - licensed font), the performance log (~/.sofit/
performance.jsonl), and review notes are all machine-local under ~/.sofit (per show:
~/.sofit/shows/<slug>/, chosen by SOFIT_SHOW or ~/.sofit/default-show).

## Layout
- `src/sofit/` — library + CLI (render, generate, publog, corrections,
  quotecards, storyboard, mcp_server)
- `scripts/pool.py` — clip candidate pool + spec builder (suggest → pick → build)
- `scripts/publish/` — TikTok/IG/YouTube uploaders (Playwright over
  ~/.ws-scraper/profile), upload_carousel.py, daily metrics scraper
  (launchd `vc.groundup.ws-stats`), WhatsApp digest
- Operating manuals live in the `/sofit*` skills; follow them for episode work.

## Workflow
- Plan first for non-trivial tasks; verify with real rendered frames /
  fresh page loads before declaring done (stale DOM lies).
- Approval gates are standing policy: covers, carousels, clips and publishes
  are shown to Navot before they ship. --dry first, always.
- Every posted clip gets publish-log with --speaker. Never post without logging.
- After any correction: update tasks/lessons.md (gitignored, machine-local).
- Run tests (.venv/bin/python -m pytest) before committing; the suite must
  stay hermetic (SOFIT_BRAND=off fixture guards machine-local brand assets).
