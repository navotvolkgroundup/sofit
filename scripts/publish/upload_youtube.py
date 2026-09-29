#!/usr/bin/env python3
"""Schedule a YouTube Short through YouTube Studio's upload flow.

Fills the whole form (file, title, description, not-for-kids, schedule
date/time) through the ws-scraper logged-in Chrome profile, then STOPS:
`--dry` saves a screenshot and exits without publishing; `--submit` clicks
Done and returns the video URL from the confirmation dialog.

A vertical video under 3 minutes is a Short automatically - no special flag.

    upload_youtube.py --login                     # one-time headed Google login
    upload_youtube.py --clip clip-2 --plan publish-plan.json --dry
    upload_youtube.py --clip clip-2 --plan publish-plan.json --submit

Plan JSON: the post's "youtube" key is the Short's title (caption surface on
Shorts); optional "youtube_description" fills the description box.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".ws-scraper" / "profile"

def _publish_cfg() -> dict:
    """Machine-local account config (~/.sofit/publish.json) - the code is
    public OSS, the account details are not. IMPORT-SAFE by design: a missing
    file returns {} with a warning (CI collects these modules on machines
    with no config); each script validates the keys it needs at RUN time."""
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path
    p = _Path.home() / ".sofit" / "publish.json"
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"warn: missing/invalid {p} - running with defaults "
              '({"ig_profile", "ig_collaborators", "clips_dir", "plans_dir"})',
              file=_sys.stderr)
        return {}

_CFG = _publish_cfg()
CLIPS_DIR = Path(_CFG.get("clips_dir", "~/Downloads")).expanduser()


def _ampm(hhmm: str) -> str:
    """'20:30' -> '8:30 PM' (YouTube's time-of-day list format)."""
    return datetime.strptime(hhmm, "%H:%M").strftime("%-I:%M %p")


def _date_label(iso: str) -> str:
    """'2026-08-23' -> 'Aug 23, 2026' (datepicker input format)."""
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d, %Y")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true",
                    help="open a headed browser for one-time Google login")
    ap.add_argument("--clip")
    ap.add_argument("--plan")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry", action="store_true", help="fill + screenshot, no publish")
    g.add_argument("--submit", action="store_true", help="actually click Done")
    ap.add_argument("--shot", default="/tmp/youtube-upload-preview.png")
    args = ap.parse_args()

    with sync_playwright() as p:
        # AutomationControlled off or Google bot-gates the login flow
        # ("this browser may not be secure") - same lesson as scrape.py.
        # Explicit UA: headless real-Chrome advertises "HeadlessChrome" and
        # YT Studio hard-blocks it with an unsupported-browser wall.
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=not args.login,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/142.0.0.0 Safari/537.36")
        page = ctx.new_page()

        if args.login:
            page.goto("https://studio.youtube.com", timeout=60_000)
            print("log in to the channel's Google account in the opened window;"
                  " waiting (up to 10 min)...")
            # Verify by session cookie, not URL - Google can land anywhere
            # after login (youtube.com, myaccount, channel picker).
            for i in range(120):
                page.wait_for_timeout(5_000)
                if any(c["name"] == "SID" and c.get("value")
                       for c in ctx.cookies("https://www.google.com")):
                    page.wait_for_timeout(3_000)
                    print("logged in - session cookie saved to the profile")
                    ctx.close()
                    return 0
                if i % 6 == 5:
                    print(f"  ...still waiting ({(i+1)*5}s), no session cookie yet")
            print("login not completed - no Google session cookie appeared",
                  file=sys.stderr)
            ctx.close()
            return 1

        if not (args.clip and args.plan and (args.dry or args.submit)):
            print("need --clip, --plan and one of --dry/--submit", file=sys.stderr)
            return 2

        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        post = next((x for x in plan["posts"] if x["clip"] == args.clip), None)
        if not post or not post.get("youtube"):
            print(f"error: {args.clip} not in plan or has no 'youtube' caption",
                  file=sys.stderr)
            return 2
        video = CLIPS_DIR / f"{plan['episode']}_{args.clip}.mp4"
        if not video.exists():
            print(f"error: {video} not found", file=sys.stderr)
            return 2
        title = " ".join(post["youtube"].split())[:100]
        desc = post.get("youtube_description", "")

        page.goto("https://www.youtube.com/upload", timeout=60_000)
        page.wait_for_timeout(4_000)
        if "accounts.google.com" in page.url:
            print(json.dumps({"status": "logged_out",
                              "fix": "run upload_youtube.py --login once"}))
            ctx.close()
            return 5

        page.set_input_files("input[type=file]", str(video), timeout=30_000)
        # Details step renders while the file uploads in the background.
        tb = page.locator("ytcp-video-metadata-editor #textbox")
        tb.first.wait_for(timeout=60_000)
        page.wait_for_timeout(2_000)

        # Title/description are contenteditables; insert_text keeps RTL intact
        # (char-typing is what scrambles Hebrew, same lesson as TikTok).
        tb.first.click()
        page.keyboard.press("Meta+A")
        page.keyboard.insert_text(title)
        if desc:
            tb.nth(1).click()
            page.keyboard.insert_text(desc)

        # Tags live behind "Show more" and were never filled - the pipeline has
        # generated them since 2026-09-24 and they went nowhere (Navot,
        # 2026-09-29). Best effort: a missing tags box must not sink the upload.
        tags = post.get("youtube_tags", "")
        if tags:
            try:
                # #toggle-button matched a DIFFERENT collapsed section, so the
                # click "worked" and the tags stayed hidden - the timeout that
                # followed looked like a bad tags selector but never was
                # (probed on a real draft, 2026-09-29). Match the button by its
                # own text; ytcp-button has no ARIA role, so get_by_role misses it.
                page.evaluate("""() => {
                  const b = [...document.querySelectorAll(
                      'ytcp-button,tp-yt-paper-button,button')]
                    .find(e => e.offsetParent && (e.innerText||'').trim() === 'Show more');
                  if (b) b.click();
                }""")
                page.wait_for_timeout(2_500)
                box = page.locator("input#text-input[aria-label='Tags']").first
                box.click(timeout=8_000)
                # A chip input commits a tag on each comma KEYSTROKE, so a paste
                # leaves the counter at 0/500 - the same reason TikTok hashtags
                # came out dead. type() is slower and is the only thing that works.
                box.type(tags[:495], delay=2)
                page.wait_for_timeout(1_500)
                # The counter is the only honest readback: it reflects committed
                # chips, not what we typed.
                got = box.evaluate(
                    "el => el.closest('ytcp-form-input-container').innerText")
                used = re.search(r"(\d+)\s*/\s*500", got)
                n = int(used.group(1)) if used else 0
                print(f"tags: {n}/500 chars committed"
                      f"{'' if n > 100 else '  <-- EMPTY, fill them by hand'}",
                      file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"warn: tags not set ({str(e)[:80]})", file=sys.stderr)

        page.locator(
            "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']"
        ).click(timeout=15_000)

        for _ in range(3):  # ad suitability / video elements / checks
            page.locator("#next-button").click(timeout=15_000)
            page.wait_for_timeout(1_200)

        # "Schedule" is an expander section on the Visibility step (not a
        # radio): open it, then the date/time triggers render inside.
        exp = page.locator("#second-container-expand-button")
        if not exp.count():
            exp = page.locator("ytcp-video-visibility-select, ytcp-uploads-review"
                               ).get_by_text("Schedule", exact=True)
        exp.first.click(timeout=15_000)
        page.wait_for_timeout(1_000)
        page.locator("#datepicker-trigger").click()
        di = page.locator("tp-yt-paper-input#input-1 input, ytcp-date-picker input")
        di.first.fill(_date_label(post["date"]))
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        want_time = _ampm(plan["time_local"])
        tin = page.locator("#time-of-day-container input").first
        tin.click()
        page.wait_for_timeout(600)
        page.keyboard.press("Meta+A")
        page.keyboard.insert_text(want_time)
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)

        # Readback before anyone publishes anything. YT renders times with a
        # narrow no-break space (U+202F) before AM/PM - normalize it.
        def _norm(s: str) -> str:
            return s.replace(" ", " ")
        vis_text = _norm(page.locator("ytcp-uploads-review").inner_text())
        date_ok = _date_label(post["date"]).split(",")[0] in vis_text
        time_ok = want_time in vis_text or _norm(tin.input_value()) == want_time
        if not (date_ok and time_ok):
            print(f"warn: review pane shows neither/partial "
                  f"'{_date_label(post['date'])} {want_time}'", file=sys.stderr)

        page.screenshot(path=args.shot, full_page=False)
        if args.dry:
            print(json.dumps({"status": "dry_ok", "screenshot": args.shot,
                              "clip": args.clip, "date": post["date"],
                              "time": want_time}, ensure_ascii=False))
            ctx.close()
            return 0

        # Committing the schedule (hard-won, 2026-08-21, three failed batches):
        # - #done-button matches a HIDDEN duplicate; clicking it does nothing
        #   while the dialog silently stays on the review step. Click the
        #   VISIBLE footer button by its text instead.
        # - Closing the browser before the "Video scheduled" panel appears
        #   loses the schedule and leaves a draft (the IG spinner lesson).
        # - A youtu.be link is NOT a commit signal - the wizard footer shows
        #   the permanent video link from the moment the upload starts.
        # So: click the visible Schedule/Save button, wait, re-click if the
        # panel hasn't appeared (the first click can land while the button
        # is still settling), and only report submitted on the panel itself.
        committed = False
        for _ in range(30):  # up to ~5 min
            page.evaluate(
                """() => { const d=document.querySelector('ytcp-uploads-dialog');
                   if(!d) return;
                   const vis=e=>!!(e.offsetWidth||e.offsetHeight);
                   const b=[...d.querySelectorAll('button')].filter(vis).find(
                     x=>['Schedule','Save','Publish'].includes(x.innerText.trim()));
                   if(b && !b.disabled) b.click(); }""")
            page.wait_for_timeout(10_000)
            if (page.get_by_text("Video scheduled").count()
                    or page.get_by_text("Video published").count()
                    or not page.locator("ytcp-uploads-dialog").count()):
                committed = True
                break
        link = page.locator("a[href*='youtu.be/'], a[href*='/shorts/']")
        url = link.first.get_attribute("href") if link.count() else None
        if not committed:
            page.screenshot(path=args.shot, full_page=False)
            print(json.dumps({"status": "save_not_confirmed", "clip": args.clip,
                              "screenshot": args.shot}, ensure_ascii=False))
            ctx.close()
            return 6
        # Dismiss the share panel with its VISIBLE Close button only - a
        # role-based lookup matches the wizard's hidden "Save and close".
        page.evaluate(
            """() => { const vis=e=>!!(e.offsetWidth||e.offsetHeight);
               const b=[...document.querySelectorAll('button')].filter(vis)
                 .find(x=>x.innerText.trim()==='Close');
               if(b) b.click(); }""")
        page.wait_for_timeout(1_500)
        page.screenshot(path=args.shot, full_page=False)
        import autolog
        out = {"status": "submitted", "clip": args.clip,
               "date": post["date"], "time": want_time, "post_url": url}
        out.update(autolog.log(args.plan, args.clip, "youtube", url))
        print(json.dumps(out, ensure_ascii=False))
        ctx.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
