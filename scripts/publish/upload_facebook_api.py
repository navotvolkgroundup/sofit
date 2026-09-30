#!/usr/bin/env python3
"""Publish a clip to a Facebook Page through the Graph API instead of the composer.

The Playwright composer path (upload_facebook.py) works, but it has cost us a
post on the wrong page (2026-09-25) and a silent skipped log, and every fix is
a selector that Meta can move. The Graph API does this one job properly:
`POST /{page-id}/videos` takes the local .mp4 as multipart form data and takes
`scheduled_publish_time` natively, so there is nothing to host and no scheduler
to run on our side.

This is the ONLY surface where the API is a clean win. Instagram has no
scheduling parameter at all (Meta's own guidance is that the app holds the job)
and needs every clip at a public HTTPS URL, and LinkedIn's Posts API cannot
schedule either - both verified against their docs, 2026-09-30. So this script
covers Facebook and nothing else; the other platforms stay on Playwright.

Setup, once:
  1. Create a Meta app (Business type). Development mode is enough - App Review
     is only needed to act for accounts outside the app.
  2. Grant it pages_manage_posts, pages_read_engagement, pages_show_list.
  3. Get a LONG-LIVED page access token for the Weekly Sync page and put it in
     ~/.sofit/publish.json as "fb_page_token" (or export FB_PAGE_TOKEN).
     A short-lived token expires in ~1h and will fail mid-batch.

Usage:
  upload_facebook_api.py --clip car --plan publish-plan.json --dry
  upload_facebook_api.py --clip car --plan publish-plan.json --submit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).parent))

GRAPH = "https://graph.facebook.com/v21.0"
CFG_PATH = Path.home() / ".sofit" / "publish.json"


def _post_multipart(url: str, fields: dict, filename: str, blob: bytes) -> tuple[int, dict]:
    """One multipart POST, stdlib only.

    sofit ships with no HTTP dependency and this is the only script that needs
    one, so the body is assembled by hand rather than adding `requests` to a
    public repo for a single call.
    """
    boundary = uuid.uuid4().hex
    sep = f"--{boundary}\r\n".encode()
    body = bytearray()
    for k, v in fields.items():
        body += sep
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += str(v).encode() + b"\r\n"
    body += sep
    body += (f'Content-Disposition: form-data; name="source"; '
             f'filename="{filename}"\r\n'
             f"Content-Type: video/mp4\r\n\r\n").encode()
    body += blob + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url, data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:      # Graph puts the reason in the body
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {"raw": str(e)[:300]}


def _get_json(url: str, params: dict) -> dict:
    try:
        with urllib.request.urlopen(
                f"{url}?{urllib.parse.urlencode(params)}", timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:  # noqa: BLE001
        return {}


def _cfg() -> dict:
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _token() -> str:
    return os.environ.get("FB_PAGE_TOKEN") or _cfg().get("fb_page_token", "")


def _scheduled_epoch(date: str, hhmm: str) -> int:
    """Local wall-clock -> unix seconds, the way the plan means it.

    Meta requires 10 minutes to 6 months out and rejects anything else, so the
    window is checked here rather than discovered from a Graph error.
    """
    when = datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H:%M").astimezone()
    epoch = int(when.timestamp())
    delta = epoch - int(time.time())
    if delta < 600:
        raise SystemExit(json.dumps(
            {"status": "too_soon", "want": f"{date} {hhmm}",
             "why": "Meta needs at least 10 minutes' lead"}))
    if delta > 182 * 86400:
        raise SystemExit(json.dumps(
            {"status": "too_far", "want": f"{date} {hhmm}",
             "why": "Meta accepts at most 6 months ahead"}))
    return epoch


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--plan", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry", action="store_true", help="show the call, post nothing")
    g.add_argument("--submit", action="store_true")
    args = ap.parse_args()

    cfg = _cfg()
    page_id = cfg.get("fb_asset_id")
    if not page_id:
        print(json.dumps({"status": "no_page_id",
                          "fix": "set fb_asset_id in ~/.sofit/publish.json"}))
        return 2
    token = _token()
    if not token and args.submit:
        print(json.dumps({"status": "no_token",
                          "fix": "set fb_page_token in ~/.sofit/publish.json "
                                 "or export FB_PAGE_TOKEN"}))
        return 2

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    post = next((p for p in plan["posts"] if p["clip"] == args.clip), None)
    if not post:
        print(json.dumps({"status": "clip_not_in_plan", "clip": args.clip}))
        return 1
    clips_dir = Path(cfg.get("clips_dir", "~/Downloads")).expanduser()
    video = clips_dir / f"{plan['episode']}_{args.clip}.mp4"
    if not video.exists():
        print(json.dumps({"status": "video_missing", "path": str(video)}))
        return 1

    # No RTL wrapping here: that is a fix for captions rendered in an LTR
    # composer box. The Graph API stores the description as given.
    caption = post.get("facebook") or post["instagram"]
    when = _scheduled_epoch(post["date"], post.get("time") or plan.get("time_local", "13:00"))

    if args.dry:
        print(json.dumps({
            "status": "dry_ok", "clip": args.clip, "page_id": page_id,
            "video": str(video), "mb": round(video.stat().st_size / 1048576, 1),
            "scheduled_at": datetime.fromtimestamp(when).isoformat(timespec="minutes"),
            "caption_head": caption.strip().splitlines()[0][:70],
            "token": "set" if token else "MISSING",
        }, ensure_ascii=False))
        return 0

    status, body = _post_multipart(
        f"{GRAPH}/{page_id}/videos",
        {"description": caption, "published": "false",
         "scheduled_publish_time": str(when), "access_token": token},
        video.name, video.read_bytes())
    if status != 200 or "id" not in body:
        print(json.dumps({"status": "upload_failed", "http": status,
                          "error": body.get("error", body)}, ensure_ascii=False))
        return 4

    video_id = body["id"]
    # Read the schedule back off the object. Every silent failure this pipeline
    # has had looked like a success at the call site, so the only thing worth
    # printing is what Meta says it stored.
    got = _get_json(f"{GRAPH}/{video_id}",
                    {"fields": "id,scheduled_publish_time,published",
                     "access_token": token})
    got_epoch = int(got.get("scheduled_publish_time") or 0)
    out = {"status": "scheduled" if got_epoch == when else "scheduled_unverified",
           "clip": args.clip, "video_id": video_id,
           "post_url": f"https://www.facebook.com/{video_id}",
           "want": datetime.fromtimestamp(when).isoformat(timespec="minutes"),
           "got": (datetime.fromtimestamp(got_epoch).isoformat(timespec="minutes")
                   if got_epoch else None)}
    import autolog
    out.update(autolog.log(args.plan, args.clip, "facebook", out["post_url"]))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["status"] == "scheduled" else 5


if __name__ == "__main__":
    raise SystemExit(main())
