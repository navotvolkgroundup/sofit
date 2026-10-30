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

`--clip episode` is the full long-form episode ({episode}_episode.mp4): it
also gets the 16:9 thumbnail ("thumbnail" key, default
{episode}_episode-cover.jpg). --submit refuses it unless seo.check passes,
the schedule read back, the Tags counter is at least ~80% of the planned
text, and the thumbnail landed. A dry run reports those same fields and is
not `dry_ok` when submit would refuse. The browser stays open until the
upload and the checks have finished; a Short is unchanged.
"""
import argparse
import json
import re
import sys
from datetime import datetime
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
CLIPS_DIR = Path(_CFG.get("clips_dir", "~/Downloads")).expanduser()


def _ampm(hhmm: str) -> str:
    """'20:30' -> '8:30 PM' (YouTube's time-of-day list format)."""
    return datetime.strptime(hhmm, "%H:%M").strftime("%-I:%M %p")


def _date_label(iso: str) -> str:
    """'2026-08-23' -> 'Aug 23, 2026' (datepicker input format)."""
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d, %Y")


# The upload context advertises a Mac user-agent so Studio does not serve the
# HeadlessChrome wall. On a Linux host that makes Ctrl+A a no-op, and Meta+A
# is not a key there, so the auto-filled filename survived and the title was
# appended to it. Select in the DOM instead of sending a chord.
SELECT_TEXTBOX_JS = (
    "el => { el.focus(); const r = document.createRange();"
    " r.selectNodeContents(el); const s = getSelection();"
    " s.removeAllRanges(); s.addRange(r); }"
)
SELECT_INPUT_JS = "el => el.select()"


# Shorts keep the labels the commit loop has always clicked. A long-form
# episode must not click Publish: that is "public now" when the schedule
# did not stick.
SHORTS_COMMIT_LABELS = ("Schedule", "Save", "Publish")
LONG_FORM_COMMIT_LABELS = ("Schedule", "Save")

# YouTube custom thumbnails must be under 2 MB.
THUMB_MAX_BYTES = 2 * 1024 * 1024
# The tags box commits on commas, and we type at most this many characters
# (the field's counter stops at 500).
TAG_TYPE_CAP = 495
TAG_COMMIT_RATIO = 0.8

_UPLOAD_DONE = re.compile(
    r"(?i)upload complete|finished uploading|"
    r"processing (?:will begin|sd|hd)|(?:sd|hd) version")
_CHECKS_DONE = re.compile(r"(?i)checks complete|check complete|no issues found")
_STILL_UPLOADING = re.compile(r"(?i)\buploading\b")
_STILL_CHECKING = re.compile(r"(?i)\bchecking\b|running checks")


def commit_labels(long_form: bool) -> tuple[str, ...]:
    return LONG_FORM_COMMIT_LABELS if long_form else SHORTS_COMMIT_LABELS


def tags_committed_enough(planned: str, committed: int, cap: int = TAG_TYPE_CAP) -> bool:
    """True when the Tags counter is not clearly short of what we typed.

    Zero committed tags is a refusal. So is anything under ~80% of the
    planned tag text (or of the 495 characters we actually type, when the
    plan is longer than that). The counter is characters, not chips.
    """
    target = min(len(planned or ""), cap)
    if target <= 0:
        return False
    # Integer comparison for `committed >= target * 0.8` (exactly 80% passes).
    return committed * 5 >= target * 4


def youtube_schedule_readback_ok(review_text: str, time_value: str,
                                 date_iso: str, want_time: str) -> tuple[bool, bool]:
    """(date_ok, time_ok) for a long-form visibility step.

    The month and day without the year is not enough: "Aug 23" matches a
    different year. `want_time` is the Studio list label ("8:30 PM"); the
    time input is accepted too, after the narrow no-break space YouTube
    puts before AM/PM is normalized.
    """
    def norm(s: str) -> str:
        return " ".join((s or "").replace("\u202f", " ").replace("\u00a0", " ").split())

    text = norm(review_text)
    label = norm(_date_label(date_iso))
    date_ok = label in text
    want = norm(want_time)
    time_ok = want in text or norm(time_value) == want
    return date_ok, time_ok


def thumbnail_precheck(exists: bool, size: int,
                       limit: int = THUMB_MAX_BYTES) -> str | None:
    """None when the file may be sent. Otherwise a short reason.

    YouTube rejects a custom thumbnail that is not under 2 MB. An empty
    file is not a thumbnail either.
    """
    if not exists:
        return "missing"
    if size >= limit:
        return "over_2mb"
    if size <= 0:
        return "empty"
    return None


def thumbnail_landed(before_src: str | None, after_src: str | None,
                     component_text: str) -> bool:
    """The thumbnail uploader's image changed, and it did not report an error."""
    if re.search(r"(?i)(couldn.?t upload|upload failed|too (?:large|big)|file is too)",
                 component_text or ""):
        return False
    after = (after_src or "").strip()
    if not after:
        return False
    return after != (before_src or "").strip()


def youtube_progress_flags(text: str) -> tuple[bool, bool, bool]:
    """(upload_complete, checks_complete, still_working) from dialog text.

    "Processing" without "Uploading" means the file has finished transferring.
    Checks stay open while the dialog still says it is checking.
    """
    t = text or ""
    upload_complete = bool(_UPLOAD_DONE.search(t)) or (
        bool(re.search(r"(?i)\bprocessing\b", t)) and not _STILL_UPLOADING.search(t))
    checks_complete = bool(_CHECKS_DONE.search(t))
    uploading = bool(_STILL_UPLOADING.search(t)) and not upload_complete
    checking = bool(_STILL_CHECKING.search(t)) and not checks_complete
    return upload_complete, checks_complete, uploading or checking


def youtube_safe_to_close(text: str, *, saw_upload_complete: bool,
                          saw_checks_complete: bool) -> bool:
    """A long-form submit may close only after the file and the checks are done.

    The share panel ("Video scheduled") often no longer shows those words, so
    a flag recorded earlier on the dialog counts. A panel that is still
    uploading does not, even if an earlier sample looked finished. Absence of
    a progress bar is not evidence: the old 5-minute loop closed on that and
    dropped multi-GB uploads.
    """
    upload_complete, checks_complete, still = youtube_progress_flags(text)
    if still:
        return False
    return ((upload_complete or saw_upload_complete)
            and (checks_complete or saw_checks_complete))


def youtube_long_form_blocker(*, seo_problems: list[str], tags_planned: str,
                              tags_committed: int, date_ok: bool, time_ok: bool,
                              thumbnail_set: bool) -> str | None:
    """Status a long-form submit must refuse with, or None if it may schedule.

    Shorts do not use this. Order matches the submit path: the SEO gate, then
    the schedule readback, then tags, then the thumbnail.
    """
    if seo_problems:
        return "seo_refused"
    if not date_ok or not time_ok:
        return "schedule_readback_refused"
    if not tags_committed_enough(tags_planned, tags_committed):
        return "tags_not_committed"
    if not thumbnail_set:
        return "thumbnail_not_set"
    return None


def youtube_refusal_code(blocker: str | None) -> int:
    """Exit code for a long-form refusal. 0 when there is nothing to refuse.

    SEO and tags stay on 7 (the code the skill already treats as a gate
    refusal). Schedule, thumbnail, and an unfinished upload stay on 6.
    """
    if blocker is None:
        return 0
    if blocker in ("seo_refused", "tags_not_committed"):
        return 7
    return 6


def youtube_dry_decision(long_form: bool, blocker: str | None) -> tuple[str, int]:
    """Dry-run status and exit code.

    A Short is `dry_ok` / 0, as before. A long-form dry run is not ok when
    submit would refuse — an exit code of 0 there used to hide a failed SEO
    gate, an empty tags box, or a thumbnail that never landed.
    """
    if not long_form:
        return "dry_ok", 0
    if blocker is None:
        return "dry_ok", 0
    return "dry_refused", youtube_refusal_code(blocker)


def _dialog_text(page) -> str:
    try:
        dlg = page.locator("ytcp-uploads-dialog")
        if dlg.count():
            return dlg.first.inner_text(timeout=5_000)
    except Exception:  # noqa: BLE001 - the dialog can detach mid-read
        pass
    try:
        return page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""


def _note_progress(page, seen: dict) -> str:
    text = _dialog_text(page)
    upload_done, checks_done, _still = youtube_progress_flags(text)
    seen["upload"] = bool(seen.get("upload")) or upload_done
    seen["checks"] = bool(seen.get("checks")) or checks_done
    if text:
        seen["excerpt"] = text[:300]
    return text


def _wait_for_checks(page, seen: dict, *, rounds: int = 80,
                     pause_ms: int = 15_000) -> bool:
    """Stay on the checks step until it reports done, or the budget runs out."""
    for i in range(rounds):
        _note_progress(page, seen)
        if seen["checks"]:
            return True
        if i % 4 == 0:
            print(f"waiting for checks to finish ({i * pause_ms // 1000}s)",
                  file=sys.stderr)
        page.wait_for_timeout(pause_ms)
    return False


def _wait_for_youtube_settle(page, seen: dict, *, rounds: int,
                             pause_ms: int = 15_000) -> bool:
    """Poll until the file has uploaded and the checks have finished."""
    for i in range(rounds):
        text = _note_progress(page, seen)
        if youtube_safe_to_close(
                text, saw_upload_complete=seen["upload"],
                saw_checks_complete=seen["checks"]):
            return True
        if i % 4 == 0:
            print(f"waiting for the upload and checks to finish "
                  f"({i * pause_ms // 1000}s)", file=sys.stderr)
        page.wait_for_timeout(pause_ms)
    return False


def _upload_not_settled(seen: dict, shot: str, *, scheduled: bool) -> dict:
    why = ("Schedule was not clicked"
           if not scheduled else
           "Schedule may already have been clicked; do not re-submit until "
           "Studio shows this upload failed")
    return {
        "status": "upload_not_settled",
        "screenshot": shot,
        "excerpt": seen.get("excerpt", ""),
        "saw_upload_complete": bool(seen.get("upload")),
        "saw_checks_complete": bool(seen.get("checks")),
        "why": why,
    }


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

    from playwright.sync_api import sync_playwright  # lazy: tests import this file
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

        # A full episode is the search surface (Shorts aren't): it never ships
        # without tags, hashtags and chapters. Dry shows the problems, submit
        # refuses them.
        long_form = args.clip == "episode"
        problems: list[str] = []
        if long_form:
            import seo
            problems = seo.check("youtube", " ".join(post["youtube"].split()),
                                 desc, post.get("youtube_tags", ""))
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
        tb.first.evaluate(SELECT_TEXTBOX_JS)
        page.keyboard.insert_text(title)
        if desc:
            tb.nth(1).click()
            page.keyboard.insert_text(desc)

        # Long-form gets the approved 16:9 episode cover (Shorts have no custom
        # thumbnail). The file input is the one inside the thumbnail uploader.
        # `input#file-loader` is not used: it is unprobed and can be the video
        # file input. A file that is not under 2 MB is not sent.
        thumbnail_set = False
        thumb_note = "skipped"
        if long_form:
            thumb = Path(post.get("thumbnail") or
                         CLIPS_DIR / f"{plan['episode']}_episode-cover.jpg").expanduser()
            thumb_note = thumbnail_precheck(thumb.exists(),
                                            thumb.stat().st_size if thumb.exists() else 0)
            if thumb_note:
                print(f"warn: thumbnail not set ({thumb_note}: {thumb})",
                      file=sys.stderr)
                thumbnail_set = False
            else:
                try:
                    comp = page.locator("ytcp-thumbnails-compact-editor-uploader")
                    file_input = comp.locator("input[type=file]")
                    if comp.count() == 0 or file_input.count() == 0:
                        thumb_note = "uploader_not_found"
                        thumbnail_set = False
                    else:
                        imgs = comp.locator("img")
                        before = imgs.first.get_attribute("src") if imgs.count() else ""
                        file_input.first.set_input_files(str(thumb), timeout=10_000)
                        page.wait_for_timeout(3_000)
                        imgs = comp.locator("img")
                        after = imgs.first.get_attribute("src") if imgs.count() else ""
                        err = ""
                        try:
                            err = comp.inner_text(timeout=3_000)
                        except Exception:  # noqa: BLE001
                            err = ""
                        thumbnail_set = thumbnail_landed(before, after, err)
                        thumb_note = "set" if thumbnail_set else "not_confirmed"
                    if thumbnail_set:
                        print(f"thumbnail: {thumb.name}", file=sys.stderr)
                    else:
                        print(f"warn: thumbnail not set ({thumb_note})",
                              file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    thumbnail_set = False
                    thumb_note = "error"
                    print(f"warn: thumbnail not set ({str(e)[:80]})",
                          file=sys.stderr)

        # Tags live behind "Show more" and were never filled - the pipeline has
        # generated them since 2026-09-24 and they went nowhere (Navot,
        # 2026-09-29). Best effort: a missing tags box must not sink the upload.
        tags = post.get("youtube_tags", "")
        tags_committed = 0
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
                box.type(tags[:TAG_TYPE_CAP], delay=2)
                page.wait_for_timeout(1_500)
                # The counter is the only honest readback: it reflects committed
                # chips, not what we typed.
                got = box.evaluate(
                    "el => el.closest('ytcp-form-input-container').innerText")
                used = re.search(r"(\d+)\s*/\s*500", got)
                n = tags_committed = int(used.group(1)) if used else 0
                if long_form:
                    flag = "" if tags_committed_enough(tags, n) else "  <-- short of the plan"
                else:
                    flag = "" if n > 100 else "  <-- EMPTY, fill them by hand"
                print(f"tags: {n}/500 chars committed{flag}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"warn: tags not set ({str(e)[:80]})", file=sys.stderr)

        page.locator(
            "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']"
        ).click(timeout=15_000)

        # Samples of the dialog, so a later step can remember "upload complete"
        # after the share panel has replaced that text. Shorts don't read it.
        seen = {"upload": False, "checks": False, "excerpt": ""}
        if long_form and args.submit:
            # The third Next leaves the checks step. Wait there first so the
            # "checks complete" line is still on screen to be recorded.
            for i in range(3):
                if i == 2:
                    _wait_for_checks(page, seen)
                page.locator("#next-button").click(timeout=15_000)
                page.wait_for_timeout(1_200)
                _note_progress(page, seen)
        else:
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
        tin.evaluate(SELECT_INPUT_JS)
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
        if long_form:
            # Month and day without the year is the warning above. The
            # refusal uses the full date label, so a wrong year does not pass.
            strict_date, strict_time = youtube_schedule_readback_ok(
                vis_text, tin.input_value(), post["date"], want_time)
            blocker = youtube_long_form_blocker(
                seo_problems=problems, tags_planned=tags,
                tags_committed=tags_committed, date_ok=strict_date,
                time_ok=strict_time, thumbnail_set=thumbnail_set)
            report = {
                "screenshot": args.shot, "clip": args.clip,
                "date": post["date"], "time": want_time,
                "seo_problems": problems, "tags_committed": tags_committed,
                "tags_planned": len(tags or ""), "thumbnail_set": thumbnail_set,
                "thumbnail_note": thumb_note, "date_ok": strict_date,
                "time_ok": strict_time, "would_refuse": blocker,
            }
            if args.dry:
                status, code = youtube_dry_decision(True, blocker)
                print(json.dumps({"status": status, **report}, ensure_ascii=False))
                ctx.close()
                return code
            if blocker:
                print(json.dumps({"status": blocker, **report}, ensure_ascii=False))
                ctx.close()
                return youtube_refusal_code(blocker)
            # A multi-GB episode is still uploading long after the form is
            # filled. Schedule only once the dialog has said the file and the
            # checks are done; closing earlier drops the upload.
            if not _wait_for_youtube_settle(page, seen, rounds=720):
                page.screenshot(path=args.shot, full_page=False)
                print(json.dumps(_upload_not_settled(seen, args.shot, scheduled=False),
                                 ensure_ascii=False))
                ctx.close()
                return 6
        elif args.dry:
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
        labels = list(commit_labels(long_form))
        for _ in range(30):  # up to ~5 min
            page.evaluate(
                """(labels) => { const d=document.querySelector('ytcp-uploads-dialog');
                   if(!d) return;
                   const vis=e=>!!(e.offsetWidth||e.offsetHeight);
                   const b=[...d.querySelectorAll('button')].filter(vis).find(
                     x=>labels.includes(x.innerText.trim()));
                   if(b && !b.disabled) b.click(); }""", labels)
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
        if long_form and not youtube_safe_to_close(
                _dialog_text(page), saw_upload_complete=seen["upload"],
                saw_checks_complete=seen["checks"]):
            # The share panel can hide the progress line. If it is still
            # uploading, keep the browser open instead of dropping the file.
            if not _wait_for_youtube_settle(page, seen, rounds=720):
                page.screenshot(path=args.shot, full_page=False)
                print(json.dumps(_upload_not_settled(seen, args.shot, scheduled=True),
                                 ensure_ascii=False))
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
