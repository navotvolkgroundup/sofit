#!/usr/bin/env python3
"""Rebuild insights.json from the performance log and the latest scrape.

Ranks TikTok and YouTube by avg_watch_s, Instagram and LinkedIn by views.
Keeps the prior pattern and note text unless --pattern is given.

Paths default under the home directory and can be pointed elsewhere:

    SOFIT_PERFORMANCE   ~/.sofit/performance.jsonl
    SOFIT_INSIGHTS      ~/.sofit/insights.json
    WS_SCRAPER_LOG      ~/.ws-scraper/logs/latest.json
    WS_SCRAPER_HISTORY  ~/.ws-scraper/logs/scrape-history.jsonl
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path

RANK = {
    "tiktok": "avg_watch_s",
    "youtube": "avg_watch_s",
    "instagram": "views",
    "linkedin": "views",
}
KEYS = ["hook", "views", "speaker", "episode", "date", "clip"]


def _default(env: str, *parts: str) -> Path:
    override = os.environ.get(env)
    if override:
        return Path(override).expanduser()
    return Path.home().joinpath(*parts)


def slim(row: dict, metric: str) -> dict:
    out = {k: row.get(k) for k in KEYS}
    if metric == "avg_watch_s":
        out["avg_watch_s"] = row.get("avg_watch_s")
        out["hook_hold_3s"] = row.get("hook_hold_3s")
    return out


def _load_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def build(rows: list, old: dict, log: dict, stamp: str | None, *,
          pattern: str | None = None, note_health: str = "") -> dict:
    """Insights document. `old` supplies the pattern and note to keep."""
    by_platform = {}
    for platform, metric in RANK.items():
        platform_rows = [r for r in rows if r.get("platform") == platform]
        measured = [r for r in platform_rows if isinstance(r.get(metric), (int, float))]
        measured.sort(key=lambda r: r[metric], reverse=True)
        cell = {
            "metric": metric,
            "n": len(platform_rows),
            "with_metrics": len(measured),
            "top": slim(measured[0], metric) if measured else None,
            "bottom": slim(measured[-1], metric) if measured else None,
        }
        if platform == "linkedin" and len(measured) < len(platform_rows):
            cell["note"] = "LinkedIn metrics mostly pending"
        by_platform[platform] = cell

    tiktok = sorted(
        (r for r in rows
         if r.get("platform") == "tiktok" and isinstance(r.get("avg_watch_s"), (int, float))),
        key=lambda r: r["avg_watch_s"], reverse=True)
    sessions = log.get("sessions") or {}
    return {
        "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="minutes"),
        "scrape_stamp": stamp,
        "updated_from": "performance.jsonl + scrape log",
        "rows": len(rows),
        "unique_posts": len({r.get("post_url") for r in rows}),
        "scrape_health": {
            "status": log.get("status"),
            "updated": log.get("updated"),
            "failed": log.get("failed"),
            "failed_urls_count": len(log.get("failed_urls") or []),
            "session_dead": any(v is False for v in sessions.values()),
            "sessions": sessions,
            "digest_note": note_health,
            "failed_urls": log.get("failed_urls") or [],
        },
        "rank_by": dict(RANK),
        "note": old.get("note"),
        "working_hooks": [slim(r, "avg_watch_s") for r in tiktok[:5]],
        "weak_hooks": [slim(r, "avg_watch_s") for r in tiktok[-5:][::-1]],
        "by_platform": by_platform,
        "pattern": pattern or old.get("pattern"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern")
    ap.add_argument("--note-health", default="")
    args = ap.parse_args()
    perf = _default("SOFIT_PERFORMANCE", ".sofit", "performance.jsonl")
    insights = _default("SOFIT_INSIGHTS", ".sofit", "insights.json")
    log_path = _default("WS_SCRAPER_LOG", ".ws-scraper", "logs", "latest.json")
    hist_path = _default("WS_SCRAPER_HISTORY", ".ws-scraper", "logs", "scrape-history.jsonl")
    rows = _load_jsonl(perf)
    old = json.loads(insights.read_text(encoding="utf-8"))
    log = json.loads(log_path.read_text(encoding="utf-8"))
    hist = _load_jsonl(hist_path)
    stamp = hist[-1].get("stamp") if hist else None
    out = build(rows, old, log, stamp, pattern=args.pattern,
                note_health=args.note_health)
    insights.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("updated_at", "scrape_stamp", "rows")},
                     ensure_ascii=False))
    print(json.dumps(out["by_platform"], ensure_ascii=False, indent=0)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
