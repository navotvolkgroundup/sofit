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

# On YouTube the needle can legitimately appear twice in ONE row - once in the
# title and once in the description - so counting text nodes reports a phantom
# second match. Count rows, which is what "is this unique?" actually means.
COUNT_ROWS_JS = """(needle) => [...document.querySelectorAll('ytcp-video-row')]
    .filter(r => (r.innerText || '').includes(needle)).length"""


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
TAG_ROW_JS = """([needle, maxText, maxH, minW]) => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  document.querySelectorAll('[data-sofit-row]').forEach(e =>
    e.removeAttribute('data-sofit-row'));
  const leaf = [...document.querySelectorAll('*')].find(e =>
    e.children.length === 0 && !SKIP.has(e.tagName) && e.offsetParent !== null &&
    (e.textContent || '').includes(needle));
  if (!leaf) return {tagged: false, why: 'needle not visible'};
  // Climb to the widest element that still reads as ONE row: it owns the
  // thumbnail and its text stays under the row budget. Counting buttons was
  // the old rule and it climbed straight past the row into the table, because
  // these action controls are not <button> or [role=button] at all.
  // A row is WIDE AND SHORT. Text length alone is not enough: with the studio
  // search filtered to one row, even the page container is under the text
  // budget, and the "rightmost control" inside it was the account avatar - the
  // click opened Log out (2026-09-11). Height is what separates a row (~100px)
  // from a page container (~1000px), so bound it and take the widest survivor.
  let best = null;
  for (let n = leaf; n; n = n.parentElement) {
    const text = (n.innerText || '').trim();
    if (text.length > maxText) break;
    const r = n.getBoundingClientRect();
    if (r.height > maxH) break;
    if (n.querySelectorAll('img, video, canvas').length > 0 && r.width > minW) best = n;
  }
  if (!best) return {tagged: false, why: 'no row-shaped ancestor with a thumbnail'};
  best.setAttribute('data-sofit-row', '1');
  const r = best.getBoundingClientRect();
  return {tagged: true, tag: best.tagName, chars: (best.innerText || '').length,
          box: {x: r.x, y: r.y, w: r.width, h: r.height}};
}"""


# The "..." control: the rightmost small clickable thing inside the row. Found
# by cursor style rather than tag name, since these are plain divs.
ROW_MENU_JS = """() => {
  const row = document.querySelector('[data-sofit-row]');
  if (!row) return null;
  const cands = [...row.querySelectorAll('*')].filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 8 && r.width < 70 && r.height > 8 && r.height < 70 &&
           getComputedStyle(e).cursor === 'pointer';
  });
  if (!cands.length) return null;
  // prefer a real BUTTON over the <svg> painted inside it - clicking the icon
  // by coordinate did not open the menu, clicking the button does
  const btns = cands.filter(e => e.tagName === 'BUTTON');
  const pool = btns.length ? btns : cands;
  const pick = pool.reduce((a, b) =>
    b.getBoundingClientRect().x > a.getBoundingClientRect().x ? b : a);
  document.querySelectorAll('[data-sofit-menu]').forEach(e =>
    e.removeAttribute('data-sofit-menu'));
  pick.setAttribute('data-sofit-menu', '1');
  const r = pick.getBoundingClientRect();
  return {tag: pick.tagName,
          x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
}"""


# A single row's text is short; a whole table's is thousands of characters. The
# 2026-09-10 misfire tagged a container spanning EVERY row, so `.last` button was
# a different row's menu. Bounding the tagged element's text rules that out.
MAX_ROW_TEXT = 600

# The shape of "one item" is not the same everywhere: a studio table row is wide
# and short, while an Instagram calendar tile is a narrow portrait card. Using
# the table numbers on Instagram rejected every tile (2026-09-12).
SHAPE = {                     # platform -> (max height, min width)
    "tiktok": (220, 300),
    "youtube": (220, 300),
    # the tile that responds to a click is the whole card, taller than it
    # looks; 520 clipped it and the click landed on the dead thumbnail
    "instagram": (900, 60),
}


def tag_row(page, want: str, platform: str = "tiktok"):
    max_h, min_w = SHAPE[platform]
    res = page.evaluate(TAG_ROW_JS, [want, MAX_ROW_TEXT, max_h, min_w])
    if not res.get("tagged"):
        raise RuntimeError(f"row not tagged: {res.get('why')}")
    row = page.locator("[data-sofit-row]").first
    text = row.inner_text(timeout=8_000)
    if want not in text:
        raise RuntimeError("tagged element does not contain the needle")
    if len(text) > MAX_ROW_TEXT:
        raise RuntimeError(
            f"tagged element holds {len(text)} chars - that is a container, "
            f"not one row; refusing to click inside it")
    return row


# Whole-listing size, so collateral damage is detectable. Yesterday's misfire
# left the target in place (which the needle check caught) while destroying a
# post 20 rows away (which nothing caught). Exactly one item must disappear.
INVENTORY_JS = {
    # No usable listing-size signal on TikTok. The "Posts N" header counts UP on
    # a new post but does NOT count down after a confirmed deletion (measured
    # 2026-09-11), and every other count is taken after the search filter has
    # already narrowed the list, so it measures the match, not the inventory.
    # Collateral has to be checked one level up - re-list the episode's other
    # posts after a delete - so don't pretend to a guarantee here.
    "tiktok": """() => null""",
    # Same trap as TikTok: the row count is the lazily-loaded PAGE SIZE, so it
    # refills after a deletion and stays flat. Measured 2026-09-11 against a
    # confirmed delete: 30 -> 30. Audit the episode instead.
    "youtube": """() => null""",
    "instagram": """() => [...document.querySelectorAll('img')]
        .filter(i => i.alt === 'Scheduled post thumbnail' && i.offsetParent !== null)
        .length""",
}


def inventory(page, platform: str):
    try:
        return page.evaluate(INVENTORY_JS[platform])
    except Exception:  # noqa: BLE001
        return None


def menu_items(page) -> list[str]:
    """Text of whatever menu is currently open. Menus render in a portal outside
    the row, so they cannot be scoped to it - assert the SHAPE instead."""
    # NOT leaf-only: a menu item is an icon plus a label, so the element whose
    # text reads "Delete" usually has an <svg> child. Match on trimmed innerText
    # and keep the innermost such element, so one item counts once.
    return page.evaluate("""() => {
        const WORDS = /^(Delete|Download|Delete forever|Edit|Duplicate)$/i;
        const hits = [...document.querySelectorAll('*')].filter(e =>
            e.offsetParent !== null && WORDS.test((e.innerText || '').trim()));
        return hits
          .filter(e => !hits.some(o => o !== e && e.contains(o)))
          .map(e => (e.innerText || '').trim());
    }""")


def delete_tiktok(page, want: str) -> dict:
    # the row carries 4 action buttons (edit / cover / comments / more)
    row = tag_row(page, want, "tiktok")
    row.scroll_into_view_if_needed(timeout=8_000)
    tag_row(page, want, "tiktok")                       # re-tag: scrolling moved the box
    spot = page.evaluate(ROW_MENU_JS)
    if not spot:
        raise RuntimeError("no row action control found")
    ctl = page.locator("[data-sofit-menu]").first
    ctl.hover(timeout=8_000)                  # the icons only arm on hover
    page.wait_for_timeout(400)
    ctl.click(timeout=8_000)                  # the "..."
    page.wait_for_timeout(2_500)
    # the row menu is exactly Download + Delete; anything else means the click
    # landed somewhere unexpected and clicking "Delete" now is a coin toss
    items = menu_items(page)
    if sorted(i.lower() for i in items) != ["delete", "download"]:
        raise RuntimeError(f"unexpected row menu {items!r} - not clicking Delete")
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
    # Studio gives every row its own custom element, which is a far better
    # anchor than any geometry: scope to it directly.
    rows = page.locator("ytcp-video-row").filter(has_text=want)
    n = rows.count()
    if n != 1:
        raise RuntimeError(f"{n} ytcp-video-row matched the needle, need exactly 1")
    row = rows.first
    row.scroll_into_view_if_needed(timeout=8_000)
    row.hover(timeout=8_000)
    # by label, not position: `.last` grabbed the visibility control and opened
    # the schedule editor instead of the menu (2026-09-11)
    row.locator("[aria-label='Options']").first.click(timeout=8_000)
    page.wait_for_timeout(1_500)
    items = menu_items(page)
    if not any(i.lower().startswith("delete") for i in items):
        raise RuntimeError(f"unexpected row menu {items!r} - not clicking Delete")
    # click the menu ITEM, not the text span inside it - the span resolves but
    # is not the click target, so a direct click just times out
    item = page.locator(
        "tp-yt-paper-item, ytcp-text-menu-item, [role=menuitem], [role=option]"
    ).filter(has_text=re.compile(r"Delete", re.I)).first
    item.click(timeout=8_000)
    page.wait_for_timeout(2_000)
    # The confirm dialog NAMES the video it is about to destroy. That is the
    # best guard available anywhere in this file - read it back before saying
    # yes, so a mis-clicked row cannot get past this point.
    page.get_by_text(re.compile(r"Permanently delete this (draft )?video", re.I)).first.wait_for(
        timeout=10_000)
    shown = page.evaluate("""() => {
        const t = [...document.querySelectorAll('*')].find(e =>
            e.offsetParent !== null &&
            /Permanently delete this (draft )?video/i.test(e.innerText || '') &&
            (e.innerText || '').length < 900);
        return t ? t.innerText : '';
    }""")
    if want not in shown:
        raise RuntimeError(f"confirm dialog is about a different video: "
                           f"{shown[:120]!r}")
    # "Delete forever" stays disabled until the acknowledgement is ticked
    # the checkbox element itself is not the click target (its input lives in a
    # shadow root); clicking its label toggles it
    page.get_by_text(re.compile("I understand that deleting", re.I)).first.click(
        timeout=8_000)
    page.wait_for_timeout(800)
    page.get_by_role("button", name=re.compile(r"Delete (forever|draft video)", re.I)).first.click(
        timeout=8_000)
    page.wait_for_timeout(6_000)
    return {}


def delete_instagram(page, want: str) -> dict:
    tile = tag_row(page, want, "instagram")
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
        counter = COUNT_ROWS_JS if args.platform == 'youtube' else COUNT_JS
        hits = page.evaluate(counter, want)
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

        before = inventory(page, args.platform)
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
        left = page.evaluate(counter, want)
        after = inventory(page, args.platform)
        page.screenshot(path=args.shot.replace(".png", "-after.png"), full_page=True)
        ctx.close()
        res = {**base, "matches_after": left, "inventory": [before, after]}

        # BOTH halves must hold: the target is gone AND the listing shrank by
        # exactly one. Target-gone alone cannot see collateral damage, and a
        # count drop alone cannot tell which item went.
        if left != 0:
            print(json.dumps({**res, "status": "still_present"}, ensure_ascii=False))
            return 6
        if before is None or after is None:
            print(json.dumps({**res, "status": "deleted_unverified",
                              "why": "could not read the listing size; confirm by hand"},
                             ensure_ascii=False))
            return 8
        if after != before - 1:
            print(json.dumps({**res, "status": "collateral_suspected",
                              "why": f"listing went {before} -> {after}, expected "
                                     f"{before - 1}; something else changed too"},
                             ensure_ascii=False))
            return 9
        print(json.dumps({**res, "status": "deleted"}, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
