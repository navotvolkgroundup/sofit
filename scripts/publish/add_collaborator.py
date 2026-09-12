"""Invite a collaborator on a LIVE Instagram reel.

The uploader's collaborator step is flaky - Instagram often returns no
suggestion and the run only warns - so posts go out crediting the wrong set of
people. This fixes one after the fact.

    add_collaborator.py --url <reel url> --verify <caption phrase> \\
                        --handle tortsuk [--apply]

Without --apply it stops before saving and reports what it found. The caption
phrase is mandatory and checked first: picking an Instagram post by position
once attributed a whole episode's metrics to the wrong clip (2026-09-04), and
the top reel is not the newest one you think it is (2026-09-12).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROFILE = Path.home() / ".ws-scraper" / "profile"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--verify", required=True,
                    help="a phrase that MUST appear in this post's caption")
    ap.add_argument("--handle", required=True)
    ap.add_argument("--apply", action="store_true", help="actually save")
    ap.add_argument("--shot", default="/tmp/collab.png")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chrome", user_agent=UA,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1400, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(8_000)

        # Poll for the caption: a slow render looks identical to the wrong post,
        # and the first version called that "wrong_post" on a URL it had already
        # verified seconds earlier. Absence only counts after it has had time.
        body = ""
        for _ in range(10):
            body = page.inner_text("body")
            if args.verify in body:
                break
            page.wait_for_timeout(3_000)
        if args.verify not in body:
            print(json.dumps({"status": "wrong_post", "verify": args.verify,
                              "snippet": body[:160].replace("\n", " | ")},
                             ensure_ascii=False))
            ctx.close()
            return 2
        if args.handle in body:
            print(json.dumps({"status": "already_credited", "handle": args.handle},
                             ensure_ascii=False))
            ctx.close()
            return 0

        for lbl in ("More options", "More"):
            try:
                page.locator(f"svg[aria-label='{lbl}']").first.click(timeout=5_000)
                page.wait_for_timeout(2_000)
                break
            except Exception:  # noqa: BLE001
                continue
        page.get_by_role("button", name="Edit").first.click(timeout=8_000)
        page.wait_for_timeout(5_000)

        page.get_by_text("Tag people", exact=True).first.click(timeout=8_000)
        page.wait_for_timeout(2_500)
        try:
            page.get_by_text("Invite collaborator", exact=False).first.click(timeout=6_000)
            page.wait_for_timeout(2_000)
        except Exception:  # noqa: BLE001
            pass  # some layouts show the search box directly

        box = page.locator("input[placeholder*='Search'], input[type=text]").last
        box.fill(args.handle, timeout=8_000)
        page.wait_for_timeout(4_000)
        # take the suggestion whose text is EXACTLY the handle, never the first row
        opt = page.get_by_text(args.handle, exact=True).first
        found = opt.count() > 0
        page.screenshot(path=args.shot, full_page=False)
        if not found:
            print(json.dumps({"status": "no_suggestion", "handle": args.handle,
                              "hint": "Instagram rate-limits this search; retry later"},
                             ensure_ascii=False))
            ctx.close()
            return 3
        if not args.apply:
            print(json.dumps({"status": "found_not_applied", "handle": args.handle},
                             ensure_ascii=False))
            ctx.close()
            return 0

        # Click the ROW, not the label: the selector is a radio at the far end,
        # and clicking the handle text alone leaves it unselected.
        tagged = page.evaluate("""(handle) => {
            const label = [...document.querySelectorAll('*')].find(e =>
                e.offsetParent !== null && (e.innerText || '').trim() === handle);
            if (!label) return false;
            for (let n = label; n; n = n.parentElement) {
                const r = n.getBoundingClientRect();
                if (r.width > 200 && r.height > 30 && r.height < 90) {
                    n.setAttribute('data-sofit-collab', '1');
                    return true;
                }
            }
            return false;
        }""", args.handle)
        if not tagged:
            print(json.dumps({"status": "row_not_found", "handle": args.handle},
                             ensure_ascii=False))
            ctx.close()
            return 5
        page.locator("[data-sofit-collab]").first.click(timeout=6_000)
        page.wait_for_timeout(1_500)
        # two separate Done buttons: the collaborators popup's, then Edit info's
        for _ in range(2):
            try:
                page.get_by_role("button", name="Done").first.click(timeout=5_000)
                page.wait_for_timeout(2_500)
            except Exception:  # noqa: BLE001
                break

        # verify from a fresh load - the open dialog is not evidence
        page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(7_000)
        ok = args.handle in page.inner_text("body")
        page.screenshot(path=args.shot.replace(".png", "-after.png"), full_page=False)
        ctx.close()
        print(json.dumps({"status": "credited" if ok else "save_not_confirmed",
                          "handle": args.handle}, ensure_ascii=False))
        return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
