#!/usr/bin/env python3
"""Rewrite the caption of a SCHEDULED Instagram post, in place.

    edit_scheduled_caption.py --find "<phrase now in the caption>" \\
                              --caption-file new.txt \\
                              --confirm "<phrase that must be there after>" \\
                              [--weeks 1] [--apply]

Instagram's scheduled tile opens an Edit panel with the caption field and a
Save button, so a wording fix needs no delete and no re-upload. That matters:
the delete path destroyed a live post with 2,400 views on its first run
(2026-09-18), and nothing here touches it.

Without --apply it fills the field and screenshots WITHOUT saving.

--find is mandatory and matched against the tile's own text. Picking an
Instagram post by position has gone wrong twice - once attributing a whole
episode's metrics to the wrong clip - so position is never used here. The
phrase has to match exactly ONE tile across the week, or the run refuses.
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
SCHEDULED = "https://www.instagram.com/scheduled_content/"

# Instagram strips these when it stores the caption, so comparing raw text
# against what we typed gives false mismatches.
BIDI = "‪‫‬‭‮⁦⁧⁨⁩‎‏"


def _clean(s: str) -> str:
    return "".join(c for c in s if c not in BIDI)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--find", required=True,
                    help="phrase currently in the caption; must hit exactly one tile")
    ap.add_argument("--caption-file", required=True, type=Path)
    ap.add_argument("--confirm", required=True,
                    help="phrase that must appear in the caption after saving")
    ap.add_argument("--weeks", type=int, default=0,
                    help="click Next week this many times first (page opens on the current week)")
    ap.add_argument("--apply", action="store_true", help="actually save")
    ap.add_argument("--shot", default="/tmp/edit_caption.png")
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
        page.goto(SCHEDULED, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(9_000)

        for _ in range(args.weeks):
            page.get_by_role("button", name="Next week").first.click(timeout=8_000)
            page.wait_for_timeout(5_000)

        # Tag the tile by its own text. A tile is tall and narrow; the week
        # container also carries the phrase, so shape is what separates them.
        hits = page.evaluate("""(needle) => {
            const ok = [...document.querySelectorAll('div[role=button], a')].filter(e => {
                const r = e.getBoundingClientRect();
                return r.width > 100 && r.width < 280 && r.height > 180
                       && (e.innerText || '').includes(needle);
            });
            ok.forEach(e => e.setAttribute('data-sofit-edit', '1'));
            return ok.length;
        }""", args.find)
        if hits != 1:
            page.screenshot(path=args.shot, full_page=False)
            print(json.dumps({"status": "tile_not_unique", "matches": hits,
                              "find": args.find, "weeks": args.weeks},
                             ensure_ascii=False))
            ctx.close()
            return 3

        page.locator("[data-sofit-edit]").first.click(timeout=8_000)
        page.wait_for_timeout(5_000)

        # The caption field is the one editable box that already holds --find.
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

        page.get_by_role("button", name="Save").first.click(timeout=8_000)
        page.wait_for_timeout(6_000)

        # Verify from a fresh load. The panel still showing the new text proves
        # nothing - a save that silently failed looks identical until reload.
        page.goto(SCHEDULED, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(9_000)
        for _ in range(args.weeks):
            page.get_by_role("button", name="Next week").first.click(timeout=8_000)
            page.wait_for_timeout(5_000)
        body = _clean(page.inner_text("body"))
        page.screenshot(path=args.shot.replace(".png", "-after.png"), full_page=False)
        # --find surviving only proves a failed save when the NEW caption does
        # not contain it too. Two versions of one caption often share an
        # opening, and that scored a correct save as a failure (2026-09-18).
        reused = args.find in _clean(new_caption)
        ok = args.confirm in body and (reused or args.find not in body)
        print(json.dumps({"status": "saved" if ok else "save_not_confirmed",
                          "confirm_present": args.confirm in body,
                          "old_text_gone": args.find not in body,
                          "find_reused_in_new": reused},
                         ensure_ascii=False))
        ctx.close()
        return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
