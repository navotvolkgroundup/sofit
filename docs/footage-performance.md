# Footage performance validation

Start with a copied spec containing 2–3 representative clips: one with no previous
coverage, one partially covered, and one sharing a source. Keep original specs
and outputs intact. A useful baseline needs the same media, plans, quality gate,
model and source availability; total duration alone cannot identify a bottleneck.

```bash
uv run sofit --render-from subset.clips.json --render-clips subset-out \
  --web-cutaways --footage-coverage 90 --titler claude-cli \
  --footage-workers 2 --progress --progress-file subset-out/progress.jsonl
```

The progress option writes to stderr, leaving stdout available to callers.
`--progress json` also wraps ordinary diagnostic lines as JSON log events.
`--progress-file` appends JSONL; use a new filename to separate runs. Heartbeats
appear every five seconds during long calls. Events include clip/source/beat
counts, downloads, rendered clips, cache hits, Claude calls and elapsed time.
`active_stages` reports concurrent in-flight operations, rather than repeating a
completed stage while another worker is still downloading. Event-only reasons and
messages do not leak into later status events. No ETA is emitted: source/model latency is too variable for an uncalibrated estimate.
Known sources include rejected or unused fallback candidates; not every known
source needs downloading or analysis before a batch is complete.

The output directory's `footage-metrics.json` contains totals. The JSONL also keeps
per-clip completion latency and coverage, as well as rejection/failure events:

| Metric | Meaning |
| --- | --- |
| `seconds.search`, `resolve`, `metadata` | Provider search and source metadata work |
| `seconds.download`, `counts.bytes_downloaded` | Transfer/probe time and received bytes, including failed transfers |
| `counts.cache_hit`, `cache_miss` | Reuse across media, metadata, evidence, excerpts and in-run futures; not unique source counts |
| `seconds.frames`, `counts.frames_extracted` | Decoder/index work and produced frames |
| `seconds.judge` | Visual batches, including waiting for the model semaphore |
| `counts.model_calls`, `seconds.model` | All selected model transport attempts and wall time |
| `counts.<backend>_calls`, `seconds.<backend>` | Registered transport attempts and time; a CLI suffix is omitted |
| `counts.claude_calls`, `seconds.claude` | Actual API/CLI transport attempts and time, including retries/planning |
| `counts.source_analyses`, `repeated_source_analyses` | Uncached evidence passes by source content hash |
| `seconds.beat_selection`, `normalize`, `ffmpeg`, `render` | Selection, excerpt normalization and final composition |
| `clip_seconds` on `rendered` events | Time since previous completed clip, including preparation before the first clip |
| `elapsed_seconds` | Batch wall time |

Concurrent and nested stage times overlap; **do not sum them** into total wall
time. Additional analysis of a genuinely different action is legitimate. A quota
failure with zero coverage measures failure handling, not successful visual quality.
Inspect `.coverage.json`, `.sources.json`, actual frames and continuous source audio
before expanding the subset. Never fill gaps with unrelated or repeated excerpts
just to reach the target.

## Reproducible offline comparison

This fixture benchmark is network-free and needs the render/dev extras plus
FFmpeg. It creates a synthetic video with separated relevant/irrelevant windows,
audio-only clips, a replayed download and a deterministic pixel-based judge.
It compares the legacy per-beat selector with the source session using real
normalization/composition. It does **not** estimate live Claude or YouTube latency.

```bash
uv sync --extra dev --extra render
SOFIT_BRAND=off uv run python scripts/benchmark_footage.py \
  --out /tmp/sofit-footage-benchmark
```

The output directory must be empty. Each engine starts with a cold cache, then
reruns with the cache warm and automatic cutaways removed from the spec to exercise
selection again. Results include wall time, downloads/bytes, source analyses,
frame passes, fixture judge batches, cache hits and coverage for all three clips.
`results.json`, stage reports, JSONL and rendered videos stay outside the repository.
A fully resolved saved spec is faster still: it bypasses selection entirely.

Cold media/index/evidence work scales with distinct sources and actions. Warm
identical actions need no additional model calls; source metadata availability,
new actions, source decoding and final encoding remain potential bottlenecks.
Automatic planning is still per clip. Real-source live cold/warm benchmarks must
be reported separately from this deterministic regression fixture.

## Historical combined-branch validation

The results below were recorded on the original combined branch in
[PR #10](https://github.com/navotvolkgroundup/sofit/pull/10), which also included
the separately proposed Codex backend. They are historical evidence for that
combination, **not a new live Claude validation or a claim that this footage-only
branch ships Codex**. The deterministic tests above run without that backend.

### Live application validation

A three-clip audio-only episode subset was also run through the actual editable
CLI with YouTube sources, native Codex image judgments (`gpt-6-astra`), two source
workers and two model workers. The warm run started from the unresolved input
plan, not the already-selected assets, so it exercised discovery and selection.

| Metric | Empty footage cache | Warm footage cache |
| --- | ---: | ---: |
| Wall time | 357.1 s | 71.9 s |
| Source downloads / received bytes | 4 / 50,528,219 | 0 / 0 |
| Source analyses / frame passes | 4 / 4 | 0 / 0 |
| Native image-model calls | 15 | 0 |
| Cache-hit events | 25 | 55 |
| Delivered footage coverage | 100%, 100%, 100% | 100%, 100%, 100% |

FFprobe confirmed 1080×1920 H.264/AAC outputs, with duration errors below 25 ms
against the kept spans. Decoded audio was byte-identical to the same clips rendered
without cutaways. Representative frames were inspected for subject/action,
aspect ratio and captions. An all-frame comparison with the original audiogram
also checked for unintended base-frame flashes. That inspection exposed a
fractional-boundary overlay EOF bug, now covered by a real FFmpeg regression test:
the last frame holds only until its explicit cutaway window ends.

Whole-file YouTube transfers had hit the 180 s bound; bounded 1 MiB HTTP ranges
reduced transfer time for these four sources to 10.3 s total. The same size/time
limits and integrity checks remain active. These measurements are observations
from one machine/account/network, not a throughput or coverage guarantee.
Separately, a live Claude quota failure produced original-visual fallback rather
than false coverage; the successful measurements above explicitly used Codex.

Only after this subset passed, validation expanded to six clips with shared
sources: 486.2 s, two additional downloads, three source/action analyses, nine
model calls, and coverage of 100%, 93.1%, 100%, 100%, 100%, 100%. All six retained
byte-identical decoded audio and expected timing. This larger run reused the
subset cache and included concurrent baseline rendering/testing; it is a
correctness expansion, not another cold-cache benchmark. The remaining 6.9% gap
was reported and retained the original visuals, without reducing confidence.

A separate live test started with an automatically generated, version-specific
toy-collection plan and actual YouTube search, rather than supplied URLs. It
exposed stale queued action sets and changing search fallbacks; these led to
late-action batching, shared continuation intents and a one-hour search cache.
The final result reached 100% coverage using two visually evaluated sources.
Replaying the unresolved plan with search/media/evidence cached took 31.1 s,
with zero search metadata calls, downloads, frame passes or model calls. Original
decoded audio and all-frame transition checks passed. Earlier attempts with
insufficient matching evidence reported 61.9% and 84.9%, rather than hiding gaps.
