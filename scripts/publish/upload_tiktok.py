#!/usr/bin/env python3
"""Semi-automated TikTok upload+schedule (M2 of the publish loop).

Fills TikTok Studio's upload form (file, caption, schedule date/time) through
the ws-scraper logged-in Chrome profile, then STOPS: `--dry` saves a
confirmation screenshot and exits WITHOUT posting. Only `--submit` clicks the
Schedule button - the human-confirm gate is the design, not a courtesy.

    upload_tiktok.py --clip clip-12 --plan publish-plan.json --dry
    upload_tiktok.py --clip clip-12 --plan publish-plan.json --submit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--plan", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry", action="store_true", help="fill + screenshot, no post")
    g.add_argument("--submit", action="store_true", help="actually click Schedule")
    ap.add_argument("--shot", default="/tmp/tiktok-upload-preview.png")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    post = next((p for p in plan["posts"] if p["clip"] == args.clip), None)
    if not post:
        print(f"error: {args.clip} not in plan", file=sys.stderr)
        return 1
    video = CLIPS_DIR / f"{plan['episode']}_{args.clip}.mp4"
    if not video.exists():
        print(f"error: {video} not found", file=sys.stderr)
        return 1
    hh, mm = plan["time_local"].split(":")
    yyyy, mo, dd = post["date"].split("-")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1500, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        if "/login" in page.url:
            print(json.dumps({"status": "session_dead"}))
            return 2

        # A previous abandoned run leaves a "Continue editing?" modal that
        # hides the file input - always start fresh. Discarding opens a
        # SECOND confirm ("Discard this post?"), so keep clicking Discard
        # until no such button remains.
        for _ in range(3):
            try:
                disc = page.get_by_role("button", name="Discard")
                if not disc.count():
                    break
                disc.first.click(timeout=3_000)
                page.wait_for_timeout(1_500)
            except Exception:  # noqa: BLE001
                break
        page.set_input_files("input[type=file]", str(video))
        # Wait for upload+processing (caption editor appears when ready).
        page.wait_for_selector("div[contenteditable='true']", timeout=120_000)
        page.wait_for_timeout(8_000)

        def purge_tour():
            # First-visit tour popup ("Preview your video on your phone"):
            # dismiss via its own Got it button; DOM-removal fallback for any
            # re-mount (the overlay intercepts all pointer events otherwise).
            try:
                btn = page.get_by_role("button", name="Got it")
                if btn.count():
                    btn.first.click(timeout=3_000)
                    page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass
            page.evaluate(
                "document.querySelectorAll('#react-joyride-portal,"
                " .react-joyride__overlay, [data-test-id=overlay]')"
                ".forEach(e => e.remove())")
            page.wait_for_timeout(300)

        purge_tour()
        n_tags = 0          # set by set_caption; how many tags were committed

        def _split_tags(caption: str) -> tuple[str, list[str]]:
            """(body, hashtag tokens). Trailing hashtag-only lines come off the
            end so they can be typed rather than pasted."""
            lines = caption.split("\n")
            tags: list[str] = []
            while lines and lines[-1].strip().startswith("#"):
                tags = lines.pop().split() + tags
            return "\n".join(lines).rstrip(), tags

        def commit_tag(tag: str) -> bool:
            """Pick `tag` out of the '#' suggestion panel. Returns whether an
            EXACT match was clicked.

            The composer never decorates hashtags - probed 2026-09-25, Latin
            '#fyp' and Hebrew '#אוטומציה' both land in one undecorated span - so
            there is nothing to read back here. What the probe DID show is that
            the suggestion panel opens, which is the only affordance for
            actually attaching a tag; dismissing it with Escape (the earlier
            attempt) posts the text and no tag. Only an exact match is clicked:
            a near-miss would silently swap in a different, wrong hashtag."""
            want = tag.lstrip("#").strip()
            try:
                # The panel is .mention-list-popover and its rows carry NO class
                # of their own (dumped 2026-09-25), so rows are found by shape:
                # the smallest descendant whose text is "#tag" + a post count.
                row = page.evaluate_handle("""(want) => {
                    const pop = document.querySelector('.mention-list-popover');
                    if (!pop) return null;
                    const hits = [...pop.querySelectorAll('*')].filter(e => {
                        const t = (e.innerText || '').trim();
                        return t.startsWith('#' + want) && /posts/i.test(t);
                    });
                    if (!hits.length) return null;
                    hits.sort((a, b) => a.innerText.length - b.innerText.length);
                    const first = hits[0].innerText.trim().split('\\n')[0].trim();
                    return first === '#' + want ? hits[0] : null;
                }""", want).as_element()
                if row:
                    row.click(timeout=2_000)
                    page.wait_for_timeout(600)
                    return True
            except Exception:  # noqa: BLE001
                pass
            page.keyboard.press("Escape")
            return False

        # Caption: clear the auto-filled filename, insert ours ATOMICALLY
        # (char-by-char typing scrambles mixed-RTL text), then read back.
        cap = page.locator("div[contenteditable='true']").first
        want_head = post["tiktok"].strip().split()[0]

        def caption_ok() -> bool:
            got = cap.inner_text().strip().replace("\n", " ")
            return want_head in got.split()[0:3]

        def set_caption(method: str) -> None:
            purge_tour()
            # DraftJS ignores synthetic clicks (activeElement stays BODY);
            # element.focus() via JS is the reliable way in.
            page.evaluate(
                "document.querySelector('div.public-DraftEditor-content').focus()")
            page.wait_for_timeout(400)
            page.keyboard.press("Meta+A")
            page.wait_for_timeout(200)
            page.keyboard.press("Backspace")
            page.wait_for_timeout(400)
            body, tags = _split_tags(post["tiktok"])
            if method == "insert":
                page.keyboard.insert_text(body)
            else:  # slow typing fallback
                page.keyboard.type(body, delay=40)
            page.wait_for_timeout(1_200)
            # Hashtags have to be TYPED. insert_text lands as a paste, so DraftJS
            # never fires its '#' handler and the tag stays plain text - verified
            # 2026-09-25 on two live posts, whose tags sat in a bare <span> while
            # another creator's Hebrew tags were real /tag/ links. Safe to type:
            # the RTL scrambling that forced insert_text applies to the mixed
            # body, not to a single unembedded token.
            nonlocal n_tags
            n_tags = 0
            for tag in tags:
                page.keyboard.type("\n\n" if tag is tags[0] else " ")
                page.keyboard.type(tag, delay=60)
                # Flaky: the same clip scored 3/3 then 0/3 an hour later, and a
                # batch came out 10/15. A retype-and-retry loop made it WORSE
                # (0/3 every time) - the Escape that ends a failed pick leaves
                # the editor in a state retyping does not recover. Left as the
                # simple wait until someone can watch this headful.
                page.wait_for_timeout(1_200)    # let the suggestion panel settle
                n_tags += commit_tag(tag)
                page.wait_for_timeout(300)
            page.wait_for_timeout(800)

        set_caption("insert")
        if not caption_ok():
            set_caption("type")
        if not caption_ok():
            page.screenshot(path=args.shot)
            print(json.dumps({"status": "caption_mismatch",
                              "got": cap.inner_text()[:120]}, ensure_ascii=False))
            ctx.close()
            return 3

        # Report, don't fail on it: a caption with dead hashtags is still worth
        # posting, but we want to SEE it rather than find out months later.
        # NOTE this counts suggestion-panel picks, not a readback - the composer
        # gives no signal. Confirm on the LIVE post (its tags should be
        # <a href="/tag/...">), not from a dry run.
        n_want = len(_split_tags(post["tiktok"])[1])
        if n_tags < n_want:
            print(f"warn: only {n_tags}/{n_want} hashtags committed from the "
                  "suggestion panel", file=sys.stderr)

        # Schedule: pick the radio, then fill date+time inputs.
        purge_tour()

        def pick_schedule() -> bool:
            for attempt in ("radio", "text", "js"):
                try:
                    if attempt == "radio":
                        page.get_by_role("radio", name="Schedule").check(force=True, timeout=4_000)
                    elif attempt == "text":
                        page.get_by_text("Schedule", exact=True).first.click(
                            force=True, timeout=4_000)
                    else:
                        page.evaluate(
                            """() => { const els=[...document.querySelectorAll('*')]
                            .filter(e=>e.children.length===0&&e.textContent.trim()==='Schedule'
                                    &&e.offsetParent!==null);
                            if(els.length) els[els.length-1].click();}""")
                    page.wait_for_selector("input[value*='-']", timeout=6_000)
                    return True
                except Exception:  # noqa: BLE001
                    purge_tour()
            return False

        if not pick_schedule():
            page.screenshot(path=args.shot)
            print(json.dumps({"status": "schedule_toggle_failed",
                              "screenshot": args.shot}))
            ctx.close()
            return 4
        page.wait_for_timeout(1_500)

        def js_click_exact(txt: str) -> bool:
            # Click the LAST visible leaf element whose text is exactly txt -
            # picker dropdowns render after (below) the static page content.
            return page.evaluate(
                """(txt) => {
                  const els = [...document.querySelectorAll('*')].filter(e =>
                    e.children.length === 0 &&
                    e.textContent.trim() === txt &&
                    e.offsetParent !== null);
                  if (!els.length) return false;
                  els[els.length - 1].click();
                  return true; }""", txt)

        # Date: open the picker, click the exact valid day cell.
        # ponytail: current-month view only (plan dates are within it); the
        # value verification below catches any miss - add month-nav chevrons
        # if a plan ever crosses a month boundary.
        purge_tour()
        date_in = page.locator("input[value*='-']").first
        date_in.click(force=True)
        page.wait_for_selector(".days-wrapper", timeout=8_000)
        import re as _re
        page.locator(".days-wrapper span.day.valid").filter(
            has_text=_re.compile(rf"^\s*{int(dd)}\s*$")).first.click(force=True)
        page.wait_for_timeout(800)

        # Time: open the dropdown, click hour column then minute column.
        purge_tour()
        time_in = page.locator("input[value*=':']").first
        time_in.click(force=True)
        page.wait_for_timeout(1_200)

        def set_time_part(txt: str, part: int) -> bool:
            # The hour and minute columns can both contain `txt` (e.g. "20"),
            # so a blind last-match click can hit the wrong column. Click
            # candidates from the last match backwards and verify the input
            # actually changed the right part.
            for nth_from_end in (1, 2, 3):
                # The hour column is a SCROLLABLE 00-23 list: "13" sits well
                # below the fold, and a click on an off-view row is swallowed -
                # the field then keeps the 00:00 default (2026-09-03: clip-8
                # shipped scheduled for midnight). Scroll the candidate into
                # view first, then click it.
                page.evaluate(
                    """(a) => { const els=[...document.querySelectorAll('*')]
                        .filter(e=>e.children.length===0 &&
                                e.textContent.trim()===a.txt &&
                                e.offsetParent!==null);
                        const el=els[els.length-a.n];
                        if(el) el.scrollIntoView({block:'center'}); }""",
                    {"txt": txt, "n": nth_from_end})
                page.wait_for_timeout(350)
                page.evaluate(
                    """(a) => { const els=[...document.querySelectorAll('*')]
                        .filter(e=>e.children.length===0 &&
                                e.textContent.trim()===a.txt &&
                                e.offsetParent!==null);
                        const el=els[els.length-a.n];
                        if(el) el.click(); }""",
                    {"txt": txt, "n": nth_from_end})
                page.wait_for_timeout(400)
                if time_in.input_value().split(":")[part] == txt:
                    return True
            return False

        if not set_time_part(hh, 0):
            print("warn: hour not set", file=sys.stderr)
        if not set_time_part(mm, 1):
            print("warn: minute not set", file=sys.stderr)

        # Clicking a row in the scrollable hour column is FLAKY - it silently
        # leaves the 00:00 default (2026-09-03: two posts in one batch, one of
        # which shipped scheduled for midnight). The field is readonly so it
        # cannot be typed into; reopening the picker and retrying is what
        # actually recovers.
        for attempt in range(2, 5):
            if time_in.input_value() == plan["time_local"]:
                break
            print(f"info: time reads {time_in.input_value()}, reopening the "
                  f"picker (attempt {attempt})", file=sys.stderr)
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            purge_tour()
            time_in.click(force=True)
            page.wait_for_timeout(1_400)
            set_time_part(hh, 0)
            set_time_part(mm, 1)
            page.wait_for_timeout(600)
        page.wait_for_timeout(800)
        page.keyboard.press("Escape")  # close pickers so the shot shows values
        page.wait_for_timeout(700)

        # Verify what the fields actually hold before anyone clicks Schedule.
        sched_date = page.locator("input[value*='-']").first.input_value()
        sched_time = page.locator("input[value*=':']").first.input_value()
        # A readback that disagrees with the plan is a HARD STOP, not a warning.
        # The time columns collide (picking 13:00 can land on 00:00 when the
        # click hits the minutes column) and on 2026-09-03 that shipped a post
        # scheduled for midnight while the run still reported "submitted" -
        # scheduled TikTok posts cannot be edited, so the only fix is delete +
        # re-upload. Refuse to post rather than post at the wrong time.
        if sched_date != post["date"] or sched_time != plan["time_local"]:
            page.screenshot(path=args.shot.replace(".png", "-timefail.png"))
            ctx.close()
            print(json.dumps({"status": "time_not_set", "clip": args.clip,
                              "wanted": f"{post['date']} {plan['time_local']}",
                              "shown": f"{sched_date} {sched_time}"},
                             ensure_ascii=False))
            return 4

        page.screenshot(path=args.shot, full_page=False)
        if args.dry:
            ctx.close()
            print(json.dumps({"status": "dry_ok", "screenshot": args.shot,
                              "clip": args.clip, "scheduled_date": sched_date,
                              "scheduled_time": sched_time,
                              "tags_registered": n_tags, "tags_wanted": n_want}))
            return 0

        purge_tour()
        page.get_by_role("button", name="Schedule").click(force=True)
        page.wait_for_timeout(4_000)
        # "We're still checking your video... continue?" - confirm; the
        # content check keeps running server-side after scheduling.
        try:
            pn = page.get_by_role("button", name="Post now")
            if pn.count():
                pn.first.click(timeout=4_000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(8_000)

        # Verify against the source of truth: the content list must show a
        # new first row for this clip; its href is the post URL for the log.
        page.goto("https://www.tiktok.com/tiktokstudio/content",
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        # Find the row whose caption matches THIS clip. The list is sorted by
        # SCHEDULED date, not creation, so a post scheduled earlier than the
        # ones already queued is not first - taking .first returned another
        # clip's URL and logged it against this one (2026-08-27).
        post_url = page.evaluate(
            """(head) => {
              for (const a of document.querySelectorAll("a[href*='/video/']")) {
                let r=a;
                for (let i=0;i<6&&r;i++){ r=r.parentElement;
                  if (r && r.innerText && r.innerText.length>60) break; }
                if (r && r.innerText.includes(head)) return a.href;
              }
              return "";
            }""", want_head)
        row_ok = bool(post_url)
        if not post_url:  # keep the old behaviour as a last resort
            first = page.locator("a[href*='/video/']").first
            post_url = first.get_attribute("href") or ""
        page.screenshot(path=args.shot.replace(".png", "-after.png"))
        ctx.close()
        if post_url.startswith("/"):
            post_url = "https://www.tiktok.com" + post_url
        import autolog
        out = {"status": "submitted" if row_ok else "submitted_unverified",
               "clip": args.clip, "date": post["date"], "post_url": post_url,
               "tags_registered": n_tags, "tags_wanted": n_want}
        # Log here, not in a later step: a batch once reported 20/20 submitted
        # and logged none of it (2026-09-17).
        out.update(autolog.log(args.plan, args.clip, "tiktok", post_url))
        print(json.dumps(out, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
