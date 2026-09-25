"""Bounded source work shared across beats/clips; the existing renderer consumes the results."""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, replace

from . import footage_progress as progress
from .footage import (
    Cue,
    candidate_from_dict,
    FootageCache,
    FootageError,
    _hash,
    canonical_url,
    rank_candidates,
    write_json,
)
from .footage_cache import lease, prune, touch, status, guard
from .footage_index import evidence, semantic_key, unused_ranges
from .footage_selection import ClaudeFrameJudge, SelectedSegment, normalize_segment
from .footage_youtube import YouTubeProvider, available, youtube_url


def source_url(url):
    return youtube_url(url) or canonical_url(url)


def cached_candidate(saved):
    candidate = candidate_from_dict(saved["candidate"])
    if candidate.provider == "direct" and "video" in saved:
        info = saved["video"]
        candidate = replace(
            candidate,
            duration=info["duration"],
            width=info["width"],
            height=info["height"],
            size=saved["size"],
        )
    return candidate


class JudgeGate:
    """A service/account failure stops further model launches for this run."""

    def __init__(self, judge, workers=1):
        self.judge, self.cache_key = judge, judge.cache_key
        self.semaphore = threading.Semaphore(workers)
        self.blocked = None

    def score_many(self, frames, intents):
        def score():
            if hasattr(self.judge, "score_many"):
                return self.judge.score_many(frames, intents)
            return [self.judge.score(frames, intent) for intent in intents]

        return self.call(score)

    def score_source(self, frames, intents, candidate):
        if hasattr(self.judge, "score_source"):
            return self.call(
                lambda: self.judge.score_source(frames, intents, candidate)
            )
        return self.score_many(frames, intents)

    def call(self, fn):
        with self.semaphore:
            if self.blocked:
                raise FootageError(
                    "visual service unavailable for this run: " + self.blocked
                )
            try:
                return fn()
            except Exception as e:
                message = str(e).lower()
                if any(
                    word in message
                    for word in (
                        "session limit",
                        "usage limit",
                        "rate limit",
                        "quota",
                        "429",
                        "oauth",
                        "not logged in",
                        "authentication",
                        "credit balance",
                    )
                ):
                    self.blocked = "quota/authentication/rate limit"
                    progress.event(
                        "visual_service_unavailable",
                        reason=self.blocked,
                        visual_service="blocked",
                    )
                    print(
                        "warning: visual service quota/authentication/rate limit; "
                        "no more model calls this run; retaining original visuals",
                        file=sys.stderr,
                    )
                raise


class FootageSession:
    def __init__(
        self,
        cache=None,
        judge=None,
        titler="api",
        workers=2,
        judge_workers=1,
        providers=None,
        safe_only=False,
    ):
        if not 1 <= workers <= 8 or not 1 <= judge_workers <= 4:
            raise ValueError("footage workers must be 1..8; judge workers 1..4")
        self.cache = cache or FootageCache()
        self.judge = JudgeGate(judge or ClaudeFrameJudge(titler), judge_workers)
        self.workers, self.providers, self.safe_only = workers, providers, safe_only
        self.discovery = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="footage-discovery"
        )
        self.sources = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="footage-source"
        )
        self.lock = threading.RLock()
        self.resolved, self.acquired, self.analyzed = {}, {}, {}
        self.jobs, self.intents, self.source_intents, self.used = {}, {}, {}, {}
        self.catalog = {}
        self.reserved_urls = {}
        self.clip_scope = None
        self.source_locks = {}
        self.completed_sources = set()
        self.disk_budget = int(
            float(os.environ.get("SOFIT_FOOTAGE_CACHE_GB", "4")) * 1024**3
        )
        if self.disk_budget <= 0:
            raise ValueError("cache budget must be positive")
        self.disk_reserved = 0
        self._lease = None

    def __enter__(self):
        # Eviction only before/after active work, never while a renderer holds assets.
        prune(self.cache.root, max_bytes=self.disk_budget)
        self.disk_reserved = status(self.cache.root)["bytes"]
        self._lease = lease(self.cache.root)
        self._lease.__enter__()
        for p in self.cache.root.glob("*/source.json"):
            try:
                saved = json.loads(p.read_text())
                c = cached_candidate(saved)
                if (p.parent / "source.media").is_file():
                    old = self.catalog.get(c.source_url)
                    if old is None or abs(c.height - 720) < abs(old.height - 720):
                        self.catalog[c.source_url] = c
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return self

    def __exit__(self, *exc):
        self.discovery.shutdown(wait=True, cancel_futures=True)
        self.sources.shutdown(wait=True, cancel_futures=True)
        if self._lease:
            self._lease.__exit__(*exc)
        prune(self.cache.root, max_bytes=self.disk_budget)

    def begin_clip(self, scope):
        """Avoid repeated shots within a clip; independent clips can reuse them."""
        self.clip_scope = scope

    def reserve_existing(self, cutaways, scope=None):
        for cutaway in cutaways:
            source = cutaway.get("source") or {}
            selection = source.get("selection") or {}
            try:
                start, end = float(selection["start"]), float(selection["end"])
                if 0 <= start < end:
                    self.reserved_urls.setdefault(
                        (scope, source["source_url"]), []
                    ).append((start, end))
            except (KeyError, TypeError, ValueError):
                continue

    def _submit(self, pool, fn, *args):
        return pool.submit(contextvars.copy_context().run, fn, *args)

    def _once(self, store, key, fn):
        with self.lock:
            future = store.get(key)
            owner = future is None
            if owner:
                future = store[key] = Future()
        if owner:
            try:
                future.set_result(fn())
            except BaseException as e:
                future.set_exception(e)
        else:
            progress.count("cache_hit")
        return future.result()

    def submit(self, intents):
        # Register the whole set before starting workers, so shared URL analysis
        # can score all distinct actions in the same image batch.
        with self.lock:
            for intent in intents:
                key = self._key(intent)
                self.intents[key] = intent
                for url in intent.source_urls:
                    self.source_intents.setdefault(source_url(url), {})[
                        semantic_key(intent)
                    ] = intent
            for key, intent in self.intents.items():
                if key not in self.jobs:
                    self.jobs[key] = self._submit(
                        self.discovery, self._discover, intent
                    )
        progress.event("discovery", sources_total=len(self.source_intents))

    @staticmethod
    def _key(intent):
        return json.dumps(asdict(intent), sort_keys=True)

    def _resolve(self, url):
        def resolve():
            if url in self.catalog:
                progress.count("metadata_cache_hit")
                progress.count("cache_hit")
                return self.catalog[url]
            directory = (
                self.cache.root
                / hashlib.sha256(("metadata:" + url).encode()).hexdigest()
            )
            manifest = directory / "metadata.json"
            try:
                saved = json.loads(manifest.read_text())
                if 0 <= time.time() - saved["saved_at"] < 3600 and saved["url"] == url:
                    candidate = candidate_from_dict(saved["candidate"])
                    progress.count("metadata_cache_hit")
                    progress.count("cache_hit")
                    touch(directory)
                    return candidate
            except (OSError, ValueError, KeyError, TypeError):
                pass
            with progress.stage("resolve"):
                if youtube_url(url):
                    candidate = YouTubeProvider().resolve(url) if available() else None
                elif self.safe_only:
                    return None
                else:
                    from .footage_direct import DirectProvider

                    reserve = self.cache.limits.max_bytes
                    with self.lock:
                        if (
                            self.judge.blocked
                            or self.disk_reserved + reserve > self.disk_budget
                        ):
                            raise FootageError(
                                "visual service/cache budget unavailable"
                            )
                        self.disk_reserved += reserve
                    try:
                        candidate = DirectProvider().resolve(
                            url,
                            self.cache,
                            cancelled=lambda: self.judge.blocked is not None,
                        )
                    finally:
                        with self.lock:
                            self.disk_reserved -= reserve
                    with self.lock:
                        self.disk_reserved += candidate.size or 0
            if candidate:
                write_json(
                    manifest,
                    {
                        "url": url,
                        "saved_at": time.time(),
                        "candidate": asdict(candidate),
                    },
                )
            return candidate

        return self._once(self.resolved, url, resolve)

    def _discover(self, intent):
        candidates = []
        if intent.source_urls:
            intent = replace(
                intent, source_urls=tuple(source_url(u) for u in intent.source_urls)
            )
            for url in dict.fromkeys(source_url(u) for u in intent.source_urls):
                try:
                    if intent.published_after and not youtube_url(url):
                        continue
                    c = self._resolve(url)
                    if c:
                        candidates.append(c)
                except Exception as e:
                    progress.count("resolve_failures")
                    progress.event("source_unavailable", reason=type(e).__name__)
        else:
            from .footage_commons import CommonsProvider

            providers = (
                self.providers
                if self.providers is not None
                else [CommonsProvider()]
                + (
                    [
                        YouTubeProvider(
                            intent.prefer_recent,
                            intent.published_after,
                            intent.preferred_channels,
                        )
                    ]
                    if available()
                    else []
                )
            )

            def search(query):
                key = (
                    "search",
                    query,
                    intent.prefer_recent,
                    intent.published_after,
                    intent.preferred_channels,
                )
                return self._once(
                    self.resolved,
                    key,
                    lambda: self._search_cached(providers, query, key),
                )

            candidates = search(intent.query)
            from .footage import channel_preference

            ranked = rank_candidates(
                candidates,
                replace(intent, duration=2),
                self.safe_only,
                self.cache.limits,
            )
            if intent.required_terms and (
                not ranked
                or intent.preferred_channels
                and not any(
                    channel_preference(
                        c.creator, c.channel_url, intent.preferred_channels
                    )
                    for c in ranked
                )
            ):
                candidates = (
                    search(
                        " ".join(dict.fromkeys(" ".join(intent.required_terms).split()))
                    )
                    + candidates
                )
        ranked = rank_candidates(
            candidates, replace(intent, duration=2), self.safe_only, self.cache.limits
        )
        if not ranked:
            progress.count("no_candidates")
            progress.event("no_matching_source")
        # Start the preferred source now. A second candidate is evaluated only
        # if the first cannot supply enough relevant ranges.
        return [
            (c, self._analysis_future(c, intent) if n == 0 else None)
            for n, c in enumerate(ranked[:2])
        ]

    def _search_cached(self, providers, query, key):
        from .footage_selection import _search

        # Only built-in providers have a stable cross-process configuration.
        # Library-injected providers retain their existing per-run cache contract.
        if self.providers is not None:
            return _search(providers, query)
        identity = json.dumps(["search-v1", key, [p.name for p in providers]])
        directory = self.cache.root / hashlib.sha256(identity.encode()).hexdigest()
        manifest = directory / "metadata.json"
        try:
            saved = json.loads(manifest.read_text())
            if (
                saved["key"] == identity
                and 0 <= time.time() - saved["saved_at"] < 3600
                and isinstance(saved["candidates"], list)
                and 0 < len(saved["candidates"]) <= 16
            ):
                candidates = [candidate_from_dict(c) for c in saved["candidates"]]
                touch(directory)
                progress.count("search_cache_hit")
                progress.count("cache_hit")
                return candidates
        except (OSError, ValueError, KeyError, TypeError):
            pass
        candidates = _search(providers, query)[:16]
        # Empty/failed searches should recover immediately when a provider recovers.
        if candidates:
            write_json(
                manifest,
                {
                    "key": identity,
                    "saved_at": time.time(),
                    "candidates": [asdict(c) for c in candidates],
                },
            )
        return candidates

    def _analysis_future(self, candidate, intent):
        with self.lock:
            intents = self.source_intents.setdefault(candidate.source_url, {})
            intents[semantic_key(intent)] = intent
            key = (candidate.cache_url or candidate.media_url, tuple(sorted(intents)))
            if key not in self.analyzed:
                progress.event("source_queued", sources_total=len(self.source_intents))
                self.analyzed[key] = self._submit(
                    self.sources, self._analyze, candidate, list(intents.values())
                )
            return self.analyzed[key]

    def _analyze(self, candidate, intents):
        identity = candidate.cache_url or candidate.media_url
        with progress.stage("source"):

            def acquire():
                import hashlib

                key = hashlib.sha256(canonical_url(identity).encode()).hexdigest()
                hit = (self.cache.root / key / "source.media").exists()
                if self.judge.blocked and not hit:
                    raise FootageError(
                        "visual service unavailable; skipping cold acquisition"
                    )
                reserve = 0 if hit else (candidate.size or self.cache.limits.max_bytes)
                with self.lock:
                    if self.disk_reserved + reserve > self.disk_budget:
                        progress.event("cache_budget_exhausted")
                        raise FootageError(
                            "cache budget exhausted; prune cache or raise SOFIT_FOOTAGE_CACHE_GB"
                        )
                    self.disk_reserved += reserve
                try:
                    try:
                        result = self.cache.retrieve(
                            candidate, cancelled=lambda: self.judge.blocked is not None
                        )
                    except Exception:
                        # A corrupt/deleted cached file may leave an expired signed
                        # URL in its manifest. Refresh once, never retry every beat.
                        if (
                            self.judge.blocked
                            or candidate.provider != "youtube"
                            or candidate.source_url not in self.catalog
                        ):
                            raise
                        refreshed = YouTubeProvider().resolve(candidate.source_url)
                        if not refreshed:
                            raise FootageError("cached YouTube source is unavailable")
                        result = self.cache.retrieve(
                            refreshed, cancelled=lambda: self.judge.blocked is not None
                        )
                    with self.lock:
                        if not hit:
                            self.disk_reserved += result[1]["size"] - reserve
                    return result
                except BaseException:
                    with self.lock:
                        self.disk_reserved -= reserve
                    raise

            media, metadata = self._once(self.acquired, identity, acquire)
            # Old manifests still carry the probed duration used for evidence validation.
            with self.lock:
                source_lock = self.source_locks.setdefault(identity, threading.Lock())
            with source_lock, guard(media.parent, ".analysis-lock"):
                touch(media.parent)
                # Discovery may add actions while acquisition or another source
                # pass is in flight. Refresh after acquiring the source lock so
                # queued jobs batch those actions instead of replaying stale
                # per-beat snapshots. Evidence still scores each action separately.
                with self.lock:
                    registered = self.source_intents.get(candidate.source_url, {})
                    intents = list(
                        {**{semantic_key(i): i for i in intents}, **registered}.values()
                    )
                result = evidence(
                    media,
                    metadata,
                    intents,
                    self.judge,
                    cues=lambda: self._cues(candidate, media, metadata),
                )
            with self.lock:
                if identity not in self.completed_sources:
                    self.completed_sources.add(identity)
                    progress.count("sources_done")
            return media, metadata, result

    def _cues(self, candidate, media, metadata):
        manifest = media.parent / "cues.json"
        try:
            saved = json.loads(manifest.read_text())
            if (
                saved["sha256"] == metadata["sha256"]
                and 0 <= time.time() - saved["saved_at"] < 86400
            ):
                return [Cue(**c) for c in saved["cues"]]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        cues = list(candidate.cues)
        if not cues and (candidate.subtitle_urls or self.providers):
            provider = next(
                (p for p in self.providers or [] if p.name == candidate.provider), None
            )
            if provider is None:
                if candidate.provider == "youtube":
                    provider = YouTubeProvider()
                elif candidate.provider == "commons":
                    from .footage_commons import CommonsProvider

                    provider = CommonsProvider()
            if provider:
                try:
                    with progress.stage("subtitles"):
                        cues = provider.subtitles(candidate)
                except Exception:
                    cues = []
        write_json(
            manifest,
            {
                "sha256": metadata["sha256"],
                "saved_at": time.time(),
                "cues": [asdict(c) for c in cues[:10000]],
            },
        )
        return cues[:10000]

    def find(self, intent):
        if self._key(intent) not in self.jobs:
            self.submit([intent])
        parts, remaining = [], intent.duration
        try:
            candidates = self.jobs[self._key(intent)].result()
        except Exception as e:
            progress.event("discovery_failed", reason=type(e).__name__)
            return None
        for candidate, future in candidates:
            try:
                future = future or self._analysis_future(candidate, intent)
                media, metadata, indexed = future.result()
                candidate = cached_candidate(metadata)
                editorial = replace(
                    intent,
                    duration=2,
                    source_urls=tuple(source_url(u) for u in intent.source_urls),
                )
                if not rank_candidates(
                    [candidate], editorial, self.safe_only, self.cache.limits
                ):
                    continue
                identity = metadata["sha256"]
                with self.lock:
                    used = self.used.setdefault(
                        (self.clip_scope, identity),
                        list(
                            self.reserved_urls.get(
                                (self.clip_scope, candidate.source_url), []
                            )
                        ),
                    )
                    choices = unused_ranges(indexed[semantic_key(intent)], used)
                    reservations = []
                    for start, end, confidence, reason in choices:
                        length = min(remaining, end - start, 30)
                        if 0 < remaining - length < 2 and remaining >= 4:
                            length = remaining - 2
                        if length < 2 - 1e-6:
                            continue
                        used.append((start, start + length))
                        reservations.append(
                            SelectedSegment(
                                candidate.source_url,
                                start,
                                start + length,
                                confidence,
                                reason,
                            )
                        )
                        remaining -= length
                        if remaining < 2 - 1e-6:
                            break
                for segment in reservations:
                    key = hashlib.sha256(
                        json.dumps(
                            [identity, segment.start, segment.end, "normalize-v1"]
                        ).encode()
                    ).hexdigest()
                    output = media.parent / (key + ".mp4")
                    manifest = output.with_suffix(".json")
                    try:
                        cached = json.loads(manifest.read_text())
                        valid = cached["asset_sha256"] == _hash(output)
                    except (OSError, ValueError, KeyError):
                        valid = False
                    try:
                        if not valid:
                            if status(self.cache.root)["bytes"] >= self.disk_budget:
                                raise FootageError("cache budget exhausted")
                            normalize_segment(media, segment, output)
                        else:
                            progress.count("cache_hit")
                        result = {
                            "video": str(output.resolve()),
                            "fit": "contain",
                            "asset_sha256": _hash(output),
                            "source": {
                                **asdict(candidate),
                                "retrieved_at": metadata["retrieved_at"],
                                "selection": asdict(segment),
                                "intent": asdict(intent),
                                "judge": self.judge.cache_key,
                                "modifications": "Temporal excerpt; audio removed; aspect preserved.",
                            },
                        }
                        result = json.loads(json.dumps(result))
                        write_json(manifest, result)
                        parts.append(
                            {"duration": segment.end - segment.start, **result}
                        )
                    except Exception:
                        with self.lock:
                            used.remove((segment.start, segment.end))
                        remaining += segment.end - segment.start
                        progress.count("normalization_failures")
                if remaining < 2 - 1e-6:
                    break
            except Exception as e:
                progress.count("source_failures")
                progress.event("source_failed", reason=type(e).__name__)
        if not parts:
            progress.count("no_confident_range")
            return None
        return {"parts": parts}
