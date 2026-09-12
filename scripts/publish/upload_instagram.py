#!/usr/bin/env python3
"""Semi-automated Instagram reel upload+schedule with collaborators.

Same human-confirm design as upload_tiktok.py: --dry fills everything
(file, caption, collaborators, schedule) and screenshots WITHOUT sharing;
only --submit clicks Share.

    upload_instagram.py --clip clip-12 --plan publish-plan.json --dry
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

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
# Empirical, not documented by IG: 10 minutes silently failed, the batch's
# other slots (hours to days out) all worked. Raise it if a 30-minute lead
# ever fails too.
MIN_LEAD_MIN = 30
CLIPS_DIR = Path(_CFG.get("clips_dir", "~/Downloads")).expanduser()
COLLABORATORS = _CFG.get("ig_collaborators", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--plan", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry", action="store_true")
    g.add_argument("--submit", action="store_true")
    ap.add_argument("--shot", default="/tmp/ig-upload-preview.png")
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

    # IG needs real lead time. A submit 10 minutes ahead of its slot took the
    # whole upload, said Schedule, and scheduled nothing (2026-09-10). The
    # upload costs minutes, so refuse before spending them.
    if args.submit:
        import datetime as _dt
        slot = _dt.datetime.fromisoformat(f"{post['date']}T{plan['time_local']}")
        lead = (slot - _dt.datetime.now()).total_seconds() / 60
        if lead < MIN_LEAD_MIN:
            print(json.dumps({"status": "too_soon", "clip": args.clip,
                              "slot": f"{post['date']} {plan['time_local']}",
                              "lead_minutes": round(lead),
                              "min_lead_minutes": MIN_LEAD_MIN}, ensure_ascii=False))
            return 5

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1500, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded",
                  timeout=60_000)
        page.wait_for_timeout(6_000)
        if not any(c["name"] == "sessionid" and c.get("value")
                   for c in ctx.cookies("https://www.instagram.com")):
            print(json.dumps({"status": "session_dead"}))
            return 2

        # 2026-08-27: IG greets a fresh session with a "Turn on Notifications"
        # modal that covers the WHOLE page - every sidebar click then lands on
        # its backdrop and silently does nothing. Dismiss it first.
        for label in ("Not Now", "Not now"):
            try:
                page.get_by_role("button", name=label).first.click(timeout=4_000)
                page.wait_for_timeout(1_200)
                break
            except Exception:  # noqa: BLE001
                continue

        # Create -> Post -> file
        # 2026-08-27: IG stopped exposing an accessible name on the composer
        # link (get_by_role("link", name="New post") now matches NOTHING), and
        # the bare "Create" text matches a HIDDEN span. The svg aria-label is
        # the only stable anchor - click its enclosing <a>.
        for loc in (page.locator("a:has(svg[aria-label='New post'])"),
                    page.locator("svg[aria-label='New post']"),
                    page.get_by_role("link", name="New post")):
            try:
                loc.first.click(timeout=5_000)
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            print(json.dumps({"status": "composer_not_found"}))
            return 4
        page.wait_for_timeout(2_000)
        try:
            page.get_by_text("Post", exact=True).first.click(timeout=3_000)
            page.wait_for_timeout(1_500)
        except Exception:  # noqa: BLE001
            pass
        page.locator("input[type=file]").first.set_input_files(str(video))
        page.wait_for_timeout(6_000)
        try:  # "Video posts are now shared as reels" info modal
            page.get_by_role("button", name="OK").click(timeout=5_000)
            page.wait_for_timeout(1_200)
        except Exception:  # noqa: BLE001
            pass
        # Crop: select Original so the native 9:16 frame is kept.
        try:
            page.locator("svg[aria-label='Select crop']").first.click(timeout=4_000)
            page.wait_for_timeout(800)
            page.get_by_text("Original", exact=True).first.click(timeout=3_000)
            page.wait_for_timeout(600)
        except Exception:  # noqa: BLE001
            pass  # native 9:16 videos default fine; preview verified in dry shot
        # Crop -> Edit step (holds "Cover photo" / "Select from computer").
        page.get_by_role("button", name="Next").click(timeout=8_000)
        page.wait_for_timeout(2_500)

        # Custom cover (2026-08-21): IG web is the ONLY platform that accepts
        # a cover FILE, and only HERE on the edit step - the share form has
        # no cover control. Auto-loads ~/Downloads/{episode}_cover-{clip}.jpg
        # (variant suffixes like .pers share the base clip's cover).
        base_clip = args.clip.replace(".pers", "")
        cover = CLIPS_DIR / f"{plan['episode']}_cover-{base_clip}.jpg"
        cover_set = False
        if cover.exists():
            try:
                sel = page.get_by_text("Select from computer", exact=True).first
                try:
                    with page.expect_file_chooser(timeout=6_000) as fc:
                        sel.click(timeout=5_000)
                    fc.value.set_files(str(cover))
                except Exception:  # noqa: BLE001 - hidden input fallback
                    page.locator("input[type=file]").last.set_input_files(
                        str(cover))
                page.wait_for_timeout(2_500)
                cover_set = True
            except Exception as e:  # noqa: BLE001
                print(f"warn: cover step failed ({e})", file=sys.stderr)

        # Edit -> Share form.
        page.get_by_role("button", name="Next").click(timeout=8_000)
        page.wait_for_timeout(2_500)

        # Caption (same DraftJS-style focus trick as TikTok).
        cap = page.locator("div[contenteditable='true']").first
        try:
            cap.click(timeout=3_000)
        except Exception:  # noqa: BLE001
            page.evaluate(
                "document.querySelector(\"div[contenteditable='true']\").focus()")
        page.wait_for_timeout(400)
        page.keyboard.insert_text(post["instagram"])
        page.wait_for_timeout(1_000)
        # The trailing hashtag opens an autocomplete dropdown that swallows
        # every later click; a trailing space dismisses it (Escape does not).
        page.keyboard.type(" ")
        page.wait_for_timeout(800)
        got = cap.inner_text().strip()
        if not got.startswith(post["instagram"].strip().split()[0]):
            page.screenshot(path=args.shot)
            print(json.dumps({"status": "caption_mismatch", "got": got[:100]},
                             ensure_ascii=False))
            ctx.close()
            return 3

        # Collaborators: open the row, type each handle, pick the suggestion.
        # Per-post "ig_collaborators" in the plan EXTENDS the defaults - used
        # to tag the episode guest on their clips (surfaces the post on their
        # profile too; Buffer-documented growth lever).
        collab_added = []
        wanted = COLLABORATORS + [c for c in (post.get("ig_collaborators") or [])
                                  if c not in COLLABORATORS]
        try:
            box = page.get_by_placeholder("Add collaborators").first
            box.click(timeout=4_000)
            page.wait_for_timeout(1_200)
            for name in wanted:
                # Two attempts: IG's suggestion list sometimes needs a re-type
                # (the first fill can land before the search field is wired).
                for attempt in (1, 2):
                    box.fill("")
                    page.wait_for_timeout(400)
                    box.fill(name)
                    page.wait_for_timeout(2_500 * attempt)
                    try:
                        page.get_by_text(name, exact=True).first.click(timeout=5_000)
                        collab_added.append(name)
                        page.wait_for_timeout(800)
                        break
                    except Exception:  # noqa: BLE001
                        continue
                else:
                    print(f"warn: collaborator {name} suggestion not found",
                          file=sys.stderr)
            page.get_by_role("button", name="Done").click(timeout=4_000)
            page.wait_for_timeout(1_000)
        except Exception as e:  # noqa: BLE001
            print(f"warn: collaborators step failed ({e})", file=sys.stderr)

        # Schedule content toggle + date + time spinbuttons.
        sched_val = ""
        try:
            switches = page.locator("[role='switch'], input[type='checkbox']")
            switches.nth(switches.count() - 1).click(force=True, timeout=4_000)
            page.wait_for_timeout(1_500)

            # Date: default is tomorrow; open the combobox and click the day
            # cell only when the plan date differs.
            import datetime as _dt
            target = _dt.date.fromisoformat(post["date"])
            default_txt = page.evaluate(
                """() => { const e=[...document.querySelectorAll('*')].find(x =>
                     x.children.length===0 && /, \\d{4}$/.test(x.textContent.trim()));
                     return e ? e.textContent.trim() : ''; }""")
            if target.strftime("%b %-d, %Y") not in default_txt:
                page.evaluate(
                    """() => { const e=[...document.querySelectorAll('*')].find(x =>
                         x.children.length===0 && /, \\d{4}$/.test(x.textContent.trim()));
                         if (e) e.click(); }""")
                page.wait_for_timeout(1_200)
                # The picker opens on the CURRENT month. Clicking a bare day
                # number without advancing schedules the wrong date (or leaves
                # it untouched, which IG then rejects with "Choose a time at
                # least 20 minutes from now") - 2026-08-27: the three
                # September posts of the WS208 batch all failed this way while
                # the two August ones went through.
                want_month = target.strftime("%B %Y")
                for _ in range(4):
                    shown = page.evaluate(
                        """() => { const e=[...document.querySelectorAll('*')].find(x =>
                             x.children.length===0 &&
                             /^[A-Z][a-z]+ \\d{4}$/.test(x.textContent.trim()));
                             return e ? e.textContent.trim() : ''; }""")
                    if shown == want_month or not shown:
                        break
                    try:
                        page.locator(
                            "div[role=button]:has(svg[aria-label='Next month']), "
                            "button[aria-label='Next month'], "
                            "div[role=button]:has(svg[aria-label='Right chevron'])"
                        ).first.click(timeout=4_000)
                        page.wait_for_timeout(1_000)
                    except Exception:  # noqa: BLE001
                        break
                # Click the day cell INSIDE the picker. A page-wide search for
                # the bare day number picks up unrelated "1"/"2" text from the
                # feed behind the dialog and clicks that instead (2026-08-27).
                page.evaluate(
                    """(d) => {
                      const lbl=[...document.querySelectorAll('*')].find(x =>
                        x.children.length===0 &&
                        /^[A-Z][a-z]+ \\d{4}$/.test(x.textContent.trim()));
                      if (!lbl) return;
                      let root=lbl;
                      for (let i=0;i<6;i++){
                        root=root.parentElement;
                        if (root && /\\bSun\\b/.test(root.textContent) &&
                            /\\bSat\\b/.test(root.textContent)) break;
                      }
                      if (!root) return;
                      const cell=[...root.querySelectorAll('*')].find(x =>
                        x.children.length===0 && x.textContent.trim()===String(d) &&
                        x.offsetParent!==null &&
                        getComputedStyle(x).cursor !== 'not-allowed');
                      if (cell) (cell.closest('[role=button]')||cell).click();
                    }""", target.day)
                page.wait_for_timeout(1_200)
                # Hard gate: the field MUST now read the target date. IG greys
                # out Schedule on a past date and the old code sailed past it.
                shown_date = page.evaluate(
                    """() => { const e=[...document.querySelectorAll('*')].find(x =>
                         x.children.length===0 && /, \\d{4}$/.test(x.textContent.trim()));
                         return e ? e.textContent.trim() : ''; }""")
                if target.strftime("%b") not in shown_date or \
                        str(target.day) not in shown_date:
                    page.screenshot(path=args.shot.replace(".png", "-datefail.png"))
                    ctx.close()
                    print(json.dumps({"status": "date_not_set", "clip": args.clip,
                                      "wanted": post["date"], "shown": shown_date},
                                     ensure_ascii=False))
                    return 5

            # Time: three keyboard-operable spinbuttons (Hours/Minutes/AM PM).
            hh24 = int(plan["time_local"].split(":")[0])
            mm = plan["time_local"].split(":")[1]
            hh12 = hh24 % 12 or 12
            mer = "PM" if hh24 >= 12 else "AM"

            def spin(label: str, text: str) -> None:
                sp = page.locator(f"[role='spinbutton'][aria-label='{label}']").first
                sp.click(force=True)
                page.wait_for_timeout(300)
                for ch in text:
                    page.keyboard.press(ch)
                    page.wait_for_timeout(150)

            spin("Hours", f"{hh12:02d}")
            spin("Minutes", mm)
            spin("AM PM", mer[0].lower())
            page.wait_for_timeout(600)
            # Readback: the spinbuttons stopped exposing aria-valuetext
            # (2026-08-27), so scrape the rendered "H:MM AM/PM" text instead -
            # that is what IG will actually schedule.
            sched_val = page.evaluate(
                """() => { const leaves=[...document.querySelectorAll('*')]
                     .filter(x => x.children.length===0);
                     const d=leaves.find(x => /, \\d{4}$/.test(x.textContent.trim()));
                     const re=/\\d{1,2}:\\d{2}\\s*(AM|PM)/i;
                     let t=null;
                     for (const inp of document.querySelectorAll(
                            'input,[role=spinbutton],[contenteditable]')) {
                       const v=(inp.value||inp.getAttribute('aria-valuetext')||
                                inp.textContent||'').trim();
                       if (re.test(v)) { t=v.match(re)[0]; break; }
                     }
                     // IG splits "01:00" and "PM" into sibling nodes, so match
                     // on the smallest CONTAINER whose joined text has both.
                     if (!t) {
                       const cands=[...document.querySelectorAll('div,span,label')]
                         .filter(x => re.test(x.textContent.replace(/\\s+/g,' ')))
                         .sort((a,b)=>a.textContent.length-b.textContent.length);
                       if (cands.length)
                         t=cands[0].textContent.replace(/\\s+/g,' ').match(re)[0];
                     }
                     return `${d?d.textContent.trim():'?'} ${t||'?:?'}`; }""")
        except Exception as e:  # noqa: BLE001
            print(f"warn: schedule step failed ({e})", file=sys.stderr)
        # Cross-post to the Facebook page, if the account is linked and the
        # config asks for it. The control lives under a collapsed "Share to"
        # section. Read the toggle back rather than trusting the click - a
        # silent no-op here is exactly how the collaborator field lost Tor on
        # seven of eight posts.
        fb_state = None
        if _CFG.get("share_to_facebook"):
            try:
                # click the ROW, not the label: the label is a text node inside
                # an expander and clicking it does not toggle the section
                # Click the expander ROW (its chevron sits at the far right);
                # clicking the label text alone leaves the section collapsed.
                opened = page.evaluate("""() => {
                    const label = [...document.querySelectorAll('*')].find(e =>
                        e.offsetParent !== null &&
                        (e.innerText || '').trim() === 'Share to');
                    if (!label) return false;
                    for (let n = label; n; n = n.parentElement) {
                        const r = n.getBoundingClientRect();
                        if (r.width > 250 && r.height > 30 && r.height < 90) {
                            n.setAttribute('data-sofit-share', '1');
                            return true;
                        }
                    }
                    return false;
                }""")
                if opened:
                    page.locator("[data-sofit-share]").first.click(timeout=6_000)
                else:
                    page.get_by_text("Share to", exact=True).first.click(timeout=6_000)
                page.wait_for_timeout(2_500)
                fb_section = page.evaluate("""() => {
                    const h = [...document.querySelectorAll('*')].find(e =>
                        e.offsetParent !== null &&
                        (e.innerText || '').trim().startsWith('Share to') &&
                        (e.innerText || '').length < 400);
                    return h ? h.innerText.replace(/\\s+/g, ' ').slice(0, 300) : null;
                }""")
                print(f"info: Share to section reads {fb_section!r}", file=sys.stderr)
                tog = page.locator(
                    "div[role=button]:has-text('Facebook'), label:has-text('Facebook')"
                ).locator("input[type=checkbox], [role=switch]").first
                if tog.count():
                    if tog.get_attribute("aria-checked") != "true":
                        tog.click(timeout=6_000)
                        page.wait_for_timeout(1_200)
                    fb_state = tog.get_attribute("aria-checked")
                else:
                    fb_state = "control_not_found"
            except Exception as e:  # noqa: BLE001
                fb_state = f"failed: {str(e)[:60]}"
        page.screenshot(path=args.shot, full_page=False)

        if args.dry:
            ctx.close()
            print(json.dumps({"status": "dry_ok", "screenshot": args.shot,
                              "clip": args.clip, "collaborators": collab_added,
                              "cover_set": cover_set, "facebook": fb_state,
                              "schedule_fields": sched_val},
                             ensure_ascii=False))
            return 0

        try:  # scheduling ON renders "Schedule"; immediate post renders "Share"
            page.get_by_role("button", name="Schedule").click(timeout=5_000)
        except Exception:  # noqa: BLE001
            page.get_by_role("button", name="Share").click(timeout=8_000)

        # The "Scheduling" spinner modal runs the ACTUAL upload+processing -
        # closing the browser during it loses the post (verified 2026-08-16:
        # a whole batch silently vanished). Wait until it disappears, up to
        # 6 minutes, then confirm on the scheduled-content calendar.
        page.wait_for_timeout(4_000)
        for _ in range(72):
            if not page.get_by_text("Scheduling", exact=True).count():
                break
            page.wait_for_timeout(5_000)
        page.wait_for_timeout(3_000)
        page.screenshot(path=args.shot.replace(".png", "-after.png"))

        # Source-of-truth verification: the reel must appear on the calendar.
        # The calendar shows ONE WEEK at a time and opens on the current one,
        # so a post scheduled for a later week reads as ZERO tiles unless we
        # page forward first (2026-08-27: a correctly scheduled post reported
        # calendar_tiles=0 and would have been re-uploaded as a duplicate).
        page.goto("https://www.instagram.com/scheduled_content/",
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(7_000)
        for label in ("Not Now", "Not now"):
            try:
                page.get_by_role("button", name=label).first.click(timeout=3_000)
                page.wait_for_timeout(1_000)
                break
            except Exception:  # noqa: BLE001
                continue
        import datetime as _dt2
        target = _dt2.date.fromisoformat(post["date"])
        # Sunday-based week index, matching IG's Sun..Sat columns.
        def _week(d):
            return (d - _dt2.timedelta(days=(d.weekday() + 1) % 7))
        hops = (_week(target) - _week(_dt2.date.today())).days // 7
        for _ in range(max(0, min(hops, 8))):
            try:
                page.locator(
                    "div[role=button]:has(svg[aria-label='Next week'])"
                ).first.click(timeout=6_000)
                page.wait_for_timeout(4_000)
            except Exception:  # noqa: BLE001
                break
        # Verify a tile sits in the TARGET DAY's column, not just that the week
        # has some tiles: a plain count carries over posts from earlier runs and
        # reads as success even when this post never scheduled (2026-08-27 -
        # three posts reported calendar_tiles=2 while scheduling nothing).
        tiles = page.evaluate(
            """(day) => {
              const hdr=[...document.querySelectorAll('*')].find(x =>
                x.children.length===0 &&
                new RegExp('^(Sun|Mon|Tue|Wed|Thu|Fri|Sat) '+day+'$')
                  .test(x.textContent.trim()));
              const imgs=[...document.querySelectorAll('img')]
                .filter(i => i.alt==='Scheduled post thumbnail' &&
                             i.offsetParent!==null);
              if (!hdr) return {day_column: null, week_tiles: imgs.length};
              const hx=hdr.getBoundingClientRect();
              const inCol=imgs.filter(i => {
                const r=i.getBoundingClientRect();
                return Math.abs((r.x+r.width/2)-(hx.x+hx.width/2)) < 70;
              });
              return {day_column: inCol.length, week_tiles: imgs.length};
            }""", target.day)
        page.screenshot(path=args.shot.replace(".png", "-calendar.png"))
        ctx.close()
        # No tile in the target day's column means nothing was scheduled, so say
        # so instead of reporting success. 2026-09-10: a submit 10 minutes ahead
        # of its slot returned calendar_tiles={0,0} and posted NOTHING, and the
        # run was logged as submitted - the caller has to be able to trust this.
        if not tiles.get("day_column"):
            print(json.dumps({"status": "not_scheduled", "clip": args.clip,
                              "date": post["date"], "calendar_tiles": tiles,
                              "hint": "no tile in the target day's column - "
                                      "re-run against a slot further out",
                              "screenshot": args.shot.replace(".png", "-calendar.png")},
                             ensure_ascii=False))
            return 4
        print(json.dumps({"status": "submitted", "clip": args.clip,
                          "date": post["date"], "collaborators": collab_added,
                          "cover_set": cover_set,
                          "calendar_tiles": tiles}, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
