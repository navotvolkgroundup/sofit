"""Local, thread-safe pipeline timings and progress; never logs prompts or signed URLs."""

from __future__ import annotations

import contextvars
import functools
import json
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager, redirect_stderr, nullcontext
from pathlib import Path

_current = contextvars.ContextVar("footage_progress", default=None)


class Progress:
    def __init__(self, mode=None, path=None):
        self.mode, self.path = mode, Path(path) if path else None
        self.stream = sys.stderr
        self.started = time.monotonic()
        self.counts, self.seconds = Counter(), Counter()
        self.lock = threading.RLock()
        self.last_output = 0.0
        self.state = {}
        self.sources = Counter()
        self.running = Counter()
        self.model_providers = set()

    def snapshot(self):
        with self.lock:
            return {
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "counts": dict(self.counts),
                "seconds": {k: round(v, 3) for k, v in self.seconds.items()},
                "active_stages": {k: v for k, v in self.running.items() if v},
                **self.state,
            }

    def event(self, stage, **state):
        with self.lock:
            for key in (
                "reason",
                "message",
                "source",
                "clip_seconds",
                "coverage",
                "credit",
            ):
                self.state.pop(key, None)
            self.state.update(state, stage=stage)
            data = self.snapshot()
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as out:
                        out.write(json.dumps(data, ensure_ascii=False) + "\n")
                except OSError as exc:
                    self.path = None  # optional telemetry must not abort composition
                    self.state["progress_file_error"] = type(exc).__name__
                    data["progress_file_error"] = type(exc).__name__
            now = time.monotonic()
            if self.mode == "json":
                print(json.dumps(data), file=self.stream, flush=True)
            elif self.mode and (
                now - self.last_output >= 1
                or stage in {"complete", "failed", "rendered"}
            ):
                c = self.counts
                providers = ", ".join(
                    f"{name} {c[name + '_calls']}"
                    for name in sorted(self.model_providers)
                )
                print(
                    f"sofit: {','.join(data['active_stages']) or stage} | clips {c['clips_done']}/{self.state.get('clips_total', '?')} "
                    f"| sources {c['sources_done']} ready/{self.state.get('sources_total', '?')} known "
                    f"| beats {c['beats_done']}/{self.state.get('beats_total', '?')} "
                    f"| downloaded {c['downloads_done']} | rendered {c['rendered']} | cache {c['cache_hit']} "
                    f"| models {c['model_calls']} ({providers}) | elapsed {data['elapsed_seconds']:.0f}s",
                    file=self.stream,
                    flush=True,
                )
                self.last_output = now

    @contextmanager
    def active(self):
        token = _current.set(self)
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(5):
                self.event(self.state.get("stage", "working"))

        thread = threading.Thread(
            target=heartbeat, name="footage-progress", daemon=True
        )
        if self.mode or self.path:
            thread.start()
        try:
            with (
                redirect_stderr(_JsonLog(self))
                if self.mode == "json"
                else nullcontext()
            ):
                yield self
        finally:
            stop.set()
            if thread.is_alive():
                thread.join(timeout=1)
            _current.reset(token)


def current():
    return _current.get()


def count(name, amount=1):
    p = current()
    if p:
        with p.lock:
            p.counts[name] += amount


def event(stage, **state):
    if current():
        current().event(stage, **state)


@contextmanager
def stage(name):
    start = time.monotonic()
    count(name + "_calls")
    p = current()
    if p:
        with p.lock:
            p.running[name] += 1
    event(name)
    try:
        yield
    finally:
        p = current()
        if p:
            with p.lock:
                p.seconds[name] += time.monotonic() - start
                p.running[name] -= 1
            p.event(name + "_done")


def measured(name):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with stage(name):
                return fn(*args, **kwargs)

        return wrapper

    return decorate


def analyzed(identity):
    p = current()
    if p:
        with p.lock:
            p.counts["source_analyses"] += 1
            if p.sources[identity]:
                p.counts["repeated_source_analyses"] += 1
            p.sources[identity] += 1


class _JsonLog:
    def __init__(self, progress):
        self.progress = progress
        self.local = threading.local()

    def write(self, text):
        data = getattr(self.local, "buffer", "") + text
        lines = data.split("\n")
        self.local.buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.progress.event("log", message=line[:500])
        return len(text)

    def flush(self):
        self.progress.stream.flush()


@contextmanager
def model_stage(backend):
    """Count each transport attempt, including failed calls and JSON retries."""
    name = (
        "claude" if backend in {"api", "claude-cli"} else backend.removesuffix("-cli")
    )
    p = current()
    if p:
        with p.lock:
            p.model_providers.add(name)
    with stage("model"), stage(name):
        yield


def report_source_credit(clip_id, span, cutaway):
    """Show reported rights before composition, including saved/cached assets."""
    source = cutaway.get("source")
    if not isinstance(source, dict):
        return
    credit = {
        "clip": clip_id,
        "span": span,
        "start": cutaway["start"],
        "end": cutaway["end"],
    }
    for key in (
        "provider",
        "source_url",
        "license",
        "license_url",
        "creator",
        "attribution",
    ):
        credit[key] = source.get(key) or "unknown"
    required = source.get("attribution_required")
    credit["attribution_required"] = (
        required if isinstance(required, bool) else "unknown"
    )
    p = current()
    if p:
        p.event("source_credit", credit=credit)
    if not p or p.mode != "json":
        # JSON escaping makes remote-controlled text safe for a terminal while
        # preserving complete credits. Do not route through the truncated log wrapper.
        print(
            "source credit before render (reported metadata): "
            + json.dumps(credit, ensure_ascii=True),
            file=sys.stderr,
            flush=True,
        )
