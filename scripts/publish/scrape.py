#!/usr/bin/env python3
"""Weekly Sync clip-metrics scraper (M1 of the publish-measure-learn loop).

Reads the sofit performance log, finds TikTok rows that are due for metrics
(posted >= MIN_AGE_DAYS ago and not yet final), scrapes TikTok Studio's
per-post analytics through a persistent logged-in browser profile, fills
views / avg-watch / retention into the SAME rows (atomic rewrite), and prints
a JSON summary for the digest step.

First run: `python3 scrape.py --headed` and log in to TikTok once - the
session persists in PROFILE_DIR. After that, headless on a schedule.

Exit codes: 0 ok; 2 session dead (needs manual re-login) - the wrapper turns
this into a WhatsApp alert instead of silent zero rows.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import statistics
import sys
import tempfile
from pathlib import Path

# ~/.sofit, NOT ~/Documents: TCC blocks launchd from Documents, which crashed
# this scraper daily (PermissionError) and silently killed the digest.
PERF_LOG = Path(os.environ.get("SOFIT_PERF_LOG")
                or Path.home() / ".sofit" / "performance.jsonl")
PROFILE_DIR = Path(os.environ.get("WS_SCRAPER_PROFILE")
                   or Path.home() / ".ws-scraper" / "profile")
MIN_AGE_DAYS = 3      # metrics stabilize ~3 days after posting
FINAL_AGE_DAYS = 14   # stop refreshing after two weeks


def _parse_count(s: str) -> int:
    s = s.strip().replace(",", "")
    m = re.match(r"^([\d.]+)([KM]?)$", s)
    if not m:
        return 0
    v = float(m.group(1))
    return int(v * {"": 1, "K": 1_000, "M": 1_000_000}[m.group(2)])


def _extract_metrics(body_text: str) -> dict | None:
    """Pull the Overview numbers out of TikTok Studio's analytics page text."""
    def after(label: str) -> str | None:
        m = re.search(re.escape(label) + r"\s*\n\s*([^\n]+)", body_text)
        return m.group(1).strip() if m else None

    views_s = after("Video views")
    avg_s = after("Average watch time")
    full_s = after("Watched full video")
    if not (views_s and avg_s):
        return None
    out = {"views": _parse_count(views_s)}
    m = re.match(r"([\d.]+)s", avg_s)
    if m:
        out["avg_watch_s"] = float(m.group(1))
    if full_s:
        m = re.match(r"([\d.]+)%", full_s)
        if m:
            out["watched_full"] = float(m.group(1))
    # Where the audience bailed ("Most viewers stopped watching at 0:02") -
    # per-clip proof of WHICH second loses people (hook-card window etc).
    m = re.search(r"stopped watching at (\d+):(\d{2})", body_text)
    if m:
        out["drop_at_s"] = int(m.group(1)) * 60 + int(m.group(2))
    # Traffic source split - the direct measure of covers/search work.
    i = body_text.find("Traffic source")
    if i >= 0:
        seg = body_text[i:i + 400]
        traffic = {}
        for label, key in (("For You", "for_you"), ("Personal profile", "profile"),
                           ("Search", "search"), ("Following", "following"),
                           ("Sound", "sound")):
            m = re.search(re.escape(label) + r"\s*\n\s*(<?)([\d.]+)%", seg)
            if m:
                traffic[key] = 0.0 if m.group(1) else float(m.group(2))
        if traffic:
            out["traffic"] = traffic
    return out


def _extract_ig_metrics(modal_text: str) -> dict | None:
    """Pull metrics out of Instagram's per-reel View-insights modal text.
    Shape observed: '4\\n0\\n0:02\\nViews\\nViews\\n262\\n...Saves\\n0\\nShares\\n3...'"""
    m = re.search(r"Views\s*\nViews\s*\n([\d,.KM]+)", modal_text)
    if not m:
        return None
    out = {"views": _parse_count(m.group(1))}
    m = re.search(r"(\d+):(\d{2})\s*\nViews", modal_text)
    if m:
        out["avg_watch_s"] = int(m.group(1)) * 60 + int(m.group(2))
    for label, key in (("Saves", "saves"), ("Shares", "shares"),
                       ("Follows", "follows")):
        m = re.search(label + r"\s*\n([\d,.KM]+)", modal_text)
        if m:
            out[key] = _parse_count(m.group(1))
    # Discovery signal: how much reach came from NON-followers.
    m = re.search(r"Non-followers\s*\n([\d.]+)%", modal_text)
    if m:
        out["nonfollower_pct"] = float(m.group(1))
    return out



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
IG_PROFILE = _CFG.get("ig_profile", "")
SOFIT_BIN = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "sofit"
PLANS_DIR = Path(_CFG.get("plans_dir", "~/.sofit/plans")).expanduser()



def _extract_yt_metrics(body_text: str) -> dict | None:
    """Pull views + avg view duration out of YT Studio's video analytics page.

    Shorts report fractional watch via "Average view duration" (M:SS), the
    same honest attention metric TikTok gives - rank youtube on avg_watch_s.
    """
    m = re.search(r"Views\s*\n\s*([\d,.KM]+)", body_text)
    if not m:
        return None
    out = {"views": _parse_count(m.group(1))}
    d = re.search(r"Average view duration\s*\n\s*(\d+):(\d{2})", body_text)
    if d:
        out["avg_watch_s"] = int(d.group(1)) * 60 + int(d.group(2))
    sw = re.search(r"[Vv]iewed vs\.? swiped away\s*\n\s*([\d.]+)%", body_text)
    if sw:
        out["watched_full"] = float(sw.group(1))
    return out



def _extract_li_metrics(body_text: str) -> dict | None:
    """Pull impressions/reactions/comments from a LinkedIn post page viewed as
    its AUTHOR (impressions are author-only). LinkedIn has no per-second
    retention surface, so views=impressions is the honest coarse metric -
    same rank-by-views logic as Instagram."""
    t = body_text.replace(",", "")
    imp = re.search(r"([\d.]+[KM]?)\s*(?:impressions?|חשיפות)", t, re.I)
    if not imp:
        return None
    out = {"views": _parse_count(imp.group(1))}
    rx = re.search(r"([\d.]+[KM]?)\s*(?:reactions?|תגובות רגשיות)", t, re.I)
    cm = re.search(r"([\d.]+[KM]?)\s*(?:comments?|תגובות)\b", t, re.I)
    if rx:
        out["reactions"] = _parse_count(rx.group(1))
    if cm:
        out["comments"] = _parse_count(cm.group(1))
    return out



def _tt_insight_extras(body: str) -> dict:
    """Mine TikTok's per-video insight JSON: second-by-second retention curve
    + share/save counts. Curve points are [[second, pct], ...]."""
    out: dict = {}
    try:
        d = json.loads(body)
    except ValueError:
        return out
    def find(o, key):
        if isinstance(o, dict):
            if key in o:
                return o[key]
            for v in o.values():
                r = find(v, key)
                if r is not None:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = find(v, key)
                if r is not None:
                    return r
        return None
    cur = find(d, "video_retention_rate_realtime")
    pts = None
    if isinstance(cur, dict):
        pts = find(cur, "list")
    if isinstance(pts, list) and pts:
        curve = []
        for p in pts:
            if isinstance(p, dict) and "value" in p:
                t = p.get("timestamp", len(curve))
                try:
                    t = int(t)
                    v = float(p["value"])
                except (TypeError, ValueError):
                    continue
                # API units: timestamps in ms, values as 0..1 fractions.
                if t >= 1000:
                    t //= 1000
                if v <= 1.0:
                    v *= 100
                curve.append([t, round(v, 1)])
        if curve:
            out["retention_curve"] = curve
            # Hook-window survival: % still watching at 3s - the direct
            # measure of whether the OPENING held (hook-line analysis).
            at3 = [v for t, v in curve if t == 3]
            if at3:
                out["hook_hold_3s"] = at3[0]
    for jkey, rkey in (("share_count", "shares"), ("collect_count", "saves")):
        v = find(d, jkey)
        if isinstance(v, (int, float)):
            out[rkey] = int(v)
    return out


def _pending_ig_posts(log_rows: list, today) -> list:
    """Planned IG posts whose date has passed but no instagram row exists yet."""
    pending = []
    for plan_path in sorted(PLANS_DIR.glob("publish-plan-*.json")):
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for post in plan.get("posts", []):
            try:
                posted = datetime.date.fromisoformat(post["date"])
            except (KeyError, ValueError):
                continue
            if posted > today:
                continue
            if any(r.get("clip") == post["clip"]
                   and r.get("episode") == plan.get("episode")
                   and r.get("platform") == "instagram" for r in log_rows):
                continue
            head = " ".join((post.get("instagram") or "").split())[:40]
            if head:
                pending.append({"episode": plan["episode"], "clip": post["clip"],
                                "head": head})
    return pending


def _discover_live_ig_reels(page, log_rows: list, today: datetime.date) -> list:
    """Scheduled IG reels have no URL until they go live. Once a planned post's
    date has passed and no instagram row exists for it yet, find the live reel
    on the profile by caption match and publish-log it (which brings hook,
    variant, duration and the duplicate guard for free)."""
    import subprocess

    pending = _pending_ig_posts(log_rows, today)
    if not pending:
        return []

    logged = []
    try:
        page.goto(f"https://www.instagram.com/{IG_PROFILE}/reels/",
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(7_000)
        links = page.eval_on_selector_all(
            "a[href*='/reel/']", "els => [...new Set(els.map(e => e.href))]")
        for url in links[:10]:
            if not pending:
                break
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(5_000)
                body = " ".join(page.inner_text("body").split())
            except Exception:  # noqa: BLE001
                continue
            for p in list(pending):
                if p["head"] in body:
                    video = (Path.home() / "Downloads"
                             / f"{p['episode']}_{p['clip']}.mp4")
                    cmd = [str(SOFIT_BIN), "publish-log", str(video),
                           "--episode", p["episode"], "--platform", "instagram",
                           "--url", url.split("?")[0]]
                    # Carry the speaker over from this clip's existing row
                    # (usually the TikTok one, logged at schedule time).
                    spk = next((r0.get("speaker") for r0 in log_rows
                                if r0.get("clip") == p["clip"]
                                and r0.get("episode") == p["episode"]
                                and r0.get("speaker")), None)
                    if spk:
                        cmd += ["--speaker", spk]
                    r = subprocess.run(cmd, capture_output=True, text=True,
                                       timeout=60)
                    if r.returncode == 0:
                        logged.append({"clip": p["clip"], "url": url.split("?")[0]})
                    pending.remove(p)
    except Exception as e:  # noqa: BLE001
        print(f"warn: live-reel discovery failed ({e})", file=sys.stderr)
    return logged


def _due(row: dict, today: datetime.date, min_age: int) -> bool:
    if row.get("platform") not in ("tiktok", "instagram", "youtube",
                                   "linkedin") \
            or not row.get("post_url"):
        return False
    if row.get("final"):
        return False
    try:
        posted = datetime.date.fromisoformat(row["date"])
    except (KeyError, ValueError):
        return False
    return (today - posted).days >= min_age


# Which metric actually separates clips, per platform.
#
# NOT retention%: it is avg_watch_s / duration, and measured watch time barely
# moves with clip length (~13.5s across a 32s, 33s and 36s clip), so retention
# mostly ranks clips by how short they are. The shortest clip "won" while
# pulling the fewest views.
#
# TikTok reports fractional avg-watch seconds, so it ranks on attention
# directly. Instagram rounds avg-watch to whole seconds - every row so far
# landed on 1 or 3 - so watch time there has no resolution to rank on, and
# views is the only honest signal. Never pool the two: Instagram counts a view
# after ~1s of play, so its watch numbers sit an order of magnitude below
# TikTok's and any shared ranking puts every Instagram post last.
RANK_BY = {"tiktok": "avg_watch_s", "instagram": "views",
           "youtube": "avg_watch_s", "linkedin": "views"}
_SUMMARY_KEYS = ("clip", "hook", "platform", "date", "duration",
                 "views", "avg_watch_s", "watched_full", "retention",
                 "shares", "saves", "drop_at_s", "nonfollower_pct", "hook_hold_3s")


def _rank(rows, platform):
    """Whole-corpus ranking for one platform, best first."""
    key = RANK_BY[platform]
    scored = [r for r in rows
              if r.get("platform") == platform and r.get(key) is not None]
    # saves+shares as the tiebreak: the strongest quality signals after the
    # platform's primary metric (saves ranked above shares per Buffer's
    # analytics guidance; both beat raw views).
    scored.sort(key=lambda r: (r[key],
                               (r.get("saves") or 0) + (r.get("shares") or 0)),
                reverse=True)
    return [{k: r.get(k) for k in _SUMMARY_KEYS} for r in scored]


# A sorted list always has a first element, but "first" is not "winner". The
# top three TikTok clips sat within 0.7s of each other (13.9 / 13.6 / 13.2)
# while the fourth collapsed to 6.1s - so only the fourth is a finding. Naming
# a best clip off a 5% gap is how a 3-second length difference gets written
# down as a content lesson. Report an outlier or report nothing.
NOISE_BAND = 0.25


def _outliers(ranked, platform):
    """(best, worst) - each None unless it clears NOISE_BAND around the median."""
    if len(ranked) < 3:
        return None, None                      # nothing to be an outlier from
    key = RANK_BY[platform]
    mid = statistics.median(r[key] for r in ranked)
    if not mid:
        return None, None
    best = ranked[0] if ranked[0][key] > mid * (1 + NOISE_BAND) else None
    worst = ranked[-1] if ranked[-1][key] < mid * (1 - NOISE_BAND) else None
    return best, worst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true",
                    help="open a visible browser (first-run login)")
    ap.add_argument("--min-age", type=int, default=MIN_AGE_DAYS)
    args = ap.parse_args()

    if not PERF_LOG.exists():
        print(json.dumps({"status": "ok", "updated": 0, "note": "no log file"}))
        return 0
    lines = [json.loads(l) for l in PERF_LOG.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    today = datetime.date.today()
    due = [r for r in lines if _due(r, today, args.min_age)]
    pending_ig = _pending_ig_posts(lines, today)
    if not due and not pending_ig and not args.headed:
        print(json.dumps({"status": "ok", "updated": 0, "note": "nothing due"}))
        return 0

    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    updated, failures = [], []
    with sync_playwright() as pw:
        # channel="chrome" = the user's real Google Chrome binary (TikTok
        # blocks logins in the bundled Chromium-for-testing build), plus the
        # AutomationControlled flag off so the login flow doesn't bot-gate.
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=not args.headed, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            # YT Studio serves a stripped page to HeadlessChrome UAs, so every
            # youtube row came back with no metrics (2026-08-28: 18 failures,
            # an empty youtube ranking). upload_youtube.py already overrode the
            # UA; the scraper never did.
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/140.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Login-state check: a dead session must alert, never silently zero.
        # This navigation is the scraper's most common crash: three failures in
        # four days (2026-09-25/27/28), the last two both 60s timeouts here,
        # while a manual rerun minutes later succeeded every time. TikTok Studio
        # is simply slow sometimes, so retry rather than lose the day's numbers.
        for attempt in (1, 2, 3):
            try:
                page.goto("https://www.tiktok.com/tiktokstudio/content",
                          wait_until="domcontentloaded", timeout=60_000 * attempt)
                break
            except PWTimeout:
                if attempt == 3:
                    raise
                print(f"warn: tiktokstudio timed out (attempt {attempt}), retrying",
                      file=sys.stderr)
                page.wait_for_timeout(5_000 * attempt)
        page.wait_for_timeout(6_000)
        def _logged_out() -> bool:
            return "/login" in page.url or "Log in" in (page.title() or "")

        if _logged_out():
            if args.headed:
                print("Log in to TikTok in the opened window (waiting up to "
                      "10 minutes, continues automatically)...", file=sys.stderr)
                try:
                    for _ in range(120):  # poll every 5s, up to 10 min
                        page.wait_for_timeout(5_000)
                        if not _logged_out():
                            break
                    else:
                        ctx.close()
                        print(json.dumps({"status": "session_dead",
                                          "fix": "login not completed in time"}))
                        return 2
                except Exception:  # noqa: BLE001 - window closed mid-login
                    print(json.dumps({"status": "session_dead",
                                      "fix": "browser window was closed before "
                                             "login finished - re-run --headed"}))
                    return 2
                # Land back on Studio so the session is fully established.
                page.goto("https://www.tiktok.com/tiktokstudio/content",
                          wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(4_000)
                print("Login detected - scraping...", file=sys.stderr)
            else:
                ctx.close()
                print(json.dumps({"status": "session_dead",
                                  "fix": "run scrape.py --headed and log in"}))
                return 2

        # Live-reel discovery: planned IG posts past their date get found on
        # the profile by caption and publish-logged (they had no URL until
        # now). Re-read the log afterwards so new rows join this run's scrape.
        newly_logged = []
        if pending_ig and not IG_PROFILE:
            print("warn: ig_profile not configured - skipping live-reel "
                  "discovery", file=sys.stderr)
            pending_ig = []
        if pending_ig:
            newly_logged = _discover_live_ig_reels(page, lines, today)
            if newly_logged:
                lines = [json.loads(l) for l in
                         PERF_LOG.read_text(encoding="utf-8").splitlines()
                         if l.strip()]
                due = [r for r in lines if _due(r, today, args.min_age)]

        # Instagram session check (only when IG rows are due): sessionid cookie.
        ig_due = [r for r in due if r["platform"] == "instagram"]
        ig_ok = True
        if ig_due:
            ig_ok = any(c["name"] == "sessionid" and c.get("value")
                        for c in ctx.cookies("https://www.instagram.com"))
            if not ig_ok:
                failures.extend(r["post_url"] for r in ig_due)
                print("instagram session dead - skipping IG rows", file=sys.stderr)

        # LinkedIn session check (only when LI rows are due): li_at cookie.
        li_due = [r for r in due if r["platform"] == "linkedin"]
        li_ok = True
        if li_due:
            li_ok = any(c["name"] == "li_at" and c.get("value")
                        for c in ctx.cookies("https://www.linkedin.com"))
            if not li_ok:
                failures.extend(r["post_url"] for r in li_due)
                print("linkedin session dead - skipping LI rows "
                      "(one-time headed login into the ws-scraper profile)",
                      file=sys.stderr)

        # YouTube session check (only when YT rows are due): probe Studio once.
        yt_due = [r for r in due if r["platform"] == "youtube"]
        yt_ok = True
        if yt_due:
            page.goto("https://studio.youtube.com",
                      wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)
            yt_ok = "accounts.google.com" not in page.url
            if not yt_ok:
                failures.extend(r["post_url"] for r in yt_due)
                print("youtube session dead - skipping YT rows "
                      "(run upload_youtube.py --login)", file=sys.stderr)

        # TikTok Studio loads per-video insight JSON (aweme/v2/data/insight)
        # that carries the second-by-second retention curve and share_count -
        # data the page renders only as a chart. Intercept it.
        tt_insight = {"body": None}

        def _capture_insight(resp):
            try:
                if ("aweme/v2/data/insight" in resp.url and resp.status == 200
                        and len(b := resp.text()) > 3_000):
                    if tt_insight["body"] is None or len(b) > len(tt_insight["body"]):
                        tt_insight["body"] = b
            except Exception:  # noqa: BLE001
                pass

        page.on("response", _capture_insight)

        for row in due:
            try:
                if row["platform"] == "linkedin":
                    if not li_ok:
                        continue
                    # The direct post permalink bot-gates ("Something went
                    # wrong"); the author's analytics page loads fine after a
                    # feed warm-up. Activity id comes from the post URL.
                    m = re.search(r"activity[-:](\d+)", row["post_url"])
                    if not m:
                        failures.append(row["post_url"])
                        continue
                    page.goto("https://www.linkedin.com/feed/",
                              wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(4_000)
                    page.goto("https://www.linkedin.com/analytics/post-summary/"
                              f"urn:li:activity:{m.group(1)}/",
                              wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(8_000)
                    metrics = _extract_li_metrics(page.inner_text("body"))
                elif row["platform"] == "youtube":
                    if not yt_ok:
                        continue
                    m = re.search(r"(?:youtu\.be/|shorts/|[?&]v=)([\w-]{6,})",
                                  row["post_url"])
                    if not m:
                        failures.append(row["post_url"])
                        continue
                    page.goto("https://studio.youtube.com/video/"
                              f"{m.group(1)}/analytics/tab-overview/period-default",
                              wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(8_000)
                    metrics = _extract_yt_metrics(page.inner_text("body"))
                elif row["platform"] == "tiktok":
                    m = re.search(r"/video/(\d+)", row["post_url"])
                    if not m:
                        failures.append(row["post_url"])
                        continue
                    tt_insight["body"] = None
                    page.goto("https://www.tiktok.com/tiktokstudio/analytics/"
                              f"{m.group(1)}", wait_until="domcontentloaded",
                              timeout=60_000)
                    page.wait_for_timeout(6_000)
                    metrics = _extract_metrics(page.inner_text("body"))
                    if metrics and tt_insight["body"]:
                        extra = _tt_insight_extras(tt_insight["body"])
                        metrics.update(extra)
                else:  # instagram: open the reel, click View insights, parse modal
                    if not ig_ok:
                        continue
                    page.goto(row["post_url"], wait_until="domcontentloaded",
                              timeout=60_000)
                    page.wait_for_timeout(7_000)
                    page.get_by_text("View insights", exact=False).first.click()
                    page.wait_for_timeout(6_000)
                    metrics = _extract_ig_metrics(page.inner_text("body"))
            except Exception:  # noqa: BLE001 - per-post failure must not kill the run
                metrics = None
            if not metrics or not metrics.get("views"):
                # Zero views = the post is not live yet (scheduled) or the
                # page failed to load real data - keep the row pending rather
                # than poisoning the log (and the selector prompt) with 0%.
                failures.append(row["post_url"])
                continue
            row.update(metrics)
            if row.get("duration") and metrics.get("avg_watch_s"):
                row["retention"] = min(100, round(
                    metrics["avg_watch_s"] / row["duration"] * 100))
            elif metrics.get("watched_full") is not None and "retention" not in row:
                row["retention"] = round(metrics["watched_full"])
            posted = datetime.date.fromisoformat(row["date"])
            if (today - posted).days >= FINAL_AGE_DAYS:
                row["final"] = True
            updated.append({"clip": row.get("clip"), "hook": row.get("hook"),
                            "views": row.get("views"),
                            "retention": row.get("retention")})
        ctx.close()

    if updated:
        # Atomic rewrite of the whole log with the filled rows.
        fd, tmp = tempfile.mkstemp(dir=str(PERF_LOG.parent), suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in lines:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, PERF_LOG)

    ranked = {p: _rank(lines, p) for p in RANK_BY}
    best, worst = _outliers(ranked["tiktok"], "tiktok")

    # Per-speaker aggregates (rows that carry `speaker`), per platform and on
    # that platform's honest metric — keeps the "who should open clips"
    # question answered by the daily summary instead of by memory.
    by_speaker: dict = {}
    for p, metric in RANK_BY.items():
        agg: dict = {}
        for r in lines:
            if (r.get("speaker") and r.get("platform") == p
                    and r.get(metric) is not None):
                agg.setdefault(r["speaker"], []).append(r[metric])
        by_speaker[p] = {s: {"clips": len(v),
                             metric: round(sum(v) / len(v), 1)}
                         for s, v in sorted(agg.items())}
    # A/B cells (Navot, 2026-09-17: every batch runs 2-3 tests and a winner is
    # called after the week). The tags already ride along - publog writes
    # hook_style, and the variant comes from the rendered filename - but nothing
    # compared them, so the data was being collected and never read.
    # Episode-blind on purpose: a cell only becomes readable once it has
    # accumulated across several batches, so this aggregates everything.
    def _cells(key, default=None):
        out: dict = {}
        for p, metric in RANK_BY.items():
            agg: dict = {}
            for r in lines:
                v = r.get(key, default)
                if v is None or r.get("platform") != p or r.get(metric) is None:
                    continue
                agg.setdefault(str(v), []).append(r[metric])
            out[p] = {k: {"clips": len(v), metric: round(sum(v) / len(v), 1)}
                      for k, v in sorted(agg.items())}
        return out

    by_hook_style = _cells("hook_style", "flash")   # untagged rows are the default arm
    by_variant = _cells("variant")
    # Every ab_* dimension any row carries, reported without needing code here:
    # a new test is a filename tag, not a patch to the scraper.
    ab_dims = sorted({k for r in lines for k in r if k.startswith("ab_")})
    by_ab = {d: _cells(d, "control") for d in ab_dims}

    print(json.dumps({
        "status": "ok", "updated": len(updated), "failed": len(failures),
        "new_live_reels": newly_logged,
        "failed_urls": failures[:5],
        "rank_by": RANK_BY, "noise_band": NOISE_BAND,
        "best": best,
        "worst": worst,
        "ranked": ranked,
        "by_speaker": by_speaker,
        "by_hook_style": by_hook_style,
        "by_variant": by_variant,
        "by_ab": by_ab,
        "all": updated,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
