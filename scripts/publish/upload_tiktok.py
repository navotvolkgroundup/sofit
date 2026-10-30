#!/usr/bin/env python3
"""Semi-automated TikTok upload+schedule (M2 of the publish loop).

Fills TikTok Studio's upload form (file, caption, schedule date/time) through
the ws-scraper logged-in Chrome profile, then STOPS: `--dry` saves a
confirmation screenshot and exits WITHOUT posting. Only `--submit` clicks the
Schedule button - the human-confirm gate is the design, not a courtesy.
In `--dry` every Schedule/Post button is disabled in the page. Scheduling
picks Asia/Jerusalem, then the date and time, and stops if that readback
disagrees. `--submit` is refused unless the video upload has finished, every
planned hashtag committed, and the caption body is in the editor. The footer
Schedule button is found by its visible text when the content check has not
exposed the accessible name "Schedule". After scheduling, the content list
must show the expected date.

Chrome's `--disable-quic` flag is on unless `SOFIT_TT_NOQUIC=0`. It is a
modest help for "Couldn't upload" failures (about 31% to 42% success per
attempt), not a fix.

    upload_tiktok.py --clip clip-12 --plan publish-plan.json --dry
    upload_tiktok.py --clip clip-12 --plan publish-plan.json --submit
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import showhome

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"
PROFILE = showhome.browser_profile()

def _publish_cfg() -> dict:
    """Machine-local account config (~/.sofit/publish.json) - the code is
    public OSS, the account details are not. IMPORT-SAFE by design: a missing
    file returns {} with a warning (CI collects these modules on machines
    with no config); each script validates the keys it needs at RUN time."""
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path
    p = showhome.home() / "publish.json"
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"warn: missing/invalid {p} - running with defaults "
              '({"ig_profile", "ig_collaborators", "clips_dir", "plans_dir"})',
              file=_sys.stderr)
        return {}

_CFG = _publish_cfg()
CLIPS_DIR = Path(_CFG.get("clips_dir", "~/Downloads")).expanduser()

# TikTok Studio's schedule controls are English. calendar.month_name and
# strftime follow the process locale, which would miss the readback on a
# machine that is not set to English.
_EN_MONTHS = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_EN_MONTH_ABBR = (
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_MENTION = re.compile(r"(?<![\w.@])@([A-Za-z0-9_.]{2,24}[A-Za-z0-9_])")
_BIDI = re.compile("[\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069]")
# Exact visible label of the footer submit button. Anchored so a longer
# label (the schedule radio's container, a tour step) does not match.
_SCHEDULE_BUTTON_TEXT = re.compile(r"^\s*Schedule\s*$")


def split_tags(caption: str) -> tuple[str, list[str]]:
    """(body, hashtag tokens). Trailing hashtag-only lines come off the
    end so they can be typed rather than pasted."""
    lines = caption.split("\n")
    tags: list[str] = []
    while lines and lines[-1].strip().startswith("#"):
        tags = lines.pop().split() + tags
    return "\n".join(lines).rstrip(), tags


def split_mentions(body: str) -> list[tuple[str, str]]:
    """[("text", run) | ("mention", handle)] in caption order."""
    out: list[tuple[str, str]] = []
    pos = 0
    for match in _MENTION.finditer(body):
        if match.start() > pos:
            out.append(("text", body[pos:match.start()]))
        out.append(("mention", match.group(1)))
        pos = match.end()
    if pos < len(body):
        out.append(("text", body[pos:]))
    return out


def ordinal_day(day: int) -> str:
    """27 -> '27th', the suffix on the day button's aria-label."""
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def tiktok_date_targets(date_iso: str) -> tuple[str, str, str]:
    """(month title, day aria-label ending, combobox text).

    '2026-10-27' -> ('October 2026', 'October 27th, 2026', 'Oct 27, 2026').
    The day label is matched against the end of
    'Tuesday, October 27th, 2026'.
    """
    year_s, month_s, day_s = date_iso.split("-")
    year, month, day = int(year_s), int(month_s), int(day_s)
    month_name = _EN_MONTHS[month]
    return (
        f"{month_name} {year}",
        f"{month_name} {ordinal_day(day)}, {year}",
        f"{_EN_MONTH_ABBR[month]} {day}, {year}",
    )


def strip_bidi(text: str) -> str:
    """Drop bidi marks and collapse whitespace. Plan captions carry marks
    the content list and the editor readback do not."""
    return " ".join(_BIDI.sub("", text or "").split())


def caption_url_needle(caption: str, words: int = 5) -> str:
    """First words of a caption, for matching a row on the content list."""
    return " ".join(_BIDI.sub("", caption or "").split()[:words])


def body_in_editor(body: str, editor_text: str) -> bool:
    return strip_bidi(body) in strip_bidi(editor_text)


def schedule_readback_ok(shown_date: str, shown_time: str, shown_zone: str,
                         date_iso: str, time_local: str) -> bool:
    """Hard stop unless the three comboboxes show the plan.

    The zone label includes the GMT offset for today ('(GMT+2) Jerusalem'
    after the October DST change). Any label containing Jerusalem is the
    Asia/Jerusalem zone.
    """
    _title, _day, want_date = tiktok_date_targets(date_iso)
    return (shown_date == want_date and shown_time == time_local
            and "Jerusalem" in (shown_zone or ""))


def tiktok_dry_or_refuse(*, dry: bool, upload_broken: bool,
                         upload_complete: bool) -> tuple[str, int] | None:
    """(status, exit code) to stop, or None to click Schedule.

    --dry never submits. A real submit is refused unless the file finished
    uploading. upload_broken is the dry-only form check after a failed upload.
    """
    if dry:
        status = "dry_ok_form_only_upload_failed" if upload_broken else "dry_ok"
        return status, 0
    if not upload_complete:
        return "refused_upload_incomplete", 6
    return None


def chrome_launch_args(environ: dict[str, str] | None = None) -> list[str]:
    """Chrome flags for the persistent upload context.

    ``--disable-quic`` is on unless ``SOFIT_TT_NOQUIC`` is set to a value
    other than ``1``. It is a modest help for TikTok's "Couldn't upload"
    chunk failures (about 31% to 42% success per attempt), not a fix.
    """
    env = os.environ if environ is None else environ
    args = ["--disable-blink-features=AutomationControlled"]
    if env.get("SOFIT_TT_NOQUIC", "1") == "1":
        args.append("--disable-quic")
    return args


def tiktok_submit_block(*, n_tags: int, n_want: int, body: str,
                        editor_text: str,
                        missed: list[str] | None = None) -> dict | None:
    """Refuse a submit whose hashtags or caption body did not land.

    Every planned hashtag has to have committed, and the full body
    (including the episode line) has to be in the editor. Returns a status
    payload, or None when both checks pass and the click may proceed.
    """
    if n_tags < n_want:
        return {
            "status": "refused_tags",
            "tags_registered": n_tags,
            "tags_wanted": n_want,
            "tags_missed": list(missed or []),
        }
    if not body_in_editor(body, editor_text):
        return {"status": "refused_body_mismatch"}
    return None


def footer_schedule_via(text_visible: int) -> str:
    """'text' when a Schedule button is visible by its label, else 'role'.

    While "Content check lite" is running, the footer button is not exposed
    under the accessible name "Schedule", so the visible-text lookup is what
    finds it. The accessible-name lookup is the fallback when that text
    match is not on screen.
    """
    return "text" if text_visible else "role"


def footer_schedule_ready(visible: int, enabled: bool) -> bool:
    """The submit click needs exactly one visible, enabled Schedule button."""
    return visible == 1 and enabled


def tiktok_list_stamp(date_iso: str, time_local: str) -> str:
    """Scheduled-list stamp, e.g. 'Oct 27, 8:00 PM'.

    English month abbreviations and AM/PM. ``calendar.month_abbr`` and
    ``strftime`` follow the process locale and would miss the row on a
    machine that is not set to English.
    """
    _year_s, month_s, day_s = date_iso.split("-")
    month, day = int(month_s), int(day_s)
    hour, minute = (int(part) for part in time_local.split(":"))
    return (f"{_EN_MONTH_ABBR[month]} {day}, "
            f"{hour % 12 or 12}:{minute:02d} {'PM' if hour >= 12 else 'AM'}")


def tiktok_list_row_has_stamp(row_text: str, date_iso: str, time_local: str) -> bool:
    """The content-list row text includes the expected scheduled stamp."""
    return tiktok_list_stamp(date_iso, time_local) in strip_bidi(row_text)


def tiktok_submit_status(row_ok: bool, row_date_ok: bool) -> str:
    """Confirmed only when the content list shows this clip on the planned date."""
    return "submitted" if row_ok and row_date_ok else "submitted_unverified"


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

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True, channel="chrome",
            args=chrome_launch_args(),
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
        if args.dry and os.environ.get("SOFIT_TT_FORM_ONLY"):
            # DRY form-only mode: the "Couldn't upload" modal steals focus from
            # the caption editor; drop it so the form can still be exercised.
            page.evaluate("()=>document.querySelectorAll('.TUXModal-overlay').forEach(o=>{const p=o.closest('[data-floating-ui-portal]')||o; if(/upload/i.test(p.innerText)) p.remove();})")
        if args.dry:
            page.evaluate("""() => { const block = () => document.querySelectorAll('button').forEach(b => {
              const t=(b.innerText||'').trim(); if (['Schedule','Post','Post now'].includes(t)) {
              b.disabled=true; b.style.pointerEvents='none'; b.setAttribute('data-sofit-blocked','1'); } });
              block(); if (!window.__sofitGuard) { window.__sofitGuard=new MutationObserver(block);
              window.__sofitGuard.observe(document.body,{subtree:true,childList:true,characterData:true}); } }""")

        def upload_failed() -> bool:
            return bool(page.get_by_text("Couldn't upload").count() or
                        page.get_by_text("Couldn\u2019t upload").count())

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
        missed: list[str] = []   # which ones the suggestion panel never offered
        missed_mentions: list[str] = []   # @handles with no exact suggestion row

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
                        // 2026-10-22: brand-new / rare tags show no "N posts"
                        // count, so don't require one - the exact first-line
                        // check below still guards against near-misses.
                        return t.startsWith('#' + want) && t.length < want.length + 60;
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

        def refocus_editor() -> None:
            # Clicking a suggestion row moves focus to the popover; DraftJS keeps
            # its own selection (right after the inserted entity), so focusing
            # the editor again resumes there. Without this the next
            # insert_text is silently dropped (probe 2026-10-08).
            page.evaluate(
                "document.querySelector('div.public-DraftEditor-content').focus()")
            page.wait_for_timeout(300)

        def commit_mention(handle: str) -> bool:
            """Type '@handle' and click the suggestion row whose user-id is
            EXACTLY handle. Must be a real (Playwright) click: the mention
            plugin selects on mousedown/mouseup, and a JS .click() leaves plain
            text with no entity. Rows (dumped 2026-10-08) are
            .mention-suggestion-item[role=option] with
            .user-id = 'tortsuk · 52 follower'."""
            page.keyboard.type("@" + handle, delay=80)
            find = """(h) => {
                const rows = [...document.querySelectorAll(
                    '.mention-list-popover .mention-suggestion-item')];
                return rows.find(r => ((r.querySelector('.user-id') || {})
                    .innerText || '').split(' \u00b7 ')[0].trim() === h) || null;
            }"""
            row = None
            for _ in range(30):
                page.wait_for_timeout(250)
                row = page.evaluate_handle(find, handle).as_element()
                if row:
                    break
            if not row:
                page.keyboard.press("Escape")
                refocus_editor()
                return False
            try:
                row.click(timeout=3_000)
            except Exception:  # noqa: BLE001
                refocus_editor()
                return False
            page.wait_for_timeout(800)
            refocus_editor()
            return True

        def mention_entities() -> list[str]:
            """Read-back: handles that exist as real mention ENTITIES in the
            editor's DraftJS state (the DOM looks the same for plain text and a
            linked mention, so the DOM can't be trusted for this)."""
            return page.evaluate("""() => {
                const el = document.querySelector('div.public-DraftEditor-content');
                if (!el) return [];
                const fk = Object.keys(el).find(k => k.startsWith('__reactFiber'));
                let f = fk ? el[fk] : null, es = null;
                for (let n = 0; f && n < 60; n++, f = f.return) {
                    const p = f.memoizedProps;
                    if (p && p.editorState) { es = p.editorState; break; }
                }
                if (!es) return [];
                const c = es.getCurrentContent(), out = [];
                c.getBlockMap().forEach(b => b.findEntityRanges(
                    ch => ch.getEntity() !== null,
                    (s) => { const en = c.getEntity(b.getEntityAt(s));
                             const d = (en.getData() || {}).mention || {};
                             if (en.getType() === 'mention' && d.uid) out.push(d.name); }));
                return out;
            }""")

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
            page.keyboard.press("ControlOrMeta+A")
            page.wait_for_timeout(200)
            page.keyboard.press("Backspace")
            page.wait_for_timeout(400)
            body, tags = split_tags(post["tiktok"])
            # @mentions have to be TYPED and PICKED, exactly like hashtags: a
            # pasted "@tortsuk" is plain text (no mention entity, nobody is
            # tagged). So the body goes in as text runs with each @handle typed
            # and its exact row clicked in between (2026-10-08).
            missed_mentions.clear()
            after_mention = False
            for kind, chunk in split_mentions(body):
                if kind == "mention":
                    if not commit_mention(chunk):
                        missed_mentions.append(chunk)
                    after_mention = True
                elif after_mention:
                    # After a mention entity a multi-line insert_text keeps only
                    # its LAST line (probe 2026-10-08), so go line by line with
                    # a real Enter between lines. The plugin already appended a
                    # space after the mention, so drop the run's leading one.
                    if chunk.startswith(" "):
                        chunk = chunk[1:]
                    for i, line in enumerate(chunk.split("\n")):
                        if i:
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(150)
                        if line:
                            if method == "insert":
                                page.keyboard.insert_text(line)
                            else:
                                page.keyboard.type(line, delay=40)
                            page.wait_for_timeout(300)
                elif method == "insert":
                    page.keyboard.insert_text(chunk)
                else:  # slow typing fallback
                    page.keyboard.type(chunk, delay=40)
            page.wait_for_timeout(1_200)
            # Hashtags have to be TYPED. insert_text lands as a paste, so DraftJS
            # never fires its '#' handler and the tag stays plain text - verified
            # 2026-09-25 on two live posts, whose tags sat in a bare <span> while
            # another creator's Hebrew tags were real /tag/ links. Safe to type:
            # the RTL scrambling that forced insert_text applies to the mixed
            # body, not to a single unembedded token.
            nonlocal n_tags
            n_tags = 0
            missed.clear()

            def _caret_to_end() -> None:
                # 2026-10-22: on clips 4/11 the tag keystrokes landed nowhere
                # (no '#' text at all in the editor) - focus had moved off the
                # DraftJS editor (upload status/checks re-render). Put focus
                # and the caret back at the END before typing.
                refocus_editor()
                page.keyboard.press("ControlOrMeta+End")
                page.wait_for_timeout(200)

            def _editor_has(txt: str) -> bool:
                return txt in (cap.inner_text() or "")

            _caret_to_end()
            for tag in tags:
                if not page.evaluate("() => !!(document.activeElement && document.activeElement.closest && document.activeElement.closest('.public-DraftEditor-content'))"):
                    _caret_to_end()
                page.keyboard.type("\n\n" if tag is tags[0] else " ")
                page.keyboard.type(tag, delay=60)
                page.wait_for_timeout(400)
                if not _editor_has(tag):
                    # Keystrokes were lost (NOT a failed pick - nothing was typed),
                    # so retyping is safe here; it never follows an Escape.
                    print(f"info: {tag} keystrokes lost, refocus+retype", file=sys.stderr)
                    _caret_to_end()
                    page.keyboard.type(("\n\n" if tag is tags[0] and not _editor_has(tags[0]) else " ") + tag, delay=60)
                    page.wait_for_timeout(400)
                # Flaky: the same clip scored 3/3 then 0/3 an hour later, and a
                # batch came out 10/15. A retype-and-retry loop made it WORSE
                # (0/3 every time) - the Escape that ends a failed pick leaves
                # the editor in a state retyping does not recover. Left as the
                # simple wait until someone can watch this headful.
                # Was a flat 1.2s sleep. The panel's latency varies, so wait
                # for the ROW to exist instead of for the clock - the same
                # stale-read that made the calendar overshoot. This does not
                # touch the failed-pick path, which is what the earlier
                # retype-and-retry attempt broke.
                want_row = tag.lstrip("#").strip()
                for _ in range(20):
                    page.wait_for_timeout(200)
                    if page.evaluate("""(want) => {
                          const pop=document.querySelector('.mention-list-popover');
                          if (!pop) return false;
                          return [...pop.querySelectorAll('*')].some(e => {
                            const t=(e.innerText||'').trim();
                            return t.split('\\n')[0].trim()==='#'+want;});
                        }""", want_row):
                        break
                if commit_tag(tag):
                    n_tags += 1
                else:
                    missed.append(tag)
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

        # Mentions are NOT report-and-continue: an @handle that didn't become a
        # linked mention entity fails the run (dry or submit) before scheduling.
        want_mentions = [h for k, h in split_mentions(split_tags(post["tiktok"])[0])
                         if k == "mention"]
        if want_mentions:
            got_mentions = mention_entities()
            unlinked = [h for h in want_mentions if h not in got_mentions]
            # Also make sure the multi-step insert didn't drop any body text.
            body_ok = body_in_editor(split_tags(post["tiktok"])[0], cap.inner_text())
            if unlinked or not body_ok:
                # One clean redo: the body run can be cut short when TikTok
                # re-renders mid-insert (e.g. the upload-failed modal).
                print("info: caption check failed, redoing caption once", file=sys.stderr)
                purge_tour()
                set_caption("insert")
                got_mentions = mention_entities()
                unlinked = [h for h in want_mentions if h not in got_mentions]
                body_ok = body_in_editor(split_tags(post["tiktok"])[0], cap.inner_text())
            if unlinked or not body_ok:
                page.screenshot(path=args.shot)
                print(json.dumps({"status": "mention_not_linked" if unlinked else "caption_body_mismatch",
                                  "mentions_wanted": want_mentions,
                                  "mentions_linked": got_mentions,
                                  "mentions_no_row": missed_mentions,
                                  "body_ok": body_ok,
                                  "screenshot": args.shot}, ensure_ascii=False))
                ctx.close()
                return 7
            print(f"mentions linked: {' '.join('@' + h for h in got_mentions)}",
                  file=sys.stderr)

        # Dry runs report a short hashtag count and continue, so the form can
        # still be reviewed. --submit refuses (tiktok_submit_block) when any
        # planned tag is missing or the caption body is not in the editor.
        # This count is suggestion-panel picks, not a readback - the composer
        # gives no signal. Confirm on the LIVE post (its tags should be
        # <a href="/tag/...">), not from a dry run.
        n_want = len(split_tags(post["tiktok"])[1])
        if n_tags < n_want:
            print(f"warn: only {n_tags}/{n_want} hashtags committed from the "
                  f"suggestion panel; missed: {' '.join(missed) or '?'}",
                  file=sys.stderr)

        # Schedule: pick the radio, then fill date+time inputs.
        purge_tour()

        GUARD_JS = """() => {
          const block = () => document.querySelectorAll('button').forEach(b => {
            const t = (b.innerText || '').trim();
            if (['Schedule', 'Post', 'Post now'].includes(t)) {
              b.disabled = true; b.style.pointerEvents = 'none'; b.setAttribute('data-sofit-blocked', '1'); }
          });
          block();
          if (!window.__sofitGuard) { window.__sofitGuard = new MutationObserver(block);
            window.__sofitGuard.observe(document.body, {subtree: true, childList: true, characterData: true}); }
        }"""
        if args.dry:
            # DRY RUN SAFETY (2026-10-22): the old js fallback below clicked the
            # LAST visible "Schedule" leaf - the footer submit button - and a dry
            # run scheduled a real post. In --dry every Schedule/Post button is
            # disabled at the DOM level for the whole session.
            page.evaluate(GUARD_JS)

        def pick_schedule() -> bool:
            # Only ever touch the RADIO. Never a generic "Schedule" text match:
            # the footer submit button carries the same text.
            for attempt in ("radio", "label"):
                try:
                    if attempt == "radio":
                        page.get_by_role("radio", name="Schedule").check(force=True, timeout=4_000)
                    else:
                        page.evaluate("""() => { const r=[...document.querySelectorAll('input[type=radio]')]
                          .find(i => (i.closest('label')||i.parentElement).innerText.trim()==='Schedule');
                          if (r && !r.closest('button')) (r.closest('label')||r).click(); }""")
                    page.wait_for_timeout(1_500)
                    if page.evaluate("""() => [...document.querySelectorAll('input[type=radio]')]
                          .some(i => i.checked && (i.closest('label')||i.parentElement).innerText.trim()==='Schedule')"""):
                        return True
                except Exception:  # noqa: BLE001
                    purge_tour()
            return False

        def upload_done() -> bool:
            return not page.get_by_text("Checks can only start after the file is uploaded").count()

        def retry_upload_inline(rounds: int = 2) -> bool:
            # 2026-10-22: TikTok's chunk upload to tos-*.tiktokcdn.com fails
            # intermittently ("Couldn't upload"). The card has its own Retry
            # button (NOT a submit); use it in place with spacing, rather than
            # re-running the whole page.
            for r in range(rounds):
                if not upload_failed() and not page.get_by_text("Upload failed").count():
                    break
                page.evaluate("()=>document.querySelectorAll('.TUXModal-overlay').forEach(o=>{const p=o.closest('[data-floating-ui-portal]')||o; if(/upload/i.test(p.innerText)) p.remove();})")
                page.wait_for_timeout(20_000 + 20_000 * r)
                try:
                    page.locator("button:has-text('Retry')").first.click(timeout=5_000)
                except Exception:  # noqa: BLE001
                    break
                print(f"info: upload retry {r + 1}", file=sys.stderr)
                for _ in range(60):
                    page.wait_for_timeout(3_000)
                    if upload_failed() or page.get_by_text("Upload failed").count():
                        break
                    if upload_done():
                        break
            for _ in range(20):
                if upload_done() or upload_failed():
                    break
                page.wait_for_timeout(3_000)
            return upload_done() and not upload_failed()

        if (upload_failed() or page.get_by_text("Upload failed").count()):
            if retry_upload_inline():
                print("info: upload recovered via in-page Retry", file=sys.stderr)
        upload_broken = False
        if upload_failed() and (args.dry and os.environ.get("SOFIT_TT_FORM_ONLY")):
            # DRY ONLY: TikTok's chunk upload failed ("Couldn't upload"), but the
            # form still renders behind the modal. Drop the modal so the
            # scheduling fields can be exercised; result is flagged.
            upload_broken = True
            page.evaluate("()=>document.querySelectorAll('.TUXModal-overlay').forEach(o=>{const p=o.closest('[data-floating-ui-portal]')||o; if(/upload/i.test(p.innerText)) p.remove();})")
            page.keyboard.press("Escape"); page.wait_for_timeout(800)
        elif upload_failed():
            page.screenshot(path=args.shot)
            print(json.dumps({"status": "upload_failed", "screenshot": args.shot}))
            ctx.close()
            return 6
        if not pick_schedule():
            page.screenshot(path=args.shot)
            print(json.dumps({"status": "schedule_toggle_failed",
                              "screenshot": args.shot}))
            ctx.close()
            return 4
        page.wait_for_timeout(1_500)

        # ---- New scheduling UI (TikTok Studio, seen 2026-10-22) ----------
        # Three comboboxes in [data-e2e=schedule_container]:
        #   date  div[role=combobox][aria-label="Select date"]  -> "Oct 27, 2026"
        #   time  div[role=combobox][aria-label="Select time"]  -> "20:00"
        #   zone  button[role=combobox] w/ input[placeholder=Search]
        #         default "(GMT+3) Local Time"; search offers option
        #         data-value='"Asia/Jerusalem"'.
        # Order matters: zone FIRST (switching zones can re-express the time),
        # then date, then time, then read all three back.
        # Asia/Jerusalem is an IANA zone, so TikTok applies Israel's DST
        # switch (GMT+3 -> GMT+2 on 2026-10-25) itself; the label shows the
        # offset for TODAY, the post lands at 20:00 Israel local on its date.
        SC = "[data-e2e=schedule_container]"
        DATE_CB = f"{SC} [role=combobox][aria-label='Select date']"
        TIME_CB = f"{SC} [role=combobox][aria-label='Select time']"
        TZ_CB = f"{SC} button[role=combobox]"
        TZ_IN = f"{SC} input[placeholder=Search]"
        _norm = lambda s: re.sub(r"\s+", " ", s or "").strip()

        def fail(status: str, **kw) -> int:
            page.screenshot(path=args.shot.replace(".png", f"-{status}.png"), full_page=True)
            ctx.close()
            print(json.dumps({"status": status, "clip": args.clip, **kw}, ensure_ascii=False))
            return 5

        def close_pop():
            page.keyboard.press("Escape"); page.wait_for_timeout(500)

        def read_fields() -> tuple[str, str, str]:
            d = _norm(page.locator(DATE_CB).first.inner_text())
            t = _norm(page.locator(TIME_CB).first.inner_text())
            z = page.locator(TZ_IN).first.input_value() if page.locator(TZ_IN).count() else ""
            return d, t, _norm(z)

        # 1) Time zone -> Asia/Jerusalem
        tz_ok = False
        for _try in range(3):
            purge_tour()
            page.locator(TZ_CB).first.click()
            page.wait_for_timeout(700)
            page.locator(TZ_IN).first.fill("Jerusalem")
            opt = page.locator("[role=option][data-value='\"Asia/Jerusalem\"']")
            try:
                opt.first.wait_for(state="visible", timeout=5_000)
                opt.first.click()
            except Exception:  # noqa: BLE001
                close_pop(); continue
            page.wait_for_timeout(800)
            if "Jerusalem" in read_fields()[2]:
                tz_ok = True; break
            close_pop()
        if not tz_ok:
            return fail("tz_not_set", shown=read_fields()[2])

        # 2) Date
        want_month, day_label, want_date = tiktok_date_targets(post["date"])
        date_ok = False
        for _try in range(3):
            purge_tour()
            page.locator(DATE_CB).first.click()
            try:
                page.wait_for_selector(".DatePicker__panel", timeout=5_000)
            except Exception:  # noqa: BLE001
                close_pop(); continue
            for _ in range(14):
                head = _norm(page.locator(".DatePicker__title").first.inner_text())
                if head == want_month:
                    break
                page.locator(".DatePicker__panel button[aria-label='Next month']").first.click()
                for _w in range(15):           # wait for the header to CHANGE
                    page.wait_for_timeout(200)
                    if _norm(page.locator(".DatePicker__title").first.inner_text()) != head:
                        break
            cell = page.locator(f".DatePicker__panel button.DatePicker__day[aria-label$='{day_label}']"
                                f"[data-outside='false']:not([disabled])")
            if not cell.count():
                close_pop(); continue
            cell.first.click()
            page.wait_for_timeout(800)
            if page.locator(".DatePicker__panel").count() and page.locator(".DatePicker__panel").first.is_visible():
                close_pop()
            if read_fields()[0] == want_date:
                date_ok = True; break
        if not date_ok:
            return fail("date_not_set", want=want_date, shown=read_fields()[0])

        # 3) Time: two option columns, labelled "Hours HH" / "Minutes MM" (5-min steps)
        time_ok = False
        for _try in range(3):
            purge_tour()
            page.locator(TIME_CB).first.click()
            try:
                page.wait_for_selector(".TimePicker__panel", timeout=5_000)
            except Exception:  # noqa: BLE001
                close_pop(); continue
            for lab in (f"Hours {hh}", f"Minutes {mm}"):
                b = page.locator(f".TimePicker__panel button[role=option][aria-label='{lab}']").first
                b.scroll_into_view_if_needed(timeout=3_000)
                b.click()
                page.wait_for_timeout(500)
            close_pop()
            if read_fields()[1] == plan["time_local"]:
                time_ok = True; break
        if not time_ok:
            return fail("time_not_set", want=plan["time_local"], shown=read_fields()[1])

        # Final readback - HARD STOP on any mismatch (scheduled posts can't be edited).
        sd, st, sz = read_fields()
        if not schedule_readback_ok(sd, st, sz, post["date"], plan["time_local"]):
            return fail("readback_mismatch", shown=f"{sd} {st} {sz}",
                        wanted=f"{want_date} {plan['time_local']} Jerusalem")
        sched_date, sched_time = post["date"], st
        # Evidence shot: caption (with mention chips) AND the schedule fields in
        # one frame - grow the viewport, put the caption at the top.
        try:
            page.set_viewport_size({"width": 1500, "height": 1900})
            page.wait_for_timeout(600)
            page.evaluate("()=>document.querySelector('div.public-DraftEditor-content').scrollIntoView({block:'start'})")
            page.evaluate("()=>window.scrollBy(0,-120)")
        except Exception as e:  # noqa: BLE001
            print("warn: evidence viewport", e, file=sys.stderr)
        page.wait_for_timeout(800)

        # Don't call a dry run done while the video is still uploading.
        for _ in range(40):
            if upload_done() or upload_failed():
                break
            page.wait_for_timeout(3_000)
        upload_complete = upload_done() and not upload_failed() and not upload_broken
        page.screenshot(path=args.shot)
        gate = tiktok_dry_or_refuse(
            dry=args.dry, upload_broken=upload_broken, upload_complete=upload_complete)
        if args.dry:
            linked = mention_entities()
            ctx.close()
            print(json.dumps({"status": gate[0], "screenshot": args.shot,
                              "clip": args.clip, "scheduled_date": sched_date,
                              "scheduled_time": sched_time, "shown": f"{sd} {st} {sz}",
                              "tags_registered": n_tags, "tags_wanted": n_want, "tags_missed": missed,
                              "upload_complete": upload_complete, "mentions_linked": linked}))
            return gate[1]

        # Never submit a post whose video didn't finish uploading.
        if gate is not None:
            ctx.close()
            print(json.dumps({"status": gate[0], "clip": args.clip}))
            return gate[1]
        # 2026-10-23 pre-submit gates: every planned hashtag committed, the
        # full body (incl. the episode line) present, and exactly ONE enabled
        # footer Schedule button. Any miss = refuse, nothing is submitted.
        blocked = tiktok_submit_block(
            n_tags=n_tags, n_want=n_want,
            body=split_tags(post["tiktok"])[0],
            editor_text=cap.inner_text(), missed=missed)
        if blocked is not None:
            page.screenshot(path=args.shot.replace(".png", "-refused.png"))
            ctx.close()
            print(json.dumps({**blocked, "clip": args.clip}, ensure_ascii=False))
            return 7
        purge_tour()
        # By visible TEXT, outside the schedule settings box: while "Content
        # check lite" is still running the footer button is not exposed under
        # the accessible name "Schedule" (clip-11/clip-4, 2026-10-23).
        _all = page.locator("button").filter(has_text=_SCHEDULE_BUTTON_TEXT).filter(
            has_not=page.locator("[data-e2e=schedule_container]"))
        _text_visible = sum(
            1 for i in range(_all.count()) if _all.nth(i).is_visible())
        if footer_schedule_via(_text_visible) == "role":
            _all = page.get_by_role("button", name="Schedule", exact=True)
        print("info: footer Schedule buttons:", _all.count(), file=sys.stderr)
        _vis = [_all.nth(i) for i in range(_all.count()) if _all.nth(i).is_visible()]
        _n_all = _all.count()
        _enabled = len(_vis) == 1 and _vis[0].is_enabled()
        if not footer_schedule_ready(len(_vis), _enabled):
            page.screenshot(path=args.shot.replace(".png", "-refused.png"))
            ctx.close()
            print(json.dumps({"status": "refused_schedule_button", "clip": args.clip,
                              "visible": len(_vis), "total": _n_all}))
            return 7
        _vis[0].click()
        page.wait_for_timeout(4_000)
        # "We're still checking your video... continue?" - confirm; the
        # content check keeps running server-side after scheduling.
        try:
            pn = page.get_by_role("button", name="Post now")
            if pn.count():
                try:
                    print("info: confirm dialog:", page.evaluate(
                        "()=>[...document.querySelectorAll('[role=dialog]')]"
                        ".map(d=>d.innerText.slice(0,300)).join(' || ')"),
                          file=sys.stderr)
                except Exception:  # noqa: BLE001
                    pass
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
        # Bidi marks (RLE/PDF/LRE/RLO/LRO, RLM/LRM/ALM, isolates U+2066-2069) are
        # in the plan caption (rtl_caption) but NOT in the content-list text, so
        # strip them - and collapse whitespace - on BOTH sides before matching.
        # The needle is the caption's first words. `want_head` is only the
        # first token, and it used to be overwritten with the month title.
        url_needle = caption_url_needle(post["tiktok"])
        find_url = """(head) => {
              const norm = s => (s || "")
                .replace(/[\\u200e\\u200f\\u061c\\u202a-\\u202e\\u2066-\\u2069]/g, "")
                .replace(/\\s+/g, " ").trim();
              const want = norm(head);
              for (const a of document.querySelectorAll("a[href*='/video/']")) {
                let r=a;
                for (let i=0;i<6&&r;i++){ r=r.parentElement;
                  if (r && r.innerText && r.innerText.length>60) break; }
                if (r && norm(r.innerText).includes(want)) return a.href;
              }
              return "";
            }"""
        # The list can lag a few seconds behind the submit, so give it a couple
        # of reloads before giving up.
        post_url = ""
        for attempt in range(3):
            post_url = page.evaluate(find_url, url_needle)
            if post_url:
                break
            page.wait_for_timeout(4_000)
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(5_000)
        row_ok = bool(post_url)
        # The row has to show the date we scheduled, in the studio's English
        # stamp ("Oct 27, 8:00 PM"). A caption match on the wrong day is not
        # a confirmed schedule.
        want_row = tiktok_list_stamp(post["date"], plan["time_local"])
        row_txt = page.evaluate("""(head) => {
              const norm = s => (s || "").replace(/[\\u200e\\u200f\\u061c\\u202a-\\u202e\\u2066-\\u2069]/g, "").replace(/\\s+/g, " ").trim();
              const want = norm(head);
              for (const a of document.querySelectorAll("a[href*='/video/']")) {
                let r=a; for (let i=0;i<8&&r;i++){ r=r.parentElement;
                  if (r && /\\b[A-Z][a-z]{2} \\d{1,2}, \\d{1,2}:\\d{2} [AP]M/.test(r.innerText||"")) break; }
                if (r && norm(r.innerText).includes(want)) return norm(r.innerText);
              }
              return ""; }""", url_needle)
        row_date_ok = tiktok_list_row_has_stamp(
            row_txt, post["date"], plan["time_local"])
        # NO first-row fallback. It used to take a[href*='/video/'] .first when
        # the caption match failed, and the list is sorted by SCHEDULED date, so
        # that returned a DIFFERENT clip's URL - the pins re-upload came back
        # carrying gordon's (2026-09-30). Only autolog's duplicate check stopped
        # it being written against pins, and that check exists for other reasons.
        # A missing URL is recoverable by hand; a confident wrong one is not.
        page.screenshot(path=args.shot.replace(".png", "-after.png"))
        ctx.close()
        if post_url.startswith("/"):
            post_url = "https://www.tiktok.com" + post_url
        import autolog
        out = {"status": tiktok_submit_status(row_ok, row_date_ok),
               "row_date_want": want_row, "row_date_ok": row_date_ok,
               "clip": args.clip, "date": post["date"], "post_url": post_url,
               "tags_registered": n_tags, "tags_wanted": n_want}
        if missed:
            out["tags_missed"] = missed
        # Log here, not in a later step: a batch once reported 20/20 submitted
        # and logged none of it (2026-09-17).
        if post_url:
            out.update(autolog.log(args.plan, args.clip, "tiktok", post_url))
        else:
            out["autolog"] = "skipped"
            out["note"] = ("no row matched this caption - the post may still "
                           "exist; check the scheduled list and log it by hand")
        print(json.dumps(out, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
