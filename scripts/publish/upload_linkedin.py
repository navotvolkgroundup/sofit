#!/usr/bin/env python3
"""Semi-automated LinkedIn company-page upload+schedule.

Same contract as the other uploaders: --dry fills everything and screenshots
WITHOUT posting; only --submit clicks the final button.

    upload_linkedin.py --clip clip-18.pers --plan plan.json --dry

ORDER MATTERS AND IS NOT NEGOTIABLE: video -> Next -> text -> schedule.
Attaching a video to a post that already has text and a schedule wipes both and
flips the primary button back to "Post" - one click there publishes a
captionless video to the page's followers immediately. Choosing the file is
also not the same as attaching it; the media editor's "Next" is what commits.

Composer map (probed 2026-09-25): text is the first `.ql-editor`, the media
button is aria-label "Add media", scheduling is aria-label "Schedule post", and
`input[type=file]` only exists AFTER the media button is clicked.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROFILE = Path.home() / ".ws-scraper" / "profile"


def _cfg() -> dict:
    p = Path.home() / ".sofit" / "publish.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CLIPS_DIR = Path(_cfg().get("clips_dir", "~/Downloads")).expanduser()
ADMIN = "https://www.linkedin.com/company/{cid}/admin/dashboard/"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--plan", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry", action="store_true")
    g.add_argument("--submit", action="store_true")
    ap.add_argument("--shot", default="/tmp/linkedin-upload-preview.png")
    args = ap.parse_args()

    cid = _cfg().get("li_company_id")
    if not cid:
        print("error: li_company_id missing from ~/.sofit/publish.json", file=sys.stderr)
        return 1
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    post = next((p for p in plan["posts"] if p["clip"] == args.clip), None)
    if not post:
        print(f"error: {args.clip} not in plan", file=sys.stderr)
        return 1
    text = post.get("linkedin")
    if not text:
        print(f"error: no 'linkedin' caption for {args.clip}", file=sys.stderr)
        return 1
    video = CLIPS_DIR / f"{plan['episode']}_{args.clip}.mp4"
    if not video.exists():
        print(f"error: {video} not found", file=sys.stderr)
        return 1

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1500, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(ADMIN.format(cid=cid), wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(9_000)
        if "/login" in page.url or "authwall" in page.url:
            print(json.dumps({"status": "session_dead"}))
            ctx.close()
            return 2

        page.locator("button:has-text('Create')").first.click(timeout=15_000)
        page.wait_for_timeout(2_000)
        page.locator("text=Start a post").first.click(timeout=15_000)
        page.wait_for_timeout(5_000)

        # 1. VIDEO FIRST.
        page.locator("button[aria-label='Add media']").first.click(timeout=15_000)
        page.wait_for_selector("input[type=file]", timeout=30_000)
        page.set_input_files("input[type=file]", str(video))
        page.wait_for_timeout(20_000)          # transcode + preview

        # 2. The media editor's Next is what actually attaches it.
        for label in ("Next", "Done"):
            try:
                page.locator(
                    f"[role=dialog] button:has-text('{label}')").first.click(timeout=8_000)
                page.wait_for_timeout(4_000)
                break
            except Exception:  # noqa: BLE001
                continue

        # 3. Text, only now. Quill ignores synthetic key events on an unfocused
        # editor, so click into it first.
        ed = page.locator("div.ql-editor").first
        ed.click(timeout=15_000)
        page.wait_for_timeout(600)
        page.keyboard.insert_text(text)
        page.wait_for_timeout(2_000)

        # 4. Schedule.
        scheduled = False
        try:
            page.locator("button[aria-label='Schedule post']").first.click(timeout=10_000)
            page.wait_for_timeout(3_000)
            d, mo, y = post["date"].split("-")[::-1]
            page.fill("input[name='date'], input[id*='date']", f"{mo}/{d}/{y}")
            page.wait_for_timeout(500)
            page.fill("input[name='time'], input[id*='time']", plan["time_local"])
            page.wait_for_timeout(500)
            page.locator("[role=dialog] button:has-text('Next')").last.click(timeout=8_000)
            page.wait_for_timeout(4_000)
            # Back in the composer, or the readbacks below measure nothing.
            if page.locator("[role=dialog] input[id*='date']").count():
                raise RuntimeError("schedule dialog still open after Next")
            scheduled = True
        except Exception as e:  # noqa: BLE001
            print(f"warn: scheduling failed ({str(e)[:90]})", file=sys.stderr)

        # What the primary button says is the ground truth for what a click
        # would do. Read it back rather than assuming the schedule took.
        primary = page.evaluate(
            "() => {const b=document.querySelector('.share-actions__primary-action');"
            " return b ? b.innerText.trim() : null;}")
        has_video = page.evaluate(
            "() => !!document.querySelector('[role=dialog] video, "
            "[role=dialog] [class*=video-preview], [role=dialog] img[src^=blob]')")
        page.screenshot(path=args.shot, full_page=False)

        state = {"clip": args.clip, "date": post["date"],
                 "time": plan["time_local"], "scheduled": scheduled,
                 "primary_button": primary, "video_attached": has_video,
                 "chars": len(text), "screenshot": args.shot}

        if args.dry:
            print(json.dumps({"status": "dry_ok", **state}, ensure_ascii=False))
            ctx.close()
            return 0

        # Refuse to publish immediately when a schedule was asked for: a primary
        # button still reading "Post" means the schedule did not stick.
        if not scheduled or (primary or "").lower().startswith("post"):
            print(json.dumps({"status": "refused_would_post_now", **state},
                             ensure_ascii=False))
            ctx.close()
            return 4

        page.locator(".share-actions__primary-action").first.click(timeout=10_000)
        page.wait_for_timeout(6_000)
        print(json.dumps({"status": "submitted", **state}, ensure_ascii=False))
        ctx.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
