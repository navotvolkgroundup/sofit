#!/usr/bin/env python3
"""Schedule a full video episode on Spotify for Creators (the show's host).

Same contract as the other uploaders, over the same ws-scraper Chrome profile:
`--dry` uploads the file, fills the wizard and screenshots WITHOUT publishing;
only `--submit` clicks Schedule/Publish.

    upload_spotify.py --login                      # one-time headed Spotify login
    upload_spotify.py --clip episode --plan plan.json --dry
    upload_spotify.py --clip episode --plan plan.json --submit

Plan keys on the post: "spotify_title", "spotify_description" (hashtags go in
the description - Spotify has no per-episode tags field), "date"; plan
"time_local" and "episode" (WS214 -> episode number 214). The video is
{clips_dir}/{episode}_episode.mp4, the same file YouTube gets.

--submit is refused unless seo.check passes and the title, description and
schedule all read back from the page.

ponytail: selectors are written against the wizard as last seen (labels
Title/Description/Episode number, Schedule option, Next/Publish footer) and
were NOT probed - the profile had no Spotify login when this was written.
First --dry after --login is the probe; fix whatever the readback flags.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import seo

PROFILE = Path.home() / ".ws-scraper" / "profile"
DASH = "https://creators.spotify.com/pod/dashboard/episodes"
WIZARD = "https://creators.spotify.com/pod/dashboard/episode/wizard"


def _cfg() -> dict:
    p = Path.home() / ".sofit" / "publish.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CLIPS_DIR = Path(_cfg().get("clips_dir", "~/Downloads")).expanduser()


def episode_number(episode: str) -> str:
    """'WS214' -> '214'; '' when the plan's episode has no number."""
    m = re.search(r"(\d+)$", episode or "")
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true",
                    help="open a headed browser for one-time Spotify login")
    ap.add_argument("--clip", default="episode")
    ap.add_argument("--plan")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry", action="store_true", help="fill + screenshot, no publish")
    g.add_argument("--submit", action="store_true", help="actually schedule")
    ap.add_argument("--shot", default="/tmp/spotify-upload-preview.png")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright  # lazy: tests import this file
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=not args.login,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/142.0.0.0 Safari/537.36")
        page = ctx.new_page()

        if args.login:
            page.goto(DASH, timeout=60_000)
            print("log in to the show's Spotify for Creators account in the "
                  "opened window; waiting (up to 10 min)...")
            for _ in range(120):
                page.wait_for_timeout(5_000)
                if "creators.spotify.com" in page.url and "login" not in page.url:
                    page.wait_for_timeout(3_000)
                    print("logged in - session saved to the profile")
                    ctx.close()
                    return 0
            print("login not completed", file=sys.stderr)
            ctx.close()
            return 1

        if not (args.plan and (args.dry or args.submit)):
            print("need --plan and one of --dry/--submit", file=sys.stderr)
            return 2
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        post = next((x for x in plan["posts"] if x["clip"] == args.clip), None)
        if not post or not post.get("spotify_title"):
            print(f"error: {args.clip} not in plan or has no 'spotify_title'",
                  file=sys.stderr)
            return 2
        title = " ".join(post["spotify_title"].split())
        desc = post.get("spotify_description", "")
        problems = seo.check("spotify", title, desc)
        age = seo.playbook_age_days()
        if age is None or age > seo.MAX_AGE_DAYS:
            print("warn: SEO playbook missing or older than a month - "
                  "run /sofit-seo refresh first", file=sys.stderr)
        if problems:
            print(f"seo: {'; '.join(problems)}", file=sys.stderr)
            if args.submit:
                print(json.dumps({"status": "seo_refused", "problems": problems},
                                 ensure_ascii=False))
                ctx.close()
                return 7
        video = CLIPS_DIR / f"{plan['episode']}_{args.clip}.mp4"
        if not video.exists():
            print(f"error: {video} not found", file=sys.stderr)
            ctx.close()
            return 2

        page.goto(WIZARD, timeout=60_000)
        page.wait_for_timeout(5_000)
        if "accounts.spotify.com" in page.url:
            print(json.dumps({"status": "logged_out",
                              "fix": "run upload_spotify.py --login once"}))
            ctx.close()
            return 5

        page.set_input_files("input[type=file]", str(video), timeout=30_000)
        title_box = page.locator("input[name='title'], #title-input").first
        title_box.wait_for(timeout=120_000)

        # fill()/insert_text, never char typing: typing scrambles Hebrew.
        title_box.fill(title)
        desc_box = page.locator(
            "[name='description'] [contenteditable='true'], "
            "div[contenteditable='true'][role='textbox'], textarea[name='description']"
        ).first
        desc_box.click(timeout=15_000)
        page.keyboard.insert_text(desc)

        num = episode_number(plan["episode"])
        if num:
            try:
                page.get_by_label(re.compile("Episode number", re.I)).first.fill(
                    num, timeout=5_000)
            except Exception as e:  # noqa: BLE001
                print(f"warn: episode number not set ({str(e)[:60]})", file=sys.stderr)

        # Schedule: the publish-date choice, then date + time inputs.
        when = datetime.strptime(f"{post['date']} {plan['time_local']}", "%Y-%m-%d %H:%M")
        sched_ok = False
        try:
            page.get_by_text(re.compile(r"^Schedule$")).first.click(timeout=10_000)
            page.wait_for_timeout(800)
            page.locator("input[type='date'], input[name*='date' i]").first.fill(
                when.strftime("%m/%d/%Y"))
            page.locator("input[type='time'], input[name*='time' i]").first.fill(
                when.strftime("%I:%M %p"))
            page.keyboard.press("Tab")
            sched_ok = True
        except Exception as e:  # noqa: BLE001
            print(f"warn: schedule not set ({str(e)[:80]})", file=sys.stderr)

        # Readback before anything is published.
        got_title = title_box.input_value()
        got_desc = desc_box.inner_text() if desc_box.count() else ""
        flat = lambda s: " ".join(s.split())  # noqa: E731
        checks = {
            "title": got_title.strip() == title,
            "description": flat(desc)[:30] in flat(got_desc)
                           and seo.hashtags(got_desc) == seo.hashtags(desc),
            "schedule": sched_ok,
        }
        bad = [k for k, ok in checks.items() if not ok]
        page.screenshot(path=args.shot, full_page=True)
        if args.dry:
            print(json.dumps({"status": "dry_ok" if not bad else "dry_readback_failed",
                              "failed": bad, "screenshot": args.shot,
                              "seo_problems": problems,
                              "when": when.isoformat(timespec="minutes")},
                             ensure_ascii=False))
            ctx.close()
            return 0
        if bad:
            print(json.dumps({"status": "readback_refused", "failed": bad,
                              "screenshot": args.shot}))
            ctx.close()
            return 6

        # Next -> review -> Schedule/Publish. The final button stays disabled
        # while the video processes, so keep clicking until it lands.
        committed = False
        for _ in range(90):  # up to ~22 min for a full-length video
            page.evaluate(
                """() => { const vis=e=>!!(e.offsetWidth||e.offsetHeight);
                   const b=[...document.querySelectorAll('button')].filter(vis)
                     .find(x=>['Next','Schedule','Publish'].includes(x.innerText.trim()));
                   if(b && !b.disabled) b.click(); }""")
            page.wait_for_timeout(15_000)
            body = page.inner_text("body")
            if re.search(r"(?i)episode (is )?(scheduled|published)|scheduled for", body):
                committed = True
                break
        page.screenshot(path=args.shot, full_page=False)
        if not committed:
            print(json.dumps({"status": "save_not_confirmed",
                              "screenshot": args.shot}))
            ctx.close()
            return 6
        link = page.locator("a[href*='open.spotify.com/episode/']")
        url = link.first.get_attribute("href") if link.count() else None
        import autolog
        out = {"status": "submitted", "clip": args.clip,
               "when": when.isoformat(timespec="minutes"), "post_url": url}
        out.update(autolog.log(args.plan, args.clip, "spotify", url))
        print(json.dumps(out, ensure_ascii=False))
        ctx.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
