"""Readback and gate decisions for the YouTube and Spotify episode uploaders.

The uploaders drive a logged-in browser, so these tests stay on the pure
decisions: date and time values, whether a dry run would be a refusal,
chapter-line readback, and when it is safe to close the browser.
"""
import importlib.util
import sys
from pathlib import Path

_PUBLISH = Path(__file__).resolve().parents[1] / "scripts" / "publish"


def _load(name: str):
    src = _PUBLISH / f"{name}.py"
    # seo.py is a sibling import inside the uploaders.
    sys.path.insert(0, str(_PUBLISH))
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sp = _load("upload_spotify")
yt = _load("upload_youtube")
al = _load("autolog")

WHEN = sp.jerusalem_when("2026-10-30", "22:30")
TITLE = "וויקלי סינק | פרק 214"


def test_spotify_date_and_time_inputs_use_iso_not_locale_strings():
    assert sp.input_fill_value("date", WHEN, "date") == "2026-10-30"
    assert sp.input_fill_value("time", WHEN, "time") == "22:30"
    assert sp.input_fill_value("datetime-local", WHEN, "date") == "2026-10-30T22:30"
    assert sp.input_fill_value("text", WHEN, "time") == "22:30"
    assert "/" not in sp.input_fill_value("date", WHEN, "date")
    assert "PM" not in sp.input_fill_value("time", WHEN, "time")
    # Wall clock, not UTC. 22:30 in Jerusalem must not become 19:30 or 20:30.
    assert (WHEN.hour, WHEN.minute, WHEN.day) == (22, 30, 30)
    assert WHEN.utcoffset().total_seconds() in (2 * 3600, 3 * 3600)


def test_spotify_schedule_readback_compares_the_input_values():
    assert sp.schedule_inputs_match("2026-10-30", "22:30", WHEN)
    assert sp.schedule_inputs_match("2026-10-30", "22:30:00", WHEN)
    assert sp.schedule_inputs_match("2026-10-30", "10:30 PM", WHEN)
    assert sp.schedule_inputs_match("2026-10-30T22:30", "", WHEN)
    # The old fill strings are not a match, and neither is a different clock.
    assert not sp.schedule_inputs_match("10/30/2026", "10:30 PM", WHEN)
    assert not sp.schedule_inputs_match("2026-10-30", "10:30 AM", WHEN)
    assert not sp.schedule_inputs_match("", "", WHEN)


def test_spotify_review_and_timezone():
    text = "Review\nOctober 30, 2026\n10:30 PM\n" + TITLE
    assert sp.review_shows_when(text, WHEN)
    assert sp.date_visible("Scheduled for Oct 30, 2026", WHEN)
    assert not sp.date_visible("Scheduled for Oct 31, 2026", WHEN)
    assert sp.zone_is_jerusalem(None) is None
    assert sp.zone_is_jerusalem("") is None
    assert sp.zone_is_jerusalem("Asia/Jerusalem (GMT+2)") is True
    assert sp.zone_is_jerusalem("America/New_York") is False


def test_publish_now_label_alone_does_not_refuse():
    # The "Publish now" radio stays visible while Schedule is selected.
    assert not sp.publish_now_selected("", "Publish now\nSchedule")
    assert not sp.publish_now_selected("schedule", "Publish now")
    assert sp.publish_now_selected("now", "Scheduled for October 30, 2026")
    assert sp.publish_now_selected("", "This episode will be published immediately")


def test_description_readback_requires_each_chapter_on_its_own_line():
    planned = "פתיחה\n\n0:00 פתיחה\n3:10 נושא\n12:40 סיום\n\n#פודקאסט #הייטק"
    assert sp.description_readback_ok(planned, planned)
    merged = "פתיחה\n\n0:00 פתיחה 3:10 נושא 12:40 סיום\n\n#פודקאסט #הייטק"
    assert not sp.description_readback_ok(planned, merged)
    assert not sp.description_readback_ok(planned, planned.replace("#הייטק", "#אחר"))
    # A leading bidi mark on an otherwise intact chapter line still matches.
    marked = planned.replace("0:00", "\u200f0:00")
    assert sp.description_readback_ok(planned, marked)


def test_wizard_scheduled_for_is_not_a_commit():
    wizard = "https://creators.spotify.com/pod/dashboard/episode/wizard"
    dash = "https://creators.spotify.com/pod/dashboard/episodes"
    listed = f"{TITLE}\nOctober 30, 2026\n10:30 PM"
    wizard_copy = f"scheduled for October 30, 2026 at 10:30 PM\n{TITLE}"
    link = "https://open.spotify.com/episode/abc"
    assert not sp.spotify_committed(wizard, wizard_copy, TITLE, WHEN, None)
    # A link rendered inside the wizard is not a commit either.
    assert not sp.spotify_committed(wizard, listed, TITLE, WHEN, link)
    assert sp.spotify_committed(dash, listed, TITLE, WHEN, None)
    # The date has to sit next to this title. A date elsewhere on the page
    # (chrome, a different row) is not this episode's schedule.
    far = TITLE + (" x" * 500) + "\nOctober 30, 2026"
    assert not sp.spotify_committed(dash, far, TITLE, WHEN, None)
    assert sp.spotify_committed(
        "https://creators.spotify.com/pod/show/done", listed, TITLE, WHEN, link)
    assert not sp.spotify_committed(dash, f"{TITLE}\nOctober 31, 2026", TITLE, WHEN, None)
    assert not sp.spotify_committed(dash, "October 30, 2026", TITLE, WHEN, link)


def test_duplicate_title_ignores_the_search_box():
    assert sp.episode_title_listed(f"row {TITLE} scheduled", TITLE)
    only_the_box = f"Search {TITLE} no results"
    assert not sp.episode_title_listed(sp.list_text_minus_query(only_the_box, TITLE), TITLE)
    box_and_row = f"Search {TITLE} {TITLE} October 30"
    assert sp.episode_title_listed(sp.list_text_minus_query(box_and_row, TITLE), TITLE)


def test_spotify_login_waits_for_the_episodes_list():
    dash = "https://creators.spotify.com/pod/dashboard/episodes"
    assert not sp.spotify_session_ready(dash, "")
    assert not sp.spotify_session_ready("https://accounts.spotify.com/en/login", "Episodes")
    assert not sp.spotify_session_ready(
        "https://creators.spotify.com/pod/login", "Log in to Spotify")
    assert not sp.spotify_session_ready(dash, "Log in to Spotify\nEmail or username")
    assert sp.spotify_session_ready(dash, "Episodes\nNew episode")
    assert sp.spotify_session_ready(dash, "פרקים\nפרק חדש")
    assert sp.spotify_wizard_logged_out(
        "https://creators.spotify.com/pod/dashboard", "Log in to Spotify to continue")
    assert not sp.spotify_wizard_logged_out(
        "https://creators.spotify.com/pod/dashboard/episode/wizard", "Title\nDescription")


def test_spotify_outcome_refuses_before_a_schedule_click():
    ok = dict(seo_problems=[], readback_failed=[], duplicate=False, publish_now=False)
    assert sp.spotify_outcome(dry=True, **ok) == ("dry_ok", 0)
    assert sp.spotify_outcome(dry=False, **ok) == ("ready_to_commit", 0)
    assert sp.spotify_outcome(dry=False, **{**ok, "seo_problems": ["x"]}) == ("seo_refused", 7)
    assert sp.spotify_outcome(dry=True, **{**ok, "seo_problems": ["x"]}) == ("dry_refused", 7)
    assert sp.spotify_outcome(
        dry=True, **{**ok, "readback_failed": ["description"]}) == ("dry_readback_failed", 6)
    assert sp.spotify_outcome(
        dry=False, **{**ok, "readback_failed": ["schedule"]}) == ("readback_refused", 6)
    assert sp.spotify_outcome(dry=False, **{**ok, "duplicate": True}) == ("duplicate_refused", 6)
    assert sp.spotify_outcome(
        dry=False, **{**ok, "publish_now": True}) == ("publish_now_refused", 6)
    assert sp.SPOTIFY_COMMIT_LABELS == ("Next", "Schedule")
    src = (_PUBLISH / "upload_spotify.py").read_text(encoding="utf-8")
    assert "episode (is )?(scheduled|published)" not in src
    assert "['Next','Schedule','Publish']" not in src
    assert '["Next", "Schedule", "Publish"]' not in src


def test_youtube_long_form_dry_matches_what_submit_would_refuse():
    ok = dict(seo_problems=[], tags_planned="abcd", tags_committed=4,
              date_ok=True, time_ok=True, thumbnail_set=True)
    assert yt.youtube_long_form_blocker(**ok) is None
    assert yt.youtube_long_form_blocker(**{**ok, "seo_problems": ["no title"]}) == "seo_refused"
    assert yt.youtube_long_form_blocker(**{**ok, "date_ok": False}) == "schedule_readback_refused"
    assert yt.youtube_long_form_blocker(**{**ok, "time_ok": False}) == "schedule_readback_refused"
    assert yt.youtube_long_form_blocker(**{**ok, "tags_committed": 0}) == "tags_not_committed"
    assert yt.youtube_long_form_blocker(**{**ok, "thumbnail_set": False}) == "thumbnail_not_set"
    # Shorts stay dry_ok even if a long-form blocker would fire.
    assert yt.youtube_dry_decision(False, "seo_refused") == ("dry_ok", 0)
    assert yt.youtube_dry_decision(True, None) == ("dry_ok", 0)
    assert yt.youtube_dry_decision(True, "seo_refused") == ("dry_refused", 7)
    assert yt.youtube_dry_decision(True, "tags_not_committed") == ("dry_refused", 7)
    assert yt.youtube_dry_decision(True, "schedule_readback_refused") == ("dry_refused", 6)
    assert yt.youtube_dry_decision(True, "thumbnail_not_set") == ("dry_refused", 6)


def test_youtube_tags_refuse_under_80_percent_of_planned():
    assert not yt.tags_committed_enough("a" * 100, 79)
    assert yt.tags_committed_enough("a" * 100, 80)
    assert not yt.tags_committed_enough("a" * 100, 0)
    assert not yt.tags_committed_enough("", 0)
    # The typed cap is 495, so a longer plan is judged against that.
    assert yt.tags_committed_enough("a" * 600, 396)
    assert not yt.tags_committed_enough("a" * 600, 395)


def test_youtube_schedule_readback_requires_the_year():
    label = yt._date_label("2026-10-30")
    want_time = yt._ampm("22:30")
    assert yt.youtube_schedule_readback_ok(
        f"Schedule {label} {want_time}", want_time, "2026-10-30", want_time) == (True, True)
    month_day = label.split(",")[0]
    assert yt.youtube_schedule_readback_ok(
        f"Schedule {month_day} {want_time}", want_time, "2026-10-30", want_time)[0] is False
    assert yt.youtube_schedule_readback_ok(
        f"Schedule {label.replace('2026', '2027')} 9:00 AM",
        "9:00 AM", "2026-10-30", want_time) == (False, False)


def test_youtube_thumbnail_is_scoped_and_under_2mb():
    assert yt.thumbnail_precheck(False, 0) == "missing"
    assert yt.thumbnail_precheck(True, 0) == "empty"
    assert yt.thumbnail_precheck(True, 2 * 1024 * 1024) == "over_2mb"
    assert yt.thumbnail_precheck(True, 2 * 1024 * 1024 - 1) is None
    assert yt.thumbnail_landed("https://auto", "blob:custom", "") is True
    assert yt.thumbnail_landed("https://auto", "https://auto", "") is False
    assert yt.thumbnail_landed("https://auto", "blob:custom", "Couldn't upload") is False
    assert yt.thumbnail_landed("https://auto", "", "") is False
    src = (_PUBLISH / "upload_youtube.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines()
                     if not line.strip().startswith("#"))
    assert "input#file-loader" not in code
    assert "ytcp-thumbnails-compact-editor-uploader" in code


def test_youtube_close_waits_for_upload_and_checks():
    assert yt.youtube_progress_flags("Uploading 40% ... 12 minutes left") == (False, False, True)
    upload_done, checks_done, still = yt.youtube_progress_flags(
        "Upload complete. Checks complete. No issues found.")
    assert upload_done and checks_done and not still
    assert yt.youtube_safe_to_close(
        "Video scheduled", saw_upload_complete=True, saw_checks_complete=True)
    assert not yt.youtube_safe_to_close(
        "Video scheduled", saw_upload_complete=False, saw_checks_complete=False)
    # Remembered flags do not excuse a dialog that is still uploading.
    assert not yt.youtube_safe_to_close(
        "Uploading 12%", saw_upload_complete=True, saw_checks_complete=True)
    assert yt.youtube_safe_to_close(
        "Processing HD version. No issues found.",
        saw_upload_complete=False, saw_checks_complete=False)
    assert "Publish" not in yt.LONG_FORM_COMMIT_LABELS
    assert yt.SHORTS_COMMIT_LABELS == ("Schedule", "Save", "Publish")
    assert yt.commit_labels(False) == yt.SHORTS_COMMIT_LABELS
    assert yt.commit_labels(True) == yt.LONG_FORM_COMMIT_LABELS


def test_autolog_passes_a_hook_for_the_full_episode():
    assert al.episode_log_hook("clip-1", "youtube", {"youtube": "x"}) is None
    assert al.episode_log_hook("episode", "spotify", {"spotify_title": "כותרת"}) == "כותרת"
    assert al.episode_log_hook("episode", "youtube", {"youtube": "כותרת"}) == "כותרת"
    assert al.episode_log_hook("WS214_episode", "youtube", {}) == "full episode"
