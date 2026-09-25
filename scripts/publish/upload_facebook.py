#!/usr/bin/env python3
"""Publish or schedule a clip to the Facebook page via Meta Business Suite.

Same contract as the other three uploaders: --dry fills the whole composer and
screenshots WITHOUT publishing; only --submit commits. Plan key "facebook"
holds the caption, falling back to the Instagram one.

    upload_facebook.py --clip clip-4 --plan PLAN --dry|--submit

Captions go through sofit.format.rtl_caption: Facebook renders Hebrew in an LTR
container exactly like Instagram and TikTok, and an unmarked caption comes out
with its segments transposed (seen on the page's own first post, 2026-09-12).
"""
from __future__ import annotations

import argparse
import calendar

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
try:
    from sofit.format import rtl_caption
except Exception:  # noqa: BLE001 - keep the module import-safe for CI
    def rtl_caption(t):  # type: ignore[misc]
        return t

PROFILE = Path.home() / ".ws-scraper" / "profile"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
COMPOSER = "https://business.facebook.com/latest/composer"


def _cfg() -> dict:
    try:
        return json.loads((Path.home() / ".sofit" / "publish.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


CLIPS_DIR = Path(_cfg().get("clips_dir", "~/Downloads")).expanduser()


def want_time_in(listed: str, hh: str, mi: str) -> bool:
    """Is the slot's time visible in the scheduled-posts list? The list renders
    it as text, which is the only place it can actually be read back."""
    return any(f"{h}:{mi}" in listed for h in (hh, hh.lstrip("0"), f"{int(hh):02d}"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--plan", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry", action="store_true")
    g.add_argument("--submit", action="store_true")
    ap.add_argument("--shot", default="/tmp/fb-upload-preview.png")
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
    caption = rtl_caption(post.get("facebook") or post["instagram"])

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chrome", user_agent=UA,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1500, "height": 1050})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # Pin the destination. The bare composer inherits whatever asset Meta
        # used last, so the target silently follows the session - on 2026-09-25
        # a Weekly Sync clip filled the composer for an unrelated page. The
        # target is config, and it is CHECKED below before anything is typed.
        want_page = _cfg().get("fb_page_name")
        cfg_asset = _cfg().get("fb_asset_id")
        page.goto(f"{COMPOSER}?asset_id={cfg_asset}" if cfg_asset else COMPOSER,
                  wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(12_000)
        if want_page:
            shown = page.evaluate(
                "() => { const m = document.body.innerText.match("
                "/Share to[\\s\\S]{0,120}/); return m ? m[0] : ''; }")
            if want_page not in shown:
                print(json.dumps({"status": "wrong_destination",
                                  "want": want_page,
                                  "got": shown.replace("\n", " | ")[:120]},
                                 ensure_ascii=False))
                ctx.close()
                return 4
        # The composer redirects to a URL carrying the PAGE's asset_id. Keep it:
        # the listings are per-asset, and the bare /posts/scheduled_posts lands
        # on the personal profile, whose list is empty - which read back as
        # "nothing was scheduled" for seven posts that had scheduled fine.
        m = re.search(r"asset_id=(\d+)", page.url)
        asset_id = m.group(1) if m else None
        for name in ("Got it", "OK", "Close"):
            try:
                page.get_by_role("button", name=name).first.click(timeout=3_000)
                page.wait_for_timeout(1_000)
                break
            except Exception:  # noqa: BLE001
                continue

        # The composer defaults to posting to the Facebook page AND the linked
        # Instagram account. Left alone it would publish a SECOND Instagram post
        # on top of the reel already scheduled there. Turn Instagram off first.
        ig_handle = _cfg().get("ig_profile") or "weeklysyncpodcast"
        try:
            # open the target picker by its chevron: the summary text itself is
            # not clickable, and the surrounding elements are page-sized divs
            box = page.evaluate("""(h) => {
                const e = [...document.querySelectorAll('*')].find(x => {
                    const r = x.getBoundingClientRect();
                    return x.offsetParent !== null &&
                           (x.innerText || '').includes('and ' + h) &&
                           r.height > 28 && r.height < 80 && r.width > 280;
                });
                if (!e) return null;
                const r = e.getBoundingClientRect();
                return {x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height)};
            }""", ig_handle)
            if box:
                page.mouse.click(box["x"] + box["w"] - 22, box["y"] + box["h"] // 2)
                page.wait_for_timeout(3_000)
                found = page.evaluate("""(h) => {
                    const e = [...document.querySelectorAll(
                        '[role=checkbox],[role=option],[role=switch],[role=menuitemcheckbox]')]
                      .find(x => x.offsetParent !== null &&
                                 new RegExp(h).test(x.innerText || ''));
                    if (!e) return false;
                    e.setAttribute('data-sofit-ig', '1');
                    return true;
                }""", ig_handle)
                if found:
                    page.locator("[data-sofit-ig]").first.click(timeout=6_000)
                    page.wait_for_timeout(2_500)
                page.keyboard.press("Escape")
                page.wait_for_timeout(2_000)
        except Exception as e:  # noqa: BLE001
            print(f"warn: could not adjust targets ({str(e)[:60]})", file=sys.stderr)
        ig_off = page.evaluate("""() => {
            const row = [...document.querySelectorAll('*')].find(e =>
                e.offsetParent !== null &&
                /Post to/i.test((e.innerText || '')) &&
                (e.innerText || '').length < 300);
            return row ? row.innerText.replace(/\\s+/g, ' ').slice(0, 160) : null;
        }""")

        # video first: the composer re-renders around the attachment, and a
        # caption typed before it can be discarded
        try:
            with page.expect_file_chooser(timeout=10_000) as fc:
                page.get_by_text("Add photo/video", exact=False).first.click(timeout=8_000)
            fc.value.set_files(str(video))
        except Exception:  # noqa: BLE001
            page.locator("input[type=file]").first.set_input_files(str(video))
        # Wait for the upload to finish rather than guessing a duration: while
        # "Uploading media" is on screen the Publish button stays disabled.
        for _ in range(40):
            page.wait_for_timeout(5_000)
            if "Uploading media" not in page.inner_text("body"):
                break
        page.wait_for_timeout(3_000)

        body = page.locator("div[contenteditable='true']").first
        body.click(timeout=10_000)
        page.keyboard.insert_text(caption)
        page.wait_for_timeout(2_000)
        # typing "#" opens a hashtag autocomplete that swallows the next keys
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
        typed = body.inner_text(timeout=8_000)
        head = caption.replace("‫", "").strip().split("\n")[0][:24]
        if head and head not in typed:
            page.screenshot(path=args.shot, full_page=False)
            print(json.dumps({"status": "caption_mismatch", "clip": args.clip,
                              "wanted_head": head}, ensure_ascii=False))
            ctx.close()
            return 3

        # Schedule for the plan's slot instead of publishing now.
        sched = None
        try:
            page.get_by_text("Set date and time", exact=False).first.click(timeout=8_000)
            page.wait_for_timeout(2_500)
            yyyy, mm, dd = post["date"].split("-")
            hh, mi = plan["time_local"].split(":")
            # The field is dd/mm/yyyy - the placeholder says so, and the first
            # version typed mm/dd/yyyy, i.e. day 09 of month 14. Select-all
            # before typing too: fill() APPENDS here.
            d = page.locator("input[placeholder='dd/mm/yyyy']").first
            if d.count():
                d.click(timeout=6_000)
                page.keyboard.press("Meta+A")
                page.keyboard.type(f"{dd}/{mm}/{yyyy}")
                page.wait_for_timeout(1_200)
                page.keyboard.press("Escape")      # close the date picker
                page.wait_for_timeout(800)
            # Time is TWO inputs, hours and minutes, like TikTok's. fill()
            # rather than click+type: typing left them empty, and the Escape
            # that closes the date picker appears to clear a focused one.
            for label, value in (("hours", hh), ("minutes", mi)):
                f = page.locator(f"input[aria-label='{label}']").first
                if f.count():
                    f.fill(value, timeout=6_000)
                    page.wait_for_timeout(900)
                    if not (f.input_value(timeout=4_000) or "").strip():
                        f.click(timeout=4_000)          # fallback: type it
                        page.keyboard.type(value)
                        page.wait_for_timeout(900)
            page.wait_for_timeout(1_200)
            try:  # bring the schedule block into view so the shot shows it
                page.locator("input[placeholder='dd/mm/yyyy']").first \
                    .scroll_into_view_if_needed(timeout=5_000)
                page.wait_for_timeout(1_200)
            except Exception:  # noqa: BLE001
                pass
            # read the fields back; a schedule that did not take is worse than
            # one that failed loudly
            # The time lives in React state, not in input.value - every DOM
            # read comes back empty while the screenshot plainly shows 20:30.
            # So gate on what IS readable here (the date, plus Facebook's own
            # validation enabling the Schedule button) and verify the time
            # properly after submitting, from the scheduled-posts list.
            want_date = f"{int(dd)} {calendar.month_name[int(mm)]} {yyyy}"
            date_ok = page.evaluate("""(w) => [...document.querySelectorAll(
                "input[placeholder='dd/mm/yyyy']")].some(e => (e.value||'') === w)""",
                want_date)
            sched = f"{want_date} {int(hh)}:{mi}"
            btn = page.get_by_role("button", name="Schedule").first
            can_schedule = btn.count() > 0 and btn.is_enabled(timeout=5_000)
            if not date_ok or not can_schedule:
                page.screenshot(path=args.shot.replace(".png", "-timefail.png"))
                print(json.dumps({"status": "schedule_not_set", "clip": args.clip,
                                  "wanted": sched, "date_ok": date_ok,
                                  "schedule_button_enabled": can_schedule},
                                 ensure_ascii=False))
                ctx.close()
                return 7
        except Exception as e:  # noqa: BLE001
            print(f"warn: schedule step failed ({str(e)[:60]})", file=sys.stderr)

        page.screenshot(path=args.shot, full_page=False)
        if args.dry:
            ctx.close()
            print(json.dumps({"status": "dry_ok", "clip": args.clip,
                              "video": str(video), "chars": len(caption),
                              "post_to": ig_off, "schedule": sched,
                              "screenshot": args.shot}, ensure_ascii=False))
            return 0

        # HARD GATE. The composer targets the page AND the linked Instagram
        # account, each with its own schedule row, and untickng Instagram is not
        # working yet. Publishing with it still selected would put a SECOND
        # Instagram post on top of the reel already scheduled there. Refuse.
        targets = page.evaluate("""() => {
            const r = [...document.querySelectorAll('*')].find(e =>
                e.offsetParent !== null &&
                /Post to/i.test(e.innerText || '') &&
                (e.innerText || '').length < 300);
            return r ? r.innerText.replace(/\\s+/g, ' ') : '';
        }""")
        if ig_handle in (targets or ""):
            page.screenshot(path=args.shot.replace(".png", "-blocked.png"))
            print(json.dumps({"status": "instagram_still_targeted",
                              "clip": args.clip, "targets": (targets or "")[:160],
                              "why": "publishing now would duplicate the Instagram "
                                     "reel; deselect Instagram in 'Post to' first"},
                             ensure_ascii=False))
            ctx.close()
            return 6

        page.get_by_role("button", name="Schedule").first.click(timeout=10_000)
        page.wait_for_timeout(15_000)

        # Verify from the scheduled list, never from the form we just filled.
        listing = "https://business.facebook.com/latest/posts/scheduled_posts"
        if asset_id:
            listing += f"?asset_id={asset_id}"
        page.goto(listing, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(14_000)
        listed = page.inner_text("body")
        head = f"{int(dd)} {calendar.month_name[int(mm)]} {int(hh)}:{mi}"
        page.screenshot(path=args.shot.replace(".png", "-after.png"), full_page=True)
        ctx.close()
        ok = head in listed
        # Log here, same as TikTok and Instagram do. Facebook was the one
        # uploader that never called autolog, so its posts were invisible to
        # publog and to the A/B arm counts - six WS212 rows came out TikTok-only.
        import autolog
        logged = autolog.log(args.plan, args.clip, "facebook", None) if ok else {}
        print(json.dumps({"status": "scheduled" if ok else "not_found_in_list",
                          "clip": args.clip, "wanted": sched,
                          "time_listed": want_time_in(listed, hh, mi), **logged},
                         ensure_ascii=False))
        return 0 if ok else 8


if __name__ == "__main__":
    raise SystemExit(main())
