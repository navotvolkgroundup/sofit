#!/usr/bin/env python3
"""Schedule a full video episode on Spotify for Creators (the show's host).

Same contract as the other uploaders, over the same ws-scraper Chrome profile:
`--dry` uploads the file, fills the wizard and screenshots WITHOUT publishing;
only `--submit` clicks Schedule. Publish is never clicked. A review step that
would publish now is a refusal, not a button to press.

    upload_spotify.py --login                      # one-time headed Spotify login
    upload_spotify.py --clip episode --plan plan.json --dry
    upload_spotify.py --clip episode --plan plan.json --submit

Plan keys on the post: "spotify_title", "spotify_description" (hashtags go in
the description - Spotify has no per-episode tags field), "date"; plan
"time_local" and "episode" (WS214 -> episode number 214). The video is
{clips_dir}/{episode}_episode.mp4, the same file YouTube gets.

`date` + `time_local` are an Asia/Jerusalem wall clock. They are written into
the inputs as that clock, not converted to UTC (a late-evening slot would
otherwise land on the next day).

--submit is refused unless seo.check passes, the title is not already on the
Episodes list, and the title, description, date and time all read back.
"scheduled for" on the wizard's own schedule step is not a success; the
Episodes list (or an open.spotify.com episode link) has to show the date.

ponytail: selectors are written against the wizard as last seen (labels
Title/Description/Episode number, Schedule option, Next/Schedule footer) and
were NOT probed - the profile had no Spotify login when this was written.
First --dry after --login is the probe; fix whatever the readback flags.
A confirm button labeled Publish, even for a scheduled episode, is left
unclicked on purpose: going live is worse than save_not_confirmed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import seo

PROFILE = Path.home() / ".ws-scraper" / "profile"
DASH = "https://creators.spotify.com/pod/dashboard/episodes"
WIZARD = "https://creators.spotify.com/pod/dashboard/episode/wizard"
JERUSALEM = ZoneInfo("Asia/Jerusalem")

# The commit loop may click these and nothing else. "Publish" goes live now
# when the schedule did not stick, so it is not in the list.
SPOTIFY_COMMIT_LABELS = ("Next", "Schedule")

_BIDI = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_LOGIN_URL = ("accounts.spotify.com", "/login", "login.spotify.com")
_LOGIN_BODY = re.compile(
    r"(?i)(log in to spotify|log in with spotify|continue with google|"
    r"email or username|sign up to spotify|התחברות|התחבר ל)")
# The Episodes dashboard is logged-in-only. A login page on
# creators.spotify.com does not show it. Hebrew UI says פרקים.
_EPISODES = re.compile(r"(?i)\bepisodes\b|פרקים")
# A sentence about what WILL happen, not the "Publish now" radio's label.
# That label stays in the DOM when Schedule is the selected option.
_WILL_PUBLISH_NOW = re.compile(
    r"(?i)("
    r"will be published immediately|"
    r"published immediately|"
    r"publish immediately|"
    r"available immediately|"
    r"will go live now|"
    r"going live now|"
    r"available to (?:listeners|everyone) now|"
    r"יפורסם מיד|זמין מיד"
    r")"
)
_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")
_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


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


def jerusalem_when(date_iso: str, time_local: str) -> datetime:
    """Asia/Jerusalem wall clock for a plan date and `time_local`.

    The clock is not converted. 23:30 stays 23:30, including on a day when
    Jerusalem is UTC+2 and a UTC conversion would still be the same calendar
    day only by luck.
    """
    naive = datetime.strptime(f"{date_iso} {time_local}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=JERUSALEM)


def input_fill_value(input_type: str, when: datetime, kind: str) -> str:
    """Value for Playwright's fill().

    `input[type=date]` throws on `%m/%d/%Y`. `input[type=time]` throws on
    `%I:%M %p`. Both want the ISO / 24-hour form. `datetime-local` wants
    `YYYY-MM-DDTHH:MM`. A text input gets the same unambiguous form so the
    readback can compare it; this function never emits a US locale string.
    """
    if kind not in ("date", "time"):
        raise ValueError(kind)
    kind_of = (input_type or "").lower()
    wall_d = when.strftime("%Y-%m-%d")
    wall_t = when.strftime("%H:%M")
    if kind_of == "datetime-local":
        return f"{wall_d}T{wall_t}"
    if kind == "date":
        return wall_d
    return wall_t


def _same_clock(got: str, when: datetime) -> bool:
    g = " ".join((got or "").replace("\u202f", " ").replace("\u00a0", " ").split())
    hhmm = when.strftime("%H:%M")
    # type=time may read back with seconds (HH:MM:SS).
    if g == hhmm or g.startswith(hhmm + ":"):
        return True
    upper = g.upper()
    compact = upper.replace(" ", "")
    for raw, fmt in ((upper, "%I:%M %p"), (compact, "%I:%M%p")):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.hour == when.hour and parsed.minute == when.minute
    return False


def schedule_inputs_match(got_date: str, got_time: str, when: datetime) -> bool:
    """The date/time inputs actually hold the planned Jerusalem clock.

    `fill()` returning is not this. A type=date input reads back as
    YYYY-MM-DD; a datetime-local input reads back as YYYY-MM-DDTHH:MM.
    """
    gd = (got_date or "").strip()
    gt = (got_time or "").strip()
    iso_d = when.strftime("%Y-%m-%d")
    iso_t = when.strftime("%H:%M")
    if "T" in gd:
        date_part, _, time_part = gd.partition("T")
        return date_part == iso_d and time_part[:5] == iso_t
    if gd != iso_d:
        return False
    return _same_clock(gt, when)


def date_phrases(when: datetime) -> tuple[str, ...]:
    """Spellings a review step might use for this calendar day."""
    d, m, y = when.day, when.month, when.year
    mon, ab = _MONTHS[m - 1], _ABBR[m - 1]
    return (
        f"{y:04d}-{m:02d}-{d:02d}",
        f"{mon} {d}, {y}",
        f"{ab} {d}, {y}",
        f"{d} {mon} {y}",
        f"{d} {ab} {y}",
        f"{m}/{d}/{y}",
        f"{m:02d}/{d:02d}/{y}",
        f"{d}/{m}/{y}",
        f"{d:02d}/{m:02d}/{y}",
        f"{d}.{m}.{y}",
        f"{d:02d}.{m:02d}.{y}",
    )


def clock_forms(when: datetime) -> tuple[str, ...]:
    h12 = when.hour % 12 or 12
    suffix = "AM" if when.hour < 12 else "PM"
    hhmm = f"{when.hour:02d}:{when.minute:02d}"
    h12s = f"{h12}:{when.minute:02d}"
    return (hhmm, f"{h12s} {suffix}", f"{h12s}{suffix}", f"{h12s} {suffix.lower()}")


def _squash(text: str) -> str:
    return " ".join((text or "").replace("\u202f", " ").replace("\u00a0", " ").split())


def date_visible(text: str, when: datetime) -> bool:
    """Some spelling of this calendar day appears in `text`."""
    flat = _squash(text)
    return any(phrase in flat for phrase in date_phrases(when))


def review_shows_when(text: str, when: datetime) -> bool:
    """The review step shows this date and this clock time."""
    flat = _squash(text)
    if not date_visible(flat, when):
        return False
    return any(form in flat for form in clock_forms(when))


def zone_is_jerusalem(label: str | None) -> bool | None:
    """True/False for a visible timezone control, None when there isn't one.

    None does not mean the zone is Jerusalem. It means the wizard didn't
    expose a control, so the account timezone is whatever the host set and
    the dry-run screenshot is the check. A control that names some other
    zone is a hard failure.
    """
    if label is None or not str(label).strip():
        return None
    return "jerusalem" in str(label).lower()


def publish_now_selected(control_state: str, review_text: str) -> bool:
    """True when the episode would go live now.

    The bare label "Publish now" is not enough: that radio stays on the
    schedule step while Schedule is selected. A selected "now" control, or
    a sentence that says the episode will be available immediately, is.
    """
    state = (control_state or "").strip().lower()
    if state == "now":
        return True
    if state == "schedule":
        return False
    return bool(_WILL_PUBLISH_NOW.search(review_text or ""))


def _flat(text: str) -> str:
    return " ".join(_BIDI.sub("", text or "").split())


def titles_match(planned: str, got: str) -> bool:
    return _flat(planned) == _flat(got)


def description_readback_ok(planned: str, got: str) -> bool:
    """Each planned chapter line is its own line in the editor.

    Comparing the first 30 characters and the hashtag set missed a rich
    editor that joined every chapter onto one line. Prose lines have to be
    present too, so a chapters-only readback does not pass.
    """
    if seo.hashtags(got) != seo.hashtags(planned):
        return False
    if seo.chapter_lines(planned) != seo.chapter_lines(got):
        return False
    flat_got = _flat(got)
    for raw in (planned or "").splitlines():
        line = _flat(raw)
        if not line or line.startswith("#"):
            continue
        if seo.chapter_lines(raw):
            continue
        if line not in flat_got:
            return False
    return True


def episode_title_listed(list_text: str, title: str) -> bool:
    """The Episodes dashboard text already contains this full title."""
    want = _flat(title)
    if not want:
        return False
    return want in _flat(list_text)


def list_text_minus_query(list_text: str, query: str) -> str:
    """Drop one copy of `query` so a search box holding the title is not a row."""
    flat = _flat(list_text)
    q = _flat(query)
    if not q:
        return flat
    return flat.replace(q, " ", 1)


def spotify_url_is_login(url: str) -> bool:
    u = (url or "").lower()
    return any(bit in u for bit in _LOGIN_URL)


def spotify_body_is_login(body: str) -> bool:
    return bool(_LOGIN_BODY.search(body or ""))


def spotify_episodes_visible(body: str) -> bool:
    """The Episodes list, not a login page that happens to mention the show."""
    if spotify_body_is_login(body):
        return False
    return bool(_EPISODES.search(body or ""))


def spotify_session_ready(url: str, body: str) -> bool:
    """Logged in only when the Episodes surface is actually showing.

    `creators.spotify.com` without the word "login" is not enough: the
    redirect after a login page can pause on that host, and some login
    screens are served from creators.spotify.com itself.
    """
    if spotify_url_is_login(url):
        return False
    return spotify_episodes_visible(body)


def spotify_wizard_logged_out(url: str, body: str) -> bool:
    """A login wall, including one whose URL is still creators.spotify.com."""
    if spotify_url_is_login(url):
        return True
    return spotify_body_is_login(body)


def date_near_title(text: str, title: str, when: datetime, window: int = 800) -> bool:
    """The planned date sits next to this title, not elsewhere on the page.

    A dashboard can show today's date in the chrome and this title on a row
    that was scheduled for a different day. The date has to fall within
    `window` characters of the title.
    """
    flat = _flat(text)
    want = _flat(title)
    idx = flat.find(want)
    if idx < 0:
        return False
    start = max(0, idx - window)
    end = idx + len(want) + window
    return date_visible(flat[start:end], when)


def spotify_committed(url: str, body: str, title: str, when: datetime,
                      episode_link: str | None) -> bool:
    """Post-commit confirmation.

    The wizard schedule step contains "scheduled for" before anything is
    committed. That sentence is not a success, and neither is an
    open.spotify.com link still sitting inside the wizard (the link can
    exist before the episode is scheduled). Success is a page that has left
    the wizard: the Episodes list showing this title with the planned date
    beside it, or that same pair next to an open.spotify.com episode link.
    """
    if "episode/wizard" in (url or ""):
        return False
    if not date_near_title(body, title, when):
        return False
    on_list = "/episodes" in (url or "")
    link_ok = "open.spotify.com/episode/" in (episode_link or "")
    return on_list or link_ok


def spotify_outcome(*, dry: bool, seo_problems: list[str], readback_failed: list[str],
                    duplicate: bool, publish_now: bool) -> tuple[str, int]:
    """Status and exit code before a Schedule click, or after a bad readback.

    `ready_to_commit` is the only submit result that may click Schedule.
    Publish-now is first: a second episode is bad, a live episode is worse.
    """
    if publish_now:
        return "publish_now_refused", 6
    if duplicate:
        return "duplicate_refused", 6
    if seo_problems:
        return ("dry_refused" if dry else "seo_refused"), 7
    if readback_failed:
        return ("dry_readback_failed" if dry else "readback_refused"), 6
    if dry:
        return "dry_ok", 0
    return "ready_to_commit", 0


def _emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return code


def _body(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:  # noqa: BLE001 - navigation can detach the document
        return ""


def _click_schedule_control(page) -> bool:
    """Click a visible Schedule control, not the first text node named that.

    `get_by_text('^Schedule$').first` can hit a heading or a nav label and
    report success. Only a button, radio, or label counts, and the date
    input has to be readable afterwards or the readback fails.
    """
    return bool(page.evaluate(
        """() => {
          const vis = e => !!(e.offsetWidth || e.offsetHeight);
          const nodes = [...document.querySelectorAll(
            'button, [role="radio"], [role="button"], label')];
          const hit = nodes.find(el => vis(el) &&
            (el.innerText || el.getAttribute('aria-label') || '').trim() === 'Schedule');
          if (!hit) return false;
          hit.click();
          return true;
        }"""))


def _input_type(locator) -> str:
    try:
        return locator.evaluate(
            "el => (el.getAttribute('type') || 'text').toLowerCase()") or "text"
    except Exception:  # noqa: BLE001
        return "text"


def _selected_publish_mode(page) -> str:
    try:
        return page.evaluate(
            """() => {
              const nodes = [...document.querySelectorAll(
                '[role=radio], input[type=radio], [role=switch], button')];
              for (const el of nodes) {
                const on = el.getAttribute('aria-checked') === 'true' || el.checked
                  || el.getAttribute('aria-pressed') === 'true';
                if (!on) continue;
                const t = (el.innerText || el.getAttribute('aria-label') || el.value || '')
                  .trim().toLowerCase();
                if (t === 'now' || t === 'publish now' || t === 'immediately') return 'now';
                if (t === 'schedule' || t.startsWith('schedule ')) return 'schedule';
              }
              return '';
            }""") or ""
    except Exception:  # noqa: BLE001
        return ""


def _read_zone(page) -> str | None:
    """Timezone control value, or None when no such control is on the page."""
    try:
        raw = page.evaluate(
            """() => {
              const vis = e => !!(e.offsetWidth || e.offsetHeight);
              const nodes = [...document.querySelectorAll(
                'select, [role=combobox], [aria-label]')];
              for (const el of nodes) {
                if (!vis(el)) continue;
                const label = ((el.getAttribute('aria-label') || '') + ' '
                  + (el.innerText || '')).toLowerCase();
                if (!(label.includes('time zone') || label.includes('timezone')
                      || label.includes('jerusalem'))) continue;
                const v = (el.value || el.innerText || el.getAttribute('aria-label') || '');
                return v.slice(0, 200);
              }
              return null;
            }""")
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    return str(raw)


def _prefer_jerusalem_zone(page) -> str | None:
    """Select a Jerusalem option when a timezone <select> offers one."""
    try:
        picked = page.evaluate(
            """() => {
              for (const sel of document.querySelectorAll('select')) {
                const opt = [...sel.options].find(o => /jerusalem/i.test(o.text));
                if (!opt) continue;
                sel.value = opt.value;
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                return opt.text;
              }
              return null;
            }""")
    except Exception:  # noqa: BLE001
        return None
    if picked:
        return str(picked)
    return _read_zone(page)


def _fill_description(page, desc_box, desc: str) -> None:
    """Put `desc` in the box without merging chapter lines.

    `fill()` on a textarea keeps newlines. A contenteditable often turns a
    pasted newline into a space, which is how chapters collapse onto one
    line. Enter between lines keeps them as separate blocks.
    """
    tag = ""
    try:
        tag = desc_box.evaluate("el => el.tagName.toLowerCase()")
    except Exception:  # noqa: BLE001
        tag = ""
    if tag == "textarea":
        desc_box.fill(desc)
        return
    desc_box.click(timeout=15_000)
    lines = desc.splitlines() or [""]
    for i, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if i != len(lines) - 1:
            page.keyboard.press("Enter")


def _advance_to_review(page) -> str:
    """Click Next until the footer is no longer Next. Never clicks Schedule
    or Publish. Returns the body of the step we stopped on."""
    for _ in range(6):
        try:
            action = page.evaluate(
                """() => {
                  const vis = e => !!(e.offsetWidth || e.offsetHeight);
                  const buttons = [...document.querySelectorAll('button')].filter(vis);
                  const label = b => (b.innerText || '').trim();
                  const finalBtn = buttons.find(b => label(b) === 'Schedule'
                    || label(b) === 'Publish');
                  const next = buttons.find(b => label(b) === 'Next' && !b.disabled);
                  // Stop as soon as the final button is on screen, even if
                  // Next is still visible. Clicking Next past it can leave
                  // the review step, and Publish is never clicked.
                  if (finalBtn) return 'stop';
                  if (next) { next.click(); return 'next'; }
                  return 'stop';
                }""")
        except Exception:  # noqa: BLE001
            break
        page.wait_for_timeout(1_500)
        if action != "next":
            break
    return _body(page)


def _click_commit(page) -> str:
    """Click Next or Schedule. Returns the label clicked, or ''."""
    try:
        return page.evaluate(
            """(labels) => {
              const vis = e => !!(e.offsetWidth || e.offsetHeight);
              const b = [...document.querySelectorAll('button')].filter(vis)
                .find(x => labels.includes(x.innerText.trim()) && !x.disabled);
              if (!b) return '';
              b.click();
              return b.innerText.trim();
            }""", list(SPOTIFY_COMMIT_LABELS)) or ""
    except Exception:  # noqa: BLE001
        return ""


def _episode_link(page) -> str | None:
    try:
        link = page.locator("a[href*='open.spotify.com/episode/']")
        if link.count():
            return link.first.get_attribute("href")
    except Exception:  # noqa: BLE001
        return None
    return None


def _logged_out_payload() -> dict:
    return {"status": "logged_out",
            "fix": "run upload_spotify.py --login once"}


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
            # Two polls in a row. A redirect can flash creators.spotify.com
            # before the login page, and a login form served by that host
            # does not contain the word "login" in the URL.
            stable = 0
            for _ in range(120):
                page.wait_for_timeout(5_000)
                if spotify_session_ready(page.url, _body(page)):
                    stable += 1
                    if stable >= 2:
                        page.wait_for_timeout(1_000)
                        print("logged in - Episodes list is up, session saved")
                        ctx.close()
                        return 0
                else:
                    stable = 0
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
        video = CLIPS_DIR / f"{plan['episode']}_{args.clip}.mp4"
        if not video.exists():
            print(f"error: {video} not found", file=sys.stderr)
            ctx.close()
            return 2
        when = jerusalem_when(post["date"], plan["time_local"])

        # The Episodes list first. A rerun after save_not_confirmed must not
        # open the wizard and create a second episode with the same title.
        page.goto(DASH, timeout=60_000)
        page.wait_for_timeout(4_000)
        dash = _body(page)
        if not spotify_session_ready(page.url, dash):
            page.wait_for_timeout(3_000)
            dash = _body(page)
        if not spotify_session_ready(page.url, dash):
            if spotify_wizard_logged_out(page.url, dash) or not spotify_episodes_visible(dash):
                ctx.close()
                return _emit(_logged_out_payload(), 5)
        if not episode_title_listed(dash, title):
            search = page.locator(
                "input[type='search'], input[placeholder*='Search' i], "
                "input[aria-label*='Search' i]")
            if search.count():
                try:
                    search.first.fill(title)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(2_000)
                    # The search box now contains the title, so it would match
                    # even when the list has no such row. Drop that one copy.
                    dash = list_text_minus_query(_body(page), title)
                except Exception as e:  # noqa: BLE001
                    print(f"warn: episode search not used ({str(e)[:60]})",
                          file=sys.stderr)
        if episode_title_listed(dash, title):
            page.screenshot(path=args.shot, full_page=True)
            status, code = spotify_outcome(
                dry=args.dry, seo_problems=problems, readback_failed=[],
                duplicate=True, publish_now=False)
            ctx.close()
            return _emit({"status": status, "title": title, "screenshot": args.shot,
                          "why": "this title is already on the Episodes list"}, code)

        if problems and args.submit:
            print(f"seo: {'; '.join(problems)}", file=sys.stderr)
            ctx.close()
            return _emit({"status": "seo_refused", "problems": problems}, 7)
        if problems:
            print(f"seo: {'; '.join(problems)}", file=sys.stderr)

        page.goto(WIZARD, timeout=60_000)
        page.wait_for_timeout(4_000)
        if spotify_wizard_logged_out(page.url, _body(page)):
            ctx.close()
            return _emit(_logged_out_payload(), 5)

        page.set_input_files("input[type=file]", str(video), timeout=30_000)
        title_box = page.locator("input[name='title'], #title-input").first
        try:
            title_box.wait_for(timeout=120_000)
        except Exception as e:  # noqa: BLE001
            body = _body(page)
            page.screenshot(path=args.shot, full_page=True)
            ctx.close()
            if spotify_wizard_logged_out(page.url, body):
                return _emit(_logged_out_payload(), 5)
            return _emit({"status": "wizard_not_ready", "screenshot": args.shot,
                          "error": str(e)[:120]}, 6)

        # fill()/insert_text, never char typing: typing scrambles Hebrew.
        title_box.fill(title)
        desc_box = page.locator(
            "[name='description'] [contenteditable='true'], "
            "div[contenteditable='true'][role='textbox'], textarea[name='description']"
        ).first
        _fill_description(page, desc_box, desc)

        num = episode_number(plan["episode"])
        if num:
            try:
                page.get_by_label(re.compile("Episode number", re.I)).first.fill(
                    num, timeout=5_000)
            except Exception as e:  # noqa: BLE001
                print(f"warn: episode number not set ({str(e)[:60]})", file=sys.stderr)

        got_date, got_time = "", ""
        zone_label: str | None = None
        try:
            if not _click_schedule_control(page):
                raise RuntimeError("no Schedule control")
            page.wait_for_timeout(800)
            date_loc = page.locator(
                "input[type='date'], input[type='datetime-local'], "
                "input[name*='date' i]")
            time_loc = page.locator("input[type='time'], input[name*='time' i]")
            if not date_loc.count():
                raise RuntimeError("no date input")
            date_el = date_loc.first
            date_type = _input_type(date_el)
            date_el.fill(input_fill_value(date_type, when, "date"))
            if date_type != "datetime-local":
                if not time_loc.count():
                    raise RuntimeError("no time input")
                time_el = time_loc.first
                time_el.fill(input_fill_value(_input_type(time_el), when, "time"))
                got_time = time_el.input_value()
            page.keyboard.press("Tab")
            got_date = date_el.input_value()
            if date_type == "datetime-local":
                got_time = ""
            zone_label = _prefer_jerusalem_zone(page)
        except Exception as e:  # noqa: BLE001
            print(f"warn: schedule not set ({str(e)[:80]})", file=sys.stderr)

        got_title = ""
        got_desc = ""
        try:
            got_title = title_box.input_value()
        except Exception:  # noqa: BLE001
            got_title = ""
        try:
            got_desc = desc_box.inner_text() if desc_box.count() else ""
        except Exception:  # noqa: BLE001
            got_desc = ""

        review = _advance_to_review(page)
        mode = _selected_publish_mode(page)
        publish_now = publish_now_selected(mode, review)
        zone_state = zone_is_jerusalem(zone_label)
        checks = {
            "title": titles_match(title, got_title),
            "description": description_readback_ok(desc, got_desc),
            "schedule": schedule_inputs_match(got_date, got_time, when)
                        and review_shows_when(review, when)
                        and zone_state is not False,
        }
        bad = [k for k, ok in checks.items() if not ok]
        if zone_state is False and "schedule" not in bad:
            bad.append("schedule")
        page.screenshot(path=args.shot, full_page=True)

        status, code = spotify_outcome(
            dry=args.dry, seo_problems=problems, readback_failed=bad,
            duplicate=False, publish_now=publish_now)
        if status != "ready_to_commit":
            ctx.close()
            return _emit({
                "status": status, "failed": bad, "screenshot": args.shot,
                "seo_problems": problems, "publish_now": publish_now,
                "when": when.isoformat(timespec="minutes"),
                "timezone": "Asia/Jerusalem",
                "timezone_seen": zone_label,
                "got_date": got_date, "got_time": got_time,
            }, code)

        # Next, then Schedule. Publish is not a label we click. If the
        # review step says the episode would go live now, stop.
        for _ in range(90):  # up to ~22 min for a full-length video
            mode = _selected_publish_mode(page)
            if publish_now_selected(mode, _body(page)):
                page.screenshot(path=args.shot, full_page=True)
                ctx.close()
                return _emit({"status": "publish_now_refused",
                              "screenshot": args.shot}, 6)
            if "episode/wizard" not in page.url and "/episodes" in page.url:
                break
            if not _click_commit(page):
                page.wait_for_timeout(5_000)
            else:
                page.wait_for_timeout(15_000)

        # Confirm on the Episodes list, not on the wizard. The schedule step
        # already says "scheduled for" while the episode is still a draft.
        committed = False
        link = None
        for _ in range(5):
            if "episode/wizard" in page.url or "/episodes" not in page.url:
                page.goto(DASH, timeout=60_000)
            page.wait_for_timeout(4_000)
            body = _body(page)
            link = _episode_link(page)
            if spotify_committed(page.url, body, title, when, link):
                committed = True
                break
            try:
                page.reload(timeout=60_000)
            except Exception:  # noqa: BLE001
                pass
        page.screenshot(path=args.shot, full_page=False)
        if not committed:
            ctx.close()
            return _emit({"status": "save_not_confirmed", "screenshot": args.shot,
                          "why": "Episodes list did not show this title on the "
                                 "scheduled date"}, 6)
        import autolog
        out = {"status": "submitted", "clip": args.clip,
               "when": when.isoformat(timespec="minutes"), "post_url": link,
               "timezone": "Asia/Jerusalem"}
        out.update(autolog.log(args.plan, args.clip, "spotify", link))
        print(json.dumps(out, ensure_ascii=False))
        ctx.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
