"""Remove the SCHEDULED post for a clip on one platform.

None of the three platforms lets you swap the video on a scheduled post, so a
caption fix that lands after scheduling means delete + re-upload.

The row is matched by CAPTION TEXT and the match must be UNIQUE - picking a row
positionally once attributed a whole episode's metrics to the wrong clip
(2026-09-04). Zero matches or more than one is a hard failure, never a guess.

    unschedule.py --clip clip-4 --plan PLAN --platform tiktok --list
    unschedule.py --clip clip-4 --plan PLAN --platform tiktok --delete

--list reports what it matched and touches nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROFILE = Path.home() / ".ws-scraper" / "profile"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
BIDI = re.compile(r"[‪-‮⁦-⁩‎‏]")

TIKTOK = "https://www.tiktok.com/tiktokstudio/content"
YOUTUBE = "https://studio.youtube.com/channel/UC/videos/short"
INSTAGRAM = "https://www.instagram.com/scheduled_content/"

# Count the VISIBLE leaf nodes carrying the needle. Script payloads and hidden
# nodes both matched during development and produced false "found" reports.
COUNT_JS = """(needle) => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  return [...document.querySelectorAll('*')].filter(e =>
    e.children.length === 0 && !SKIP.has(e.tagName) && e.offsetParent !== null &&
    (e.textContent || '').includes(needle)).length;
}"""


def needle(post: dict, platform: str) -> str:
    """A distinctive phrase from this clip's caption, for matching a row.

    Listings truncate captions, so use an opening run of words - long enough to
    be unique within the episode, short enough to survive the ellipsis.
    """
    text = post["youtube"] if platform == "youtube" else post[platform]
    first = next((ln for ln in BIDI.sub("", text).split("\n") if ln.strip()), "")
    words = [w for w in first.split() if not w.startswith("#")]
    return " ".join(words[:4])


def dismiss(page) -> None:
    for label in ("Not Now", "Not now"):
        try:
            page.get_by_role("button", name=label).first.click(timeout=3_000)
            page.wait_for_timeout(1_000)
            return
        except Exception:  # noqa: BLE001
            continue


def open_listing(page, platform: str, post: dict) -> None:
    page.goto({"tiktok": TIKTOK, "youtube": YOUTUBE, "instagram": INSTAGRAM}[platform],
              wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(20_000)
    dismiss(page)
    if platform == "tiktok":
        # the studio list is paginated; its own search is more reliable than
        # scrolling for a row that may be pages down
        try:
            box = page.locator("input[placeholder*='Search for post description']").first
            box.fill(needle(post, "tiktok"), timeout=8_000)
            page.keyboard.press("Enter")
            page.wait_for_timeout(6_000)
        except Exception:  # noqa: BLE001
            pass
    elif platform == "instagram":
        import datetime as dt
        target = dt.date.fromisoformat(post["date"])

        def week(d):
            return d - dt.timedelta(days=(d.weekday() + 1) % 7)   # IG weeks start Sunday

        hops = (week(target) - week(dt.date.today())).days // 7
        for _ in range(max(0, min(hops, 8))):
            page.locator("div[role=button]:has(svg[aria-label='Next week'])").first.click(
                timeout=8_000)
            page.wait_for_timeout(4_000)


# Tag the row that owns the needle so Playwright can address it by attribute.
# Built in JS on purpose: an XPath text() literal cannot carry Hebrew through
# json.dumps (it escapes to \\uXXXX, which XPath does not decode), and every
# class name on these pages is a build hash.
TAG_ROW_JS = """([needle, minButtons, needImg]) => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  document.querySelectorAll('[data-sofit-row]').forEach(e =>
    e.removeAttribute('data-sofit-row'));
  const leaf = [...document.querySelectorAll('*')].find(e =>
    e.children.length === 0 && !SKIP.has(e.tagName) && e.offsetParent !== null &&
    (e.textContent || '').includes(needle));
  if (!leaf) return {tagged: false, why: 'needle not visible'};
  for (let n = leaf; n; n = n.parentElement) {
    const btns = n.querySelectorAll('button, [role=button]').length;
    const imgs = n.querySelectorAll('img, video').length;
    if (btns >= minButtons && (!needImg || imgs > 0)) {
      n.setAttribute('data-sofit-row', '1');
      return {tagged: true, tag: n.tagName, buttons: btns, imgs: imgs};
    }
  }
  return {tagged: false, why: 'no ancestor with enough controls'};
}"""


def tag_row(page, want: str, min_buttons: int, need_img: bool = False):
    res = page.evaluate(TAG_ROW_JS, [want, min_buttons, need_img])
    if not res.get("tagged"):
        raise RuntimeError(f"row not tagged: {res.get('why')}")
    return page.locator("[data-sofit-row]").first


def delete_tiktok(page, want: str) -> dict:
    # the row carries 4 action buttons (edit / cover / comments / more)
    row = tag_row(page, want, min_buttons=4)
    row.scroll_into_view_if_needed(timeout=8_000)
    row.locator("button, [role=button]").last.click(timeout=8_000)   # the "..."
    page.wait_for_timeout(1_500)
    page.get_by_text("Delete", exact=True).first.click(timeout=8_000)
    page.wait_for_timeout(1_500)
    for name in ("Delete", "Confirm", "OK"):             # confirmation dialog
        try:
            page.get_by_role("button", name=name, exact=True).last.click(timeout=4_000)
            break
        except Exception:  # noqa: BLE001
            continue
    page.wait_for_timeout(5_000)
    return {}


def delete_youtube(page, want: str) -> dict:
    row = tag_row(page, want, min_buttons=1)
    row.scroll_into_view_if_needed(timeout=8_000)
    row.hover(timeout=8_000)
    row.locator("ytcp-icon-button#menu-button, #menu-button").first.click(timeout=8_000)
    page.wait_for_timeout(1_500)
    page.get_by_text(re.compile(r"Delete forever|Delete"), exact=False).first.click(timeout=8_000)
    page.wait_for_timeout(2_000)
    # YT gates the destructive confirm behind an "I understand" checkbox
    try:
        page.locator("ytcp-checkbox-lit, tp-yt-paper-checkbox").first.click(timeout=6_000)
        page.wait_for_timeout(1_000)
    except Exception:  # noqa: BLE001
        pass
    page.get_by_role("button", name=re.compile("DELETE FOREVER|Delete forever",
                                               re.I)).last.click(timeout=8_000)
    page.wait_for_timeout(6_000)
    return {}


def delete_instagram(page, want: str) -> dict:
    tile = tag_row(page, want, min_buttons=0, need_img=True)
    tile.scroll_into_view_if_needed(timeout=8_000)
    tile.click(timeout=8_000)
    page.wait_for_timeout(3_000)
    page.get_by_text(re.compile("Delete", re.I)).first.click(timeout=8_000)
    page.wait_for_timeout(1_500)
    for name in ("Delete", "Delete post", "Confirm"):
        try:
            page.get_by_role("button", name=name, exact=False).last.click(timeout=4_000)
            break
        except Exception:  # noqa: BLE001
            continue
    page.wait_for_timeout(5_000)
    return {}


DELETERS = {"tiktok": delete_tiktok, "youtube": delete_youtube,
            "instagram": delete_instagram}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--platform", required=True, choices=sorted(DELETERS))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="report the match, change nothing")
    g.add_argument("--delete", action="store_true", help="remove the scheduled post")
    ap.add_argument("--shot", default="/tmp/unschedule.png")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    post = next((p for p in plan["posts"] if p["clip"] == args.clip), None)
    if not post:
        print(f"error: {args.clip} not in plan", file=sys.stderr)
        return 1
    want = needle(post, args.platform)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chrome", user_agent=UA,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1500, "height": 1100})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        open_listing(page, args.platform, post)
        page.screenshot(path=args.shot, full_page=True)
        hits = page.evaluate(COUNT_JS, want)
        base = {"platform": args.platform, "clip": args.clip, "needle": want,
                "matches": hits, "date": post["date"], "time": plan["time_local"],
                "screenshot": args.shot}

        if hits != 1:
            print(json.dumps({**base, "status": "no_unique_match",
                              "hint": "0 = already gone or listing not loaded; "
                                      ">1 = needle is not distinctive enough"},
                             ensure_ascii=False))
            ctx.close()
            return 4
        if args.list:
            print(json.dumps({**base, "status": "match_ok"}, ensure_ascii=False))
            ctx.close()
            return 0

        # DISARMED 2026-09-11. On its first run against the live account this
        # deleted a DIFFERENT post than the one it had matched and verified:
        # the target (WS210 clip-2, scheduled Sep 16) survived, and WS208
        # clip-9 - a published post with 2,400 views - was destroyed instead.
        # The tagged row and the row that received the click came apart
        # somewhere between the "..." menu, the first page element reading
        # "Delete", and a confirm button picked with .last.
        # Do not re-arm until the whole click path has been proven end to end
        # against a throwaway post on a throwaway account. --list is safe.
        if not os.environ.get("SOFIT_UNSCHEDULE_ARMED"):
            print(json.dumps({**base, "status": "disarmed",
                              "why": "deleted the wrong post on first live run; "
                                     "see the comment in unschedule.py",
                              "hint": "delete by hand, or set SOFIT_UNSCHEDULE_ARMED=1 "
                                      "only after proving the click path safely"},
                             ensure_ascii=False))
            ctx.close()
            return 7

        try:
            DELETERS[args.platform](page, want)
        except Exception as e:  # noqa: BLE001
            page.screenshot(path=args.shot.replace(".png", "-error.png"), full_page=True)
            print(json.dumps({**base, "status": "delete_failed", "error": str(e)[:200]},
                             ensure_ascii=False))
            ctx.close()
            return 5

        # verify from a fresh load: the in-page DOM lies after a mutation
        open_listing(page, args.platform, post)
        left = page.evaluate(COUNT_JS, want)
        page.screenshot(path=args.shot.replace(".png", "-after.png"), full_page=True)
        ctx.close()
        print(json.dumps({**base, "status": "deleted" if left == 0 else "still_present",
                          "matches_after": left}, ensure_ascii=False))
        return 0 if left == 0 else 6


if __name__ == "__main__":
    raise SystemExit(main())
