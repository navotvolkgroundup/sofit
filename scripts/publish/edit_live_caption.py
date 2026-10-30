#!/usr/bin/env python3
"""Rewrite the caption of a LIVE Instagram reel.

    edit_live_caption.py --url <reel url> --find "<phrase now in it>" \\
                         --caption-file new.txt --confirm "<phrase after>" [--apply]

Same Edit dialog add_collaborator.py drives, different field. Instagram marks
an edited post "Edited" publicly, so this is for a caption worth the mark.

--find is checked against the post BEFORE anything is touched: the top reel on
a profile is not the newest one you think it is (2026-09-12), and picking by
position once attributed an episode's metrics to the wrong clip.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import showhome

PROFILE = showhome.browser_profile()
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
BIDI = "‪‫‬‭‮⁦⁧⁨⁩‎‏"


def _clean(s: str) -> str:
    return "".join(c for c in s if c not in BIDI)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--find", required=True)
    ap.add_argument("--caption-file", required=True, type=Path)
    ap.add_argument("--confirm", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--shot", default="/tmp/edit_live.png")
    args = ap.parse_args()

    new_caption = args.caption_file.read_text().strip()
    if args.confirm not in _clean(new_caption):
        print(json.dumps({"status": "confirm_not_in_new_caption"}), file=sys.stderr)
        return 2

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chrome", user_agent=UA,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1500, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(8_000)

        # Poll: a slow render looks exactly like the wrong post.
        body = ""
        for _ in range(10):
            body = _clean(page.inner_text("body"))
            if args.find in body:
                break
            page.wait_for_timeout(3_000)
        if args.find not in body:
            print(json.dumps({"status": "wrong_post", "find": args.find,
                              "snippet": body[:160].replace("\n", " | ")},
                             ensure_ascii=False))
            ctx.close()
            return 3

        for lbl in ("More options", "More"):
            try:
                page.locator(f"svg[aria-label='{lbl}']").first.click(timeout=5_000)
                page.wait_for_timeout(2_000)
                break
            except Exception:  # noqa: BLE001
                continue
        page.get_by_role("button", name="Edit").first.click(timeout=8_000)
        page.wait_for_timeout(5_000)

        field = None
        for loc in (page.locator("textarea"),
                    page.locator("div[contenteditable='true']")):
            for i in range(loc.count()):
                cand = loc.nth(i)
                try:
                    cur = cand.input_value() if cand.evaluate(
                        "e => e.tagName === 'TEXTAREA'") else cand.inner_text()
                except Exception:  # noqa: BLE001
                    continue
                if args.find in _clean(cur):
                    field = cand
                    break
            if field:
                break
        if field is None:
            page.screenshot(path=args.shot, full_page=False)
            print(json.dumps({"status": "caption_field_not_found"}, ensure_ascii=False))
            ctx.close()
            return 4

        field.click(timeout=6_000)
        page.keyboard.press("Meta+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(600)
        field.type(new_caption, delay=1)
        page.wait_for_timeout(2_000)
        page.screenshot(path=args.shot, full_page=False)

        if not args.apply:
            print(json.dumps({"status": "filled_not_saved", "chars": len(new_caption)},
                             ensure_ascii=False))
            ctx.close()
            return 0

        page.get_by_role("button", name="Done").first.click(timeout=8_000)
        page.wait_for_timeout(6_000)

        page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(8_000)
        after = _clean(page.inner_text("body"))
        page.screenshot(path=args.shot.replace(".png", "-after.png"), full_page=False)
        # See edit_scheduled_caption.py: a --find the new caption reuses is not
        # evidence the save failed.
        reused = args.find in _clean(new_caption)
        ok = args.confirm in after and (reused or args.find not in after)
        print(json.dumps({"status": "saved" if ok else "save_not_confirmed",
                          "confirm_present": args.confirm in after,
                          "old_text_gone": args.find not in after,
                          "find_reused_in_new": reused},
                         ensure_ascii=False))
        ctx.close()
        return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
