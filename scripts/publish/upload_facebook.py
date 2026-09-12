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
import json
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
        page.goto(COMPOSER, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(12_000)
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
        ig_off = page.evaluate("""() => {
            const row = [...document.querySelectorAll('*')].find(e =>
                e.offsetParent !== null &&
                /Post to/i.test((e.innerText || '')) &&
                (e.innerText || '').length < 300);
            return row ? row.innerText.replace(/\\s+/g, ' ').slice(0, 160) : null;
        }""")
        try:
            page.get_by_text("Post to", exact=False).first.click(timeout=6_000)
            page.wait_for_timeout(2_000)
            ig = page.get_by_text("weeklysyncpodcast", exact=True).first
            if ig.count():
                ig.click(timeout=5_000)          # untick the Instagram target
                page.wait_for_timeout(1_500)
            page.keyboard.press("Escape")
            page.wait_for_timeout(1_000)
        except Exception as e:  # noqa: BLE001
            print(f"warn: could not adjust targets ({str(e)[:60]})", file=sys.stderr)

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
            # select-all before typing: fill() APPENDS here, which produced
            # "12/9/202609/14/2026" on the first run
            d = page.locator("input[placeholder*='/'], input[type=date]").first
            if d.count():
                yyyy, mm, dd = post["date"].split("-")
                d.click(timeout=6_000)
                page.keyboard.press("Meta+A")
                page.keyboard.type(f"{mm}/{dd}/{yyyy}")
                page.wait_for_timeout(1_000)
            t = page.locator("input[placeholder*=':'], input[type=time]").first
            if t.count():
                t.click(timeout=6_000)
                page.keyboard.press("Meta+A")
                page.keyboard.type(plan["time_local"])
                page.wait_for_timeout(1_000)
            page.keyboard.press("Escape")
            page.wait_for_timeout(1_500)
            sched = page.evaluate("""() => {
                const s = [...document.querySelectorAll('*')].find(e =>
                    e.offsetParent !== null &&
                    /Schedule/i.test(e.innerText || '') &&
                    (e.innerText || '').length < 220);
                return s ? s.innerText.replace(/\\s+/g, ' ').slice(0, 140) : null;
            }""")
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
        ig_handle = _cfg().get("ig_profile") or "weeklysyncpodcast"
        if ig_handle in (targets or "") or ig_handle in page.inner_text("body"):
            page.screenshot(path=args.shot.replace(".png", "-blocked.png"))
            print(json.dumps({"status": "instagram_still_targeted",
                              "clip": args.clip, "targets": (targets or "")[:160],
                              "why": "publishing now would duplicate the Instagram "
                                     "reel; deselect Instagram in 'Post to' first"},
                             ensure_ascii=False))
            ctx.close()
            return 6

        page.get_by_role("button", name="Schedule").first.click(timeout=10_000)
        page.wait_for_timeout(12_000)
        page.screenshot(path=args.shot.replace(".png", "-after.png"), full_page=False)
        ctx.close()
        print(json.dumps({"status": "submitted", "clip": args.clip},
                         ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
