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
| `sofit-seo` | monthly SEO playbook refresh + full-episode upload to YouTube and Spotify for Creators |

**Personalize before use.** Each `SKILL.md` has a `Home` block with absolute paths for one
setup (`HC=` the repo/venv, `LOGO=` the show wordmark). Edit those to match your machine.
`sofit-clips` calls the bundled `sofit/clips.py` (candidate-pool → pick → clips.json helper);
keep it alongside the `sofit` skill.
