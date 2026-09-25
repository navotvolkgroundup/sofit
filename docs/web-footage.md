# Real web footage cutaways

The feature extends `storyboard.plan_cutaways` and the existing dictionary-based
clip spec, without changing `schema_version: 1` or introducing a second renderer.

```
clip words → one visual plan → provider search → metadata/rights ranking
  → bounded cached download → subtitles/coarse frames → fine visual windows
  → silent local video + provenance → existing cutaway/captions/audio renderer
```

## Contracts

- `footage.VisualIntent`: English intent/query, desired duration, spoken context,
  optional explicit source URLs, upload-date cutoff, recent-upload preference,
  required subject/version terms and preferred publisher names/handles.
- `footage.FootageProvider`: `name`, `search(query, limit)` and
  `subtitles(candidate)`. An implementation without timed text returns `[]`.
- `footage.Candidate`: provider-independent source/media URLs, dimensions,
  duration, optional size/cues, descriptive text and rights metadata.
- `footage_selection.FrameJudge`: a batch `score(frames, intent)` and a stable
  `cache_key`. The built-in `ModelFrameJudge` reuses Sofit's selected model transport
  and model override; `ClaudeFrameJudge` remains a compatibility alias. A library
  caller can inject another judge without changing rendering. An optional
  `score_source(frames, intents, candidate)` method receives source attribution too.
- `SelectedSegment`: source URL, start/end, confidence and visible-evidence reason.
- Resolved cutaway: existing `span`, `start`, `end`, plus a local `video`, optional
  `fit`, `asset_sha256`, and `source` metadata. An `image` is no longer required
  alongside a video. A still-only/generated cutaway retains its existing behavior.

`visual_plan` times are relative to each **kept span**, exactly like captions and
existing cutaways. The source excerpt's times are separate, in
`cutaways[].source.selection`; they never shift the podcast timeline. For example:

```json
{
  "visual_plan": [{
    "span": 0, "start": 4, "end": 8, "duration": 4,
    "source": "web", "intent": "a Unitree humanoid robot running",
    "query": "Unitree humanoid robot running",
    "context": "The speaker describes a running demonstration.",
    "prompt": "A humanoid robot running on a track, illustrated"
  }]
}
```

A saved plan can also be authored directly through the CLI's clips JSON interface.
Sparse planning omits beats best served by the recording and reserves the hook
and closing seconds. Audio-only CLI inputs default to 85% coverage; an explicit
`--footage-coverage 0..100` overrides the target. Dense planning can cover the
opening/closing too (titles/captions are composed above it), with a duration-based
beat budget up to 64. Manual plans also accept up to 64 beats and 2–30s windows.
Plans reject overlaps and out-of-span times. Editorial
cutaways already occupying a beat take precedence. `source: "generated"` beats
are rendered only when the generated fallback is explicitly enabled.

## Discovery and selection

The MVP provider uses Commons' public `generator=search`, `filetype:video` and
`videoinfo` API. It normalizes HTML metadata into text and prefers a 360–1080p
transcode near 720p, avoiding multi-gigabyte originals. Only Commons upload hosts
are accepted by this provider. Commons timed-text tracks are preferred in English.
Commons has no optional dependency or API key.

The YouTube provider uses the existing `youtube` extra (`yt-dlp[default]`) for
public search and individual-video metadata. It resolves at most four of eight
search hits, rejects live/private/age-restricted/DRM results and selects a direct
HTTPS video stream, preferring up to 720p with a 1080p fallback. It never downloads through yt-dlp: the same bounded
Sofit downloader validates Google video URLs and media. HLS/DASH-only sources are
skipped. Metadata extraction uses yt-dlp's own networking in a subprocess with a
90s timeout, bounded JSON output and socket/retry limits; configs, plugins,
browser cookies and remote component downloads are disabled. Deno or Node 22+
is needed for YouTube's player challenges; packaged EJS scripts come with the extra.

YouTube removed sort-by-upload-date. Recent intents instead use an upload window
(last month by default; week/month/year for an explicit cutoff) and then rank by
relevance plus a small upload-age bonus. `published_after` rejects unknown/older
dates across all providers. Upload dates are not event dates. Title/channel,
upload date and reported license persist as provenance without fabricated rights.
YouTube metadata often lacks enough information for the safe-only allowlist.

Explicit `source_urls` replace discovery: YouTube URLs resolve through that
provider, and direct HTTPS files are bounded-downloaded/probed by `DirectProvider`.
Opaque filenames do not need a keyword match, but all links still pass visual
selection. Direct files have unknown rights/date metadata; safe-only or an explicit
date cutoff skips them before download. Generic HTML pages/playlists are unsupported.
At most eight explicit links are accepted. Direct-file metadata probing downloads
each candidate under the same per-file bounds; only two ranked candidates reach
visual inspection. Prefer a short list when the source files are large.

Planning receives clip words, its hook and `visual_context`: up to 180s before
and 30s after the clip from the existing transcript, capped at 16,000 characters.
This preserves topic introductions lost in short edits. New clip specs include
it; the CLI recovers it from the transcript cache for old specs when possible.
User-supplied clip/document context takes precedence. No render triggers transcription.

Queries preserve company/product/version, event and demonstration, rather than
reducing to generic categories. `required_terms` gate publisher metadata before
download (all subject words, intact decimal versions). A title's omitted company
may be supplied by its channel name. `preferred_channels` boosts exact publisher
name/handle matches before and after full metadata extraction; a video's title
saying 'official' is not proof. If no eligible result or no preferred publisher
is found, a single retry searches
the required subject/version without action adjectives. It never relaxes identity.
Explicit bare-file links have no subject metadata, so rely on their editorial
selection and the same visual evidence gate. Verified `source_urls` remain the
strongest way to pin research-backed sources. Search/link discovery is cached
within a run, including repeated publisher queries across beats. Successful
built-in provider search results also persist for one hour, keyed by query,
provider set, date/recent filters and publisher preference. This stabilizes warm
runs without suppressing fresh results indefinitely; empty/failed searches are
not persisted. Library-injected providers retain per-run caching only.

Ranking uses English token overlap across title, description/tags and supplied
cues; dimensions and duration only break relevant ties. Rights filtering happens
before any download. At most eight results per provider and two candidates per
beat proceed to download and visual inspection.

The CLI uses a `FootageSession` shared by all selected clips. A source is acquired
once, decoded once into a persistent one-frame-per-second index (640px maximum,
600 frames maximum), then analyzed for its distinct requested visual actions.
Equivalent intents share evidence independent of beat duration or query wording;
a different action or required identity needs its own evidence. Direct URLs are
registered before workers start, allowing up to four actions in one visual call.
Search-discovered actions arriving later reuse existing frames/evidence and only
analyze missing actions. Queued analysis refreshes the known action set after
acquiring the source lock, so discoveries made while an earlier pass ran can be
batched together. Automatic continuation beats reuse the same precise intent/query
instead of creating a new semantic action for every edit point.

Each action group starts with at most 24 coarse frames. Available subtitles can
guide half the coarse samples; missing subtitles never trigger transcription.
Up to three separated promising areas per action receive contiguous one-second
sampling, with at most 96 additional frames per group. Calls contain at most 25
images and four actions: at most five image batches per group, each with the
shared JSON helper's single retry. The score threshold remains 0.75; a range
requires three consecutive observations, and low/unobserved gaps cannot be bridged.
Boundaries stay within the probed source duration. Sparse coarse sampling can
miss brief actions; these judgments are not calibrated probabilities.

Analysis returns multiple verified ranges. Beat selection can concatenate several
of them without changing the podcast timeline. Previously allocated source
intervals are not repeated within one clip just to inflate coverage. Independent
clips can reuse those intervals and their cached evidence/excerpts. Insufficient
evidence leaves a reported gap. Exact span ends tolerate floating-point error up
to one microsecond and clamp to the span, so editors need no 0.05-second workaround.
The existing `select_segment` library function remains available for single-shot
callers; the CLI batch path uses the shared index instead.

## Cache, media and failure boundaries

The cache is under the same XDG convention as transcription, in `sofit/footage`.
A canonical HTTPS URL keys the original; YouTube uses canonical video+format
identity so rotating signed URLs do not redownload cached bytes. Existing media
manifests are reused. Uncached URL metadata has a one-hour TTL; subtitle results
have a one-day TTL. Expired YouTube download URLs are refreshed once if a cached
source needs repair. Atomic publication and size/hash/probe checks reject partial
or corrupt media.

Versioned evidence is keyed by source content hash, visual action, required
identity and judge/model version. Both positive and negative judgments persist;
service errors do not become negative evidence. Normalized excerpts are keyed by
source hash and selected times. A warm run can reuse evidence without frame
extraction or model calls. Editing the action or changing the judge invalidates
that evidence. A cache hit does not refresh upstream rights metadata.

Discovery and source processing each use a bounded pool, default two workers
(`--footage-workers 1..8`). Model work defaults to one concurrent call
(`SOFIT_FOOTAGE_JUDGE_WORKERS=1..4`); distinct actions share image batches when
possible. Visual requests have a 180-second default transport timeout, configurable
with `SOFIT_VISUAL_TIMEOUT`. Automatic planning still runs per clip before source
jobs are registered. Each completed clip checkpoints its spec and renders through
the existing renderer while later sources continue processing. No second renderer
or change to original audio/caption timing is involved.

`sofit cache status` reports managed bytes/files and temporary bytes/files.
`sofit cache prune [--dry-run]` evicts least-recently-used source groups over 4 GiB
or unused for 30 days and removes temporary files older than 24 hours.
`--max-size-mb`, `--max-age-days` and `--temp-age-hours` override those prune limits.
`sofit cache clean [--dry-run]` removes all recognized footage artifacts. These
commands understand the earlier cache layout and do not touch transcripts,
user input files, final outputs, unknown files or symlink targets.

Sessions prune before/after work, hold process leases against eviction, and reserve
space before cold acquisition. `SOFIT_FOOTAGE_CACHE_GB` changes the session budget.
The budget is a managed-cache limit, not a hard filesystem quota: concurrent
normalization/frame intermediates may temporarily exceed it. Cross-process locks
serialize analysis; in-process futures share successful and failed acquisition.
Independent processes may still race to download a cold source. Eviction removes
entire source groups; saved specs pointing to evicted excerpts need web mode to
rebuild them. Without it, missing assets fall back to the original recording.

A quota/authentication/rate-limit failure prevents additional model launches for
that session; persisted successful evidence can still be used. See
[performance validation](footage-performance.md) for progress and profiling.

YouTube CDN media is fetched in bounded 1 MiB HTTP ranges. Every partial response
must match its requested offset/length and a stable total size; unexpected full
responses after the first chunk, truncated ranges and oversized totals are rejected.
This avoids the very slow whole-file transfer observed during live validation.
The existing 720p preference, public-host validation and overall transfer limits
remain unchanged. A blocked visual service cancels in-flight transfers at the next
received chunk, removes partial files, and still allows valid cache hits.

Defaults: 256 MiB, 600 source seconds, 30-second socket/probe timeout,
180-second download budget, 120-second normalization timeout, 7680px maximum
source dimensions. The library's `FootageCache(..., limits=Limits(...))` can set
stricter download limits. Only direct public HTTPS is fetched: credentials in
URLs, local destinations and private-address redirects are rejected; connections
pin validated IPs while retaining TLS hostname verification. Environment HTTP
proxies are not used. Media subprocesses accept only local file/pipe protocols
and video container formats, never remote playlists.

Normalization decodes the selected excerpt into silent H.264/yuv420p, square
pixels and 30fps without stretching or upscaling. Composition preserves its
aspect, using containment by default or `fit: "cover"`. Captions, logo and original
audio use the existing pipeline. Decode/composition errors retry with the original
recording; source errors still surface if that retry fails.

Search/vision/network/metadata failures are isolated per candidate and clip. No
confident asset means generated art if enabled and available, otherwise the
original. Existing specs work offline; enabling web cutaways reuses successful
plans/assets and can recover deleted assets. Safe-only also rechecks saved source
metadata, but does not refresh upstream license pages for an offline rerender.

`footage_coverage` stores each clip's target. `footage_coverage_report` measures the
union of usable video windows across kept spans, excluding stills and missing
assets, and lists gaps. Planning retries an under-target plan once and retains a
useful partial plan if the target still cannot be met. No confidence thresholds
are relaxed. Rendering recalculates coverage from successfully composed windows
and saves `<clip-id>.coverage.json`, so composition fallback cannot claim coverage
it didn't deliver. Old automatic assets with orphaned `plan_id`s are removed when
a plan is edited; independent editorial cutaways are preserved.

## Rights and provenance

The default retains provider-supplied metadata without a license gate. Safe-only
is a deliberately narrow allowlist: public domain or CC0 explicitly reporting no
attribution requirement; or CC BY with a recognized Creative Commons URL, named
creator and attribution. It excludes unknown/custom/NC/ND/ShareAlike licenses and
reported restrictions. Missing metadata remains unknown, never fabricated.

The spec, cache and rendered `<clip-id>.sources.json` retain source URL, provider,
title, creator, license/URL, attribution text and requirement, restrictions,
retrieval time, original media URL, query/context, selected timestamps and the
modification note. The sidecar records only assets used in successful composition;
it is not an automatic attribution publication or rights-clearance mechanism.

Provider reference: [Commons MediaWiki API](https://commons.wikimedia.org/wiki/Commons:API/MediaWiki).
YouTube references: [yt-dlp](https://github.com/yt-dlp/yt-dlp),
[JavaScript setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS),
[removal of date sorting](https://github.com/yt-dlp/yt-dlp/pull/15959).

## Model providers

Claude remains the default: `api` uses Anthropic and `claude-cli` uses the existing
Claude Code account. Both accept native image input. Use `--titler-model` to pin
the model for reproducible evidence caching. Other built-in backends are separate
contributions, not part of this footage feature.

The shared JSON parser/validator and one retry are provider independent; the
historical `generate.call_claude_json` name remains compatible. A library caller
can register `register_backend(name, transport, cache_key="provider-v1")`; the
callable receives system/user text, model and optional local image paths and
returns text. Registration is explicit Python API configuration, never code loaded
from a clips spec. Transport identity/model separates evidence caches.

Source-level judgment receives publisher/title/source context alongside images.
Metadata does not prove the visible action or identity by itself: branding/design
in the supplied frames must corroborate attribution. A factory name need not be
readable in every shot, and identifiable component manufacture can satisfy a
production intent. Unrelated machinery, CGI and unsupported attribution
remain rejected under the same confidence gate. Evidence cache versioning prevents
old context-free judgments from being reused for this contract.

For an existing video cutaway, `"fit": "blur"` keeps the complete source frame over a blurred moving portrait background. This is rendered in the existing composition pass, without an extra intermediate encode. The default remains `contain`; use `cover` only when cropping will preserve the relevant action.

## Before-render source credits

Every usable cutaway carrying source metadata is reported immediately before its
span is composed, including cached assets and ordinary saved-spec rerenders.
The report includes clip ID, zero-based span, span-local start/end in source-time
seconds, provider, source URL, license and license URL, creator, attribution text
and whether attribution is required. Missing fields are explicitly unknown.
No progress flag or interactive confirmation is needed. `--progress json` and
`--progress-file` carry complete `source_credit` events under `credit`, without
the diagnostic log's length limit. Rights are not inferred or cleared.

The pre-render report describes planned composition. If composition fails and
falls back to the recording, the final `.sources.json` continues to include only
successfully composed sources. Safe-only filtering remains opt-in.
