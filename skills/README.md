# Claude Code skills for sofit

Drop-in [Claude Code](https://claude.com/claude-code) skills that drive `sofit` in natural
language from any directory. Install by copying (or symlinking) the folders you want into
`~/.claude/skills/`:

```bash
cp -R skills/sofit* ~/.claude/skills/
```

Then invoke them by name, e.g. `/sofit-clips`.

| Skill | Does |
|---|---|
| `sofit` | full pipeline + router to the sub-skills below |
| `sofit-transcribe` | local faster-whisper transcription (cached, one-time) |
| `sofit-kit` | chapters + Hebrew show notes + pull-quotes for descriptions |
| `sofit-clips` | suggest → pick → render captioned 9:16 social clips |
| `sofit-captions` | fix caption typos, timing-preserving, then re-render |
| `sofit-trim` | cut a moment out of a finished clip |

**Personalize before use.** Each `SKILL.md` has a `Home` block with absolute paths for one
setup (`HC=` the repo/venv, `LOGO=` the show wordmark). Edit those to match your machine.
`sofit-clips` calls the bundled `sofit/clips.py` (candidate-pool → pick → clips.json helper);
keep it alongside the `sofit` skill.

The web-footage instructions in `sofit-clips` also work for agents such as Codex:
they call the same local Python CLI and clips JSON interface, with no agent-specific
runtime dependency. Requests for real B-roll use `--web-cutaways` after selecting
clips. See the repository README for credentials, caching, provenance and the
optional `--web-cutaways-safe-only` metadata policy.
YouTube search uses the optional `youtube` extra; provided YouTube/direct-video
links use `--footage-url`, and explicit upload-date bounds use `--footage-after`.
Audio-only web mode targets 85% video coverage; `--footage-coverage` overrides it.
Use episode context and exact subject/version queries, prefer primary publishers,
and check the rendered coverage report before calling a clip complete.
