#!/usr/bin/env python3
"""Read-only login probe for the ws-scraper Chrome profile.

For each publisher, launches the profile headless the way that uploader
does (same channel/args/UA), loads only the uploader's entry page, and
reports whether it is a login redirect and whether the session cookie
exists. Never touches a form, never uploads, never clicks. Cookie values
are never printed — only presence.

    session_check.py [--ua uploader|mac|none] [--only tiktok,instagram,...]

Exit 0 if every checked platform is logged in, 2 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROFILE = Path(os.environ.get("WS_SCRAPER_PROFILE") or Path.home() / ".ws-scraper" / "profile")
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def _cfg() -> dict:
    try:
        return json.loads((Path.home() / ".sofit" / "publish.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def checks() -> list[tuple]:
    """(name, url, ua mode, (cookie domain, cookie name), login-url markers)."""
    c = _cfg()
    li = c.get("li_company_id", "")
    fb = c.get("fb_asset_id", "")
    return [
        ("tiktok", "https://www.tiktok.com/tiktokstudio/upload", "none",
         ("https://www.tiktok.com", "sessionid"), ("/login",)),
        ("instagram", "https://www.instagram.com/", "none",
         ("https://www.instagram.com", "sessionid"), ("/accounts/login",)),
        ("youtube", "https://studio.youtube.com", "mac",
         ("https://www.google.com", "SID"), ("accounts.google.com",)),
        ("linkedin", f"https://www.linkedin.com/company/{li}/admin/dashboard/", "none",
         ("https://www.linkedin.com", "li_at"), ("/login", "authwall", "/uas/")),
        ("facebook", "https://business.facebook.com/latest/composer"
         + (f"?asset_id={fb}" if fb else ""), "mac",
         ("https://www.facebook.com", "c_user"), ("/login",)),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ua", choices=["uploader", "mac", "none"], default="uploader",
                    help="uploader = mirror each uploader script's own UA choice")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    only = {x for x in a.only.split(",") if x}
    from playwright.sync_api import sync_playwright
    out, all_ok = {}, True
    with sync_playwright() as pw:
        for name, url, ua_mode, (cdom, cname), bad in checks():
            if only and name not in only:
                continue
            mode = ua_mode if a.ua == "uploader" else a.ua
            kw = {"user_agent": MAC_UA} if mode == "mac" else {}
            r: dict = {"ua": mode}
            try:
                ctx = pw.chromium.launch_persistent_context(
                    str(PROFILE), headless=True, channel="chrome",
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1400, "height": 900}, **kw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                r["cookie"] = any(c["name"] == cname and c.get("value")
                                  for c in ctx.cookies(cdom))
                page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(9_000)
                final = page.url
                r["final_url"] = final.split("?")[0][:120]
                r["title"] = (page.title() or "")[:80]
                r["login_redirect"] = any(b in final for b in bad)
                body = page.inner_text("body")[:4000]
                low = body.lower()
                r["login_prompt_on_page"] = any(s in low for s in (
                    "log in to tiktok", "sign in to continue", "join linkedin",
                    "log into facebook", "unsupported browser", "browser is not supported"))
                r["cookie_after"] = any(c["name"] == cname and c.get("value")
                                        for c in ctx.cookies(cdom))
                r["logged_in"] = (r["cookie_after"] and not r["login_redirect"]
                                  and not r["login_prompt_on_page"])
                shot = Path.home() / ".ws-scraper" / "checks" / f"{name}.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot))
                r["shot"] = str(shot)
                ctx.close()
            except Exception as e:  # noqa: BLE001
                r["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                r["logged_in"] = False
            all_ok &= bool(r["logged_in"])
            out[name] = r
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
