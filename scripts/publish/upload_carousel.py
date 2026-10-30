#!/usr/bin/env python3
"""Post an image carousel to Instagram (immediate share, not scheduled).

    upload_carousel.py --images DIR --caption FILE [--dry] [--shot out.png]

Images post in filename order. Collaborators come from ~/.sofit/publish.json
(ig_collaborators). --dry fills everything and screenshots WITHOUT sharing;
without it the script clicks Share, waits for Instagram's own "has been
shared" confirmation (the spinner lesson: never trust the click), and prints
the new post URL from the profile grid.
"""
import argparse
import glob
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

import showhome

PROFILE = showhome.browser_profile()


def _cfg() -> dict:
    p = showhome.home() / "publish.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit(f"error: missing/invalid {p}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True, help="dir of jpg/png slides (filename order)")
    ap.add_argument("--caption", required=True, help="text file with the caption")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--shot", default="/tmp/ig-carousel.png")
    args = ap.parse_args()

    cfg = _cfg()
    files = sorted(glob.glob(str(Path(args.images).expanduser() / "*.jpg")) +
                   glob.glob(str(Path(args.images).expanduser() / "*.png")))
    if not files:
        print("error: no images found", file=sys.stderr)
        return 1
    caption = Path(args.caption).read_text(encoding="utf-8").strip()

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1500, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded",
                  timeout=60_000)
        page.wait_for_timeout(6_000)
        if not any(c["name"] == "sessionid" and c.get("value")
                   for c in ctx.cookies("https://www.instagram.com")):
            print(json.dumps({"status": "session_dead"}))
            return 2
        # "Turn on Notifications" modal covers the whole page on a fresh
        # session and swallows every sidebar click - dismiss it first.
        for label in ("Not Now", "Not now"):
            try:
                page.get_by_role("button", name=label).first.click(timeout=4_000)
                page.wait_for_timeout(1_200)
                break
            except Exception:  # noqa: BLE001
                continue

        # See upload_instagram.py: role/text locators broke 2026-08-27; the
        # svg aria-label is the only stable anchor for the composer link.
        for loc in (page.locator("a:has(svg[aria-label='New post'])"),
                    page.locator("svg[aria-label='New post']"),
                    page.get_by_role("link", name="New post")):
            try:
                loc.first.click(timeout=5_000)
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            print(json.dumps({"status": "composer_not_found"}))
            return 4
        page.wait_for_timeout(2_000)
        try:
            page.get_by_text("Post", exact=True).first.click(timeout=3_000)
            page.wait_for_timeout(1_500)
        except Exception:  # noqa: BLE001
            pass
        page.locator("input[type=file]").first.set_input_files(files)
        page.wait_for_timeout(6_000)
        try:  # keep native aspect
            page.locator("svg[aria-label='Select crop']").first.click(timeout=4_000)
            page.wait_for_timeout(800)
            page.get_by_text("Original", exact=True).first.click(timeout=3_000)
            page.wait_for_timeout(600)
        except Exception:  # noqa: BLE001
            pass
        page.get_by_role("button", name="Next").click(timeout=8_000)
        page.wait_for_timeout(2_500)
        page.get_by_role("button", name="Next").click(timeout=8_000)
        page.wait_for_timeout(2_500)
        cap = page.locator("div[contenteditable='true']").first
        cap.click(timeout=3_000)
        page.wait_for_timeout(400)
        page.keyboard.insert_text(caption)
        page.wait_for_timeout(800)
        page.keyboard.type(" ")  # dismiss hashtag autocomplete
        page.wait_for_timeout(800)
        collab_added = []
        try:
            box = page.get_by_placeholder("Add collaborators").first
            box.click(timeout=4_000)
            page.wait_for_timeout(1_200)
            for name in cfg.get("ig_collaborators", []):
                box.fill(name)
                page.wait_for_timeout(2_000)
                try:
                    page.get_by_text(name, exact=True).first.click(timeout=4_000)
                    collab_added.append(name)
                    page.wait_for_timeout(800)
                except Exception:  # noqa: BLE001
                    print(f"warn: collaborator {name} not found", file=sys.stderr)
            page.get_by_role("button", name="Done").click(timeout=4_000)
            page.wait_for_timeout(1_000)
        except Exception as e:  # noqa: BLE001
            print(f"warn: collaborators step failed ({e})", file=sys.stderr)

        if args.dry:
            page.screenshot(path=args.shot)
            print(json.dumps({"status": "dry_ok", "slides": len(files),
                              "collaborators": collab_added,
                              "screenshot": args.shot}, ensure_ascii=False))
            ctx.close()
            return 0

        # Role-based, NOT get_by_text: bare "Share" also matches an svg <title>.
        page.get_by_role("button", name="Share").first.click(timeout=8_000)
        shared = False
        for _ in range(90):
            page.wait_for_timeout(2_000)
            body = page.evaluate("()=>document.body.innerText")
            if "has been shared" in body or "Post shared" in body:
                shared = True
                break
        page.screenshot(path=args.shot)
        if not shared:
            print(json.dumps({"status": "share_not_confirmed",
                              "screenshot": args.shot}))
            ctx.close()
            return 3
        # grab the new post URL from the profile grid
        page.goto(f"https://www.instagram.com/{cfg['ig_profile']}/",
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8_000)
        url = page.evaluate(
            "()=>{const a=document.querySelector(\"a[href*='/p/']\");"
            "return a?a.href:null;}")
        print(json.dumps({"status": "shared", "slides": len(files),
                          "collaborators": collab_added, "post_url": url},
                         ensure_ascii=False))
        ctx.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
