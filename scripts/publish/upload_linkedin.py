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

A schedule counts as set only when the composer line reads
"Posting <Ddd>, <Mon> <d> at <h:mm AM/PM>". `--dry` still does not post;
`--submit` is refused when that line is missing or the primary button
still says Post.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

import showhome

PROFILE = showhome.browser_profile()


def _cfg() -> dict:
    p = showhome.home() / "publish.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CLIPS_DIR = Path(_cfg().get("clips_dir", "~/Downloads")).expanduser()
ADMIN = "https://www.linkedin.com/company/{cid}/admin/dashboard/"

# The composer is English. strftime('%a')/'%b' follow the process locale.
_EN_WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_EN_MONTH = (
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def linkedin_time_label(time_local: str) -> str:
    """'20:00' -> '8:00 PM', the 12-hour string the schedule box displays."""
    hour, minute = (int(part) for part in time_local.split(":"))
    return f"{hour % 12 or 12}:{minute:02d} {'PM' if hour >= 12 else 'AM'}"


def linkedin_posting_line(date_iso: str, time_local: str) -> str:
    """Composer readback, e.g. 'Posting Sun, Nov 1 at 8:00 PM'."""
    day = datetime.date.fromisoformat(date_iso)
    return (f"Posting {_EN_WEEKDAY[day.weekday()]}, {_EN_MONTH[day.month]} "
            f"{day.day} at {linkedin_time_label(time_local)}")


def linkedin_schedule_matches(got_line: str, date_iso: str, time_local: str) -> bool:
    return " ".join((got_line or "").split()) == linkedin_posting_line(date_iso, time_local)


def linkedin_stop(*, dry: bool, scheduled: bool,
                  primary: str | None) -> tuple[str, int] | None:
    """(status, exit code) when the run must stop, else None to click the button.

    --dry never posts. A schedule that did not stick, or a primary button that
    still says Post, is refused: that click would publish immediately.
    """
    if dry:
        return ("dry_ok" if scheduled else "dry_schedule_failed"), 0
    if not scheduled or (primary or "").lower().startswith("post"):
        return "refused_would_post_now", 4
    return None


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

        # 2026-10-22: a "Premium Company Page" upsell modal covers the dashboard - dismiss it.
        for sel in ("button[aria-label='Dismiss']", "[role=dialog] button[aria-label*='Close']", "button:has-text('Not now')"):
            try:
                page.locator(sel).first.click(timeout=3_000); page.wait_for_timeout(1_500); break
            except Exception:  # noqa: BLE001
                continue
        page.keyboard.press("Escape"); page.wait_for_timeout(1_000)
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
            # 2026-10-22: page.fill() on the time box showed "8:00 PM" in the
            # dialog but the composer then said "Posting ... at 5:15 PM" - the
            # value never reached the component state. TYPE it (12h, the
            # format the box displays), pick the matching dropdown option if
            # one shows, commit with Tab, and read the composer back below.
            want_t = linkedin_time_label(plan["time_local"])
            t_in = page.locator("[role=dialog] input[name='time'], [role=dialog] input[id*='time']").first
            for _try in range(3):
                t_in.click(timeout=8_000)
                page.keyboard.press("Control+A"); page.keyboard.press("Backspace")
                page.keyboard.type(want_t, delay=60)
                page.wait_for_timeout(900)
                opt = page.locator("[role=dialog] [role=option], [role=listbox] li, .artdeco-typeahead__result")
                picked = False
                for i in range(min(opt.count(), 120)):
                    o = opt.nth(i)
                    try:
                        if o.is_visible() and o.inner_text().strip() == want_t:
                            o.click(timeout=3_000); picked = True; break
                    except Exception:  # noqa: BLE001
                        pass
                if not picked:
                    page.keyboard.press("Enter")
                page.keyboard.press("Tab")
                page.wait_for_timeout(700)
                if t_in.input_value().strip().upper() == want_t:
                    break
            print(f"info: time box reads {t_in.input_value()!r} (picked={picked})", file=sys.stderr)
            page.evaluate("""() => { const b=[...document.querySelectorAll('[role=dialog] button')].filter(x=>x.innerText.trim()==='Next' && x.offsetParent!==null); if(b.length) b[b.length-1].click(); }""")
            page.wait_for_timeout(4_000)
            # Back in the composer, or the readbacks below measure nothing.
            if page.locator("[role=dialog] input[id*='date']").count():
                raise RuntimeError("schedule dialog still open after Next")
            # Ground truth is the composer line, e.g. "Posting Sun, Nov 1 at 8:00 PM".
            want_line = linkedin_posting_line(post["date"], plan["time_local"])
            got_line = page.evaluate(r"""() => { const m=(document.querySelector('[role=dialog]')||document.body).innerText.match(/Posting [A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2} at \d{1,2}:\d{2}\s?[AP]M/); return m?m[0].replace(/\s+/g,' '):''; }""")
            print(f"info: composer says {got_line!r}", file=sys.stderr)
            if not linkedin_schedule_matches(got_line, post["date"], plan["time_local"]):
                raise RuntimeError(f"schedule readback mismatch: want {want_line!r} got {got_line!r}")
            scheduled = True
        except Exception as e:  # noqa: BLE001
            print(f"warn: scheduling failed ({str(e)[:180]})", file=sys.stderr)

        # What the primary button says is the ground truth for what a click
        # would do. Read it back rather than assuming the schedule took.
        primary = page.evaluate(
            "() => {const b=document.querySelector('.share-actions__primary-action');"
            " return b ? b.innerText.trim() : null;}")
        has_video = page.evaluate(
            "() => !!document.querySelector('[role=dialog] video, "
            "[role=dialog] [class*=video-preview], [role=dialog] img[src^=blob]')")
        page.screenshot(path=args.shot, full_page=False)

        posting_line = page.evaluate(r"""() => { const m=(document.querySelector('[role=dialog]')||document.body).innerText.match(/Posting [^\n]*? at \d{1,2}:\d{2}\s?[AP]M/); return m?m[0]:''; }""")
        state = {"clip": args.clip, "date": post["date"], "posting_line": posting_line,
                 "time": plan["time_local"], "scheduled": scheduled,
                 "primary_button": primary, "video_attached": has_video,
                 "chars": len(text), "screenshot": args.shot}

        stop = linkedin_stop(dry=args.dry, scheduled=scheduled, primary=primary)
        if stop is not None:
            status, code = stop
            print(json.dumps({"status": status, **state}, ensure_ascii=False))
            ctx.close()
            return code

        page.locator(".share-actions__primary-action").first.click(timeout=10_000)
        page.wait_for_timeout(6_000)
        print(json.dumps({"status": "submitted", **state}, ensure_ascii=False))
        ctx.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
