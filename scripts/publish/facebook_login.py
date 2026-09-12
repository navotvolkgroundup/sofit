"""Open a headed browser on the shared profile so the USER can sign in to
Facebook. Claude never types credentials - this only opens the page, waits, and
confirms by cookie that a session landed.

    facebook_login.py            # open and wait
    facebook_login.py --check    # just report whether a session exists

Same shape as upload_youtube.py --login, for the same reason.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROFILE = Path.home() / ".ws-scraper" / "profile"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def session(ctx) -> bool:
    """c_user is the logged-in user id; the other cookies exist for logged-out
    visitors too, so they prove nothing."""
    return any(c["name"] == "c_user" and c.get("value")
               for c in ctx.cookies() if "facebook" in c.get("domain", ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report state, open nothing")
    ap.add_argument("--minutes", type=int, default=10, help="how long to wait")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=args.check, channel="chrome", user_agent=UA,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded",
                  timeout=60_000)
        page.wait_for_timeout(4_000)

        if args.check:
            ok = session(ctx)
            print(json.dumps({"logged_in": ok}, ensure_ascii=False))
            ctx.close()
            return 0 if ok else 1

        if session(ctx):
            print(json.dumps({"status": "already_logged_in"}, ensure_ascii=False))
            ctx.close()
            return 0

        print("A browser window is open. Sign in to Facebook there - type the "
              "password yourself, it is never handled here.", file=sys.stderr)
        for _ in range(args.minutes * 6):
            page.wait_for_timeout(10_000)
            if session(ctx):
                print(json.dumps({"status": "session_saved"}, ensure_ascii=False))
                ctx.close()
                return 0
        print(json.dumps({"status": "timed_out", "minutes": args.minutes},
                         ensure_ascii=False))
        ctx.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
