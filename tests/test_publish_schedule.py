"""Pure scheduling checks for the Playwright uploaders.

The uploaders themselves drive a logged-in browser, so these tests stay on
the decisions that can be wrong without one: date and time labels, the
readback comparison, mention/hashtag splitting, and the dry/submit gates.
"""
import importlib.util
import sys
from pathlib import Path

_PUBLISH = Path(__file__).resolve().parents[1] / "scripts" / "publish"


def _load(name: str):
    src = _PUBLISH / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tt = _load("upload_tiktok")
ig = _load("upload_instagram")
li = _load("upload_linkedin")


def test_tiktok_date_labels_match_the_studio_controls():
    # Combobox shows "Oct 27, 2026"; the day button's aria-label ends with
    # "October 27th, 2026"; the panel title is "October 2026".
    assert tt.tiktok_date_targets("2026-10-27") == (
        "October 2026", "October 27th, 2026", "Oct 27, 2026")
    assert tt.tiktok_date_targets("2026-11-01")[1:] == (
        "November 1st, 2026", "Nov 1, 2026")
    assert tt.tiktok_date_targets("2026-11-02")[1].endswith("2nd, 2026")
    assert tt.tiktok_date_targets("2026-11-03")[1].endswith("3rd, 2026")
    assert tt.tiktok_date_targets("2026-11-11")[1].endswith("11th, 2026")
    assert tt.tiktok_date_targets("2026-11-12")[1].endswith("12th, 2026")
    assert tt.tiktok_date_targets("2026-11-13")[1].endswith("13th, 2026")
    assert tt.tiktok_date_targets("2026-11-22")[1].endswith("22nd, 2026")
    assert tt.tiktok_date_targets("2026-11-23")[1].endswith("23rd, 2026")


def test_ordinal_teens_are_not_st_nd_rd():
    assert [tt.ordinal_day(d) for d in (1, 2, 3, 4, 11, 12, 13, 21, 31)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "31st"]


def test_schedule_readback_requires_date_time_and_jerusalem():
    ok = tt.schedule_readback_ok
    assert ok("Oct 27, 2026", "20:00", "(GMT+2) Jerusalem", "2026-10-27", "20:00")
    assert ok("Nov 1, 2026", "20:00", "(GMT+3) Jerusalem", "2026-11-01", "20:00")
    assert not ok("Oct 27, 2026", "20:00", "(GMT+3) Local Time", "2026-10-27", "20:00")
    assert not ok("Oct 28, 2026", "20:00", "(GMT+2) Jerusalem", "2026-10-27", "20:00")
    assert not ok("Oct 27, 2026", "00:00", "(GMT+2) Jerusalem", "2026-10-27", "20:00")
    assert not ok("Oct 27, 2026", "20:00", "", "2026-10-27", "20:00")


def test_tiktok_dry_never_submits_and_incomplete_upload_is_refused():
    assert tt.tiktok_dry_or_refuse(
        dry=True, upload_broken=False, upload_complete=True) == ("dry_ok", 0)
    assert tt.tiktok_dry_or_refuse(
        dry=True, upload_broken=True, upload_complete=False) == (
            "dry_ok_form_only_upload_failed", 0)
    assert tt.tiktok_dry_or_refuse(
        dry=False, upload_broken=False, upload_complete=True) is None
    assert tt.tiktok_dry_or_refuse(
        dry=False, upload_broken=False, upload_complete=False) == (
            "refused_upload_incomplete", 6)


def test_form_only_upload_flag_cannot_arm_on_a_real_submit():
    src = (_PUBLISH / "upload_tiktok.py").read_text(encoding="utf-8")
    hits = [line for line in src.splitlines() if "SOFIT_TT_FORM_ONLY" in line]
    assert hits and all("args.dry" in line for line in hits)
    assert "textContent.trim()==='Schedule'" not in src
    assert "textContent.trim() === 'Schedule'" not in src
    assert 'get_by_text("Schedule"' not in src
    assert "els[els.length - 1].click()" not in src
    assert "els[els.length-1].click()" not in src
    assert "data-sofit-blocked" in src


def test_split_tags_keeps_inline_hashes_and_lifts_trailing_lines():
    body, tags = tt.split_tags("hello world\n\n#one #two\n#three")
    assert body == "hello world"
    assert tags == ["#one", "#two", "#three"]
    body, tags = tt.split_tags("a #inline tag stays\nsecond line")
    assert tags == []
    assert "#inline" in body


def test_split_mentions_picks_handles_and_skips_emails():
    assert tt.split_mentions("hi @tortsuk there") == [
        ("text", "hi "), ("mention", "tortsuk"), ("text", " there")]
    assert tt.split_mentions("@tortsuk leads")[0] == ("mention", "tortsuk")
    assert tt.split_mentions("mail user@domain.com today") == [
        ("text", "mail user@domain.com today")]
    assert tt.split_mentions("@@tortsuk") == [("text", "@@tortsuk")]
    assert tt.split_mentions(".@tortsuk") == [("text", ".@tortsuk")]
    # The pattern needs three characters: {2,24} plus one more.
    assert tt.split_mentions("ping @ab now") == [("text", "ping @ab now")]
    assert tt.split_mentions("ping @abc now")[1] == ("mention", "abc")
    body, _tags = tt.split_tags("see @tortsuk\n\n#tag @not_in_body")
    mentions = [h for kind, h in tt.split_mentions(body) if kind == "mention"]
    assert mentions == ["tortsuk"]


def test_bidi_stripped_body_and_url_needle():
    body = "אחד\u200f שניים שלושה"
    editor = "אחד שניים שלושה\n\n#fyp"
    assert tt.body_in_editor(body, editor)
    assert not tt.body_in_editor("אחד שניים שלושה ארבעה", "אחד שניים")
    caption = "\u202aאחד שניים שלושה ארבעה חמישה שישה"
    assert tt.caption_url_needle(caption) == "אחד שניים שלושה ארבעה חמישה"


def test_linkedin_time_label_is_twelve_hour():
    assert li.linkedin_time_label("20:00") == "8:00 PM"
    assert li.linkedin_time_label("08:05") == "8:05 AM"
    assert li.linkedin_time_label("00:00") == "12:00 AM"
    assert li.linkedin_time_label("12:00") == "12:00 PM"
    assert li.linkedin_time_label("00:05") == "12:05 AM"
    assert li.linkedin_time_label("23:59") == "11:59 PM"
    assert li.linkedin_time_label("13:15") == "1:15 PM"


def test_linkedin_posting_line_and_readback():
    # 2026-11-01 is a Sunday; 2026-10-27 is a Tuesday.
    assert li.linkedin_posting_line("2026-11-01", "20:00") == (
        "Posting Sun, Nov 1 at 8:00 PM")
    assert li.linkedin_posting_line("2026-10-27", "20:00") == (
        "Posting Tue, Oct 27 at 8:00 PM")
    assert li.linkedin_schedule_matches(
        "Posting Sun, Nov 1 at 8:00 PM", "2026-11-01", "20:00")
    assert li.linkedin_schedule_matches(
        "Posting  Sun,  Nov 1  at  8:00 PM", "2026-11-01", "20:00")
    assert not li.linkedin_schedule_matches(
        "Posting Sun, Nov 1 at 5:15 PM", "2026-11-01", "20:00")
    assert not li.linkedin_schedule_matches("", "2026-11-01", "20:00")
    assert not li.linkedin_schedule_matches(
        "Posting Mon, Nov 1 at 8:00 PM", "2026-11-01", "20:00")


def test_linkedin_dry_does_not_post_and_a_post_button_is_refused():
    assert li.linkedin_stop(dry=True, scheduled=True, primary="Schedule") == (
        "dry_ok", 0)
    assert li.linkedin_stop(dry=True, scheduled=False, primary="Post") == (
        "dry_schedule_failed", 0)
    assert li.linkedin_stop(dry=False, scheduled=True, primary="Schedule") is None
    assert li.linkedin_stop(dry=False, scheduled=False, primary="Schedule") == (
        "refused_would_post_now", 4)
    assert li.linkedin_stop(dry=False, scheduled=True, primary="Post") == (
        "refused_would_post_now", 4)
    assert li.linkedin_stop(dry=False, scheduled=True, primary="POST now") == (
        "refused_would_post_now", 4)


def test_disable_quic_is_on_unless_turned_off():
    base = ["--disable-blink-features=AutomationControlled"]
    assert tt.chrome_launch_args({}) == base + ["--disable-quic"]
    assert tt.chrome_launch_args({"SOFIT_TT_NOQUIC": "1"}) == base + ["--disable-quic"]
    assert tt.chrome_launch_args({"SOFIT_TT_NOQUIC": "0"}) == base
    assert tt.chrome_launch_args({"SOFIT_TT_NOQUIC": ""}) == base
    src = (_PUBLISH / "upload_tiktok.py").read_text(encoding="utf-8")
    assert "modest help" in src
    assert "not a fix" in src
    assert "31%" in src and "42%" in src


def test_submit_refuses_missing_tags_or_caption_body():
    assert tt.tiktok_submit_block(
        n_tags=2, n_want=2, body="the episode line",
        editor_text="the episode line\n\n#one", missed=[]) is None
    blocked = tt.tiktok_submit_block(
        n_tags=1, n_want=3, body="the episode line",
        editor_text="the episode line", missed=["#two", "#three"])
    assert blocked == {
        "status": "refused_tags",
        "tags_registered": 1,
        "tags_wanted": 3,
        "tags_missed": ["#two", "#three"],
    }
    # Tags win when both are wrong: nothing is submitted either way.
    blocked = tt.tiktok_submit_block(
        n_tags=0, n_want=1, body="the episode line", editor_text="",
        missed=["#one"])
    assert blocked["status"] == "refused_tags"
    assert tt.tiktok_submit_block(
        n_tags=1, n_want=1, body="the episode line",
        editor_text="#one only") == {"status": "refused_body_mismatch"}
    # Bidi marks in the plan still count as the same caption.
    assert tt.tiktok_submit_block(
        n_tags=0, n_want=0, body="אחד\u200f שניים",
        editor_text="אחד שניים\nmore") is None


def test_footer_schedule_uses_visible_text_then_the_accessible_name():
    assert tt.footer_schedule_via(1) == "text"
    assert tt.footer_schedule_via(2) == "text"
    assert tt.footer_schedule_via(0) == "role"
    assert tt.footer_schedule_ready(1, True)
    assert not tt.footer_schedule_ready(0, True)
    assert not tt.footer_schedule_ready(2, True)
    assert not tt.footer_schedule_ready(1, False)
    src = (_PUBLISH / "upload_tiktok.py").read_text(encoding="utf-8")
    text_at = src.index("has_text=_SCHEDULE_BUTTON_TEXT")
    role_at = src.index('get_by_role("button", name="Schedule", exact=True)')
    assert text_at < role_at
    assert "schedule_container" in src
    assert "refused_schedule_button" in src


def test_list_stamp_stays_english_when_the_locale_changes():
    import locale
    assert tt.tiktok_list_stamp("2026-10-27", "20:00") == "Oct 27, 8:00 PM"
    assert tt.tiktok_list_stamp("2026-11-01", "08:05") == "Nov 1, 8:05 AM"
    assert tt.tiktok_list_stamp("2026-11-01", "00:00") == "Nov 1, 12:00 AM"
    assert tt.tiktok_list_stamp("2026-11-01", "12:00") == "Nov 1, 12:00 PM"
    assert tt.tiktok_list_stamp("2026-01-05", "13:15") == "Jan 5, 1:15 PM"
    assert tt.tiktok_list_stamp("2026-12-31", "23:59") == "Dec 31, 11:59 PM"
    saved = locale.setlocale(locale.LC_TIME)
    try:
        tried = 0
        for name in ("C", "de_DE.UTF-8", "he_IL.UTF-8", "fr_FR.UTF-8"):
            try:
                locale.setlocale(locale.LC_TIME, name)
            except locale.Error:
                continue
            tried += 1
            assert tt.tiktok_list_stamp("2026-10-27", "20:00") == "Oct 27, 8:00 PM"
        assert tried >= 1
    finally:
        locale.setlocale(locale.LC_TIME, saved)
    assert tt.tiktok_list_row_has_stamp(
        "clip caption\nOct 27, 8:00 PM\nScheduled", "2026-10-27", "20:00")
    assert tt.tiktok_list_row_has_stamp(
        "clip \u200eOct  27,  8:00 PM", "2026-10-27", "20:00")
    assert not tt.tiktok_list_row_has_stamp(
        "clip caption\nOct 28, 8:00 PM", "2026-10-27", "20:00")
    assert not tt.tiktok_list_row_has_stamp("", "2026-10-27", "20:00")
    assert tt.tiktok_submit_status(True, True) == "submitted"
    assert tt.tiktok_submit_status(True, False) == "submitted_unverified"
    assert tt.tiktok_submit_status(False, True) == "submitted_unverified"
    assert tt.tiktok_submit_status(False, False) == "submitted_unverified"
    src = (_PUBLISH / "upload_tiktok.py").read_text(encoding="utf-8")
    assert "import calendar" not in src
    body = src.split("def tiktok_list_stamp", 1)[1].split(
        "def tiktok_list_row_has_stamp", 1)[0]
    code = body.split('"""', 2)[-1]
    assert "strftime" not in code
    assert "calendar" not in code
    assert "_EN_MONTH_ABBR" in body


def test_trial_must_read_back_on_before_share():
    assert not ig.trial_unavailable(False, None)
    assert not ig.trial_unavailable(True, "true")
    assert ig.trial_unavailable(True, None)
    assert ig.trial_unavailable(True, "control_not_found")
    assert ig.trial_unavailable(True, "false")
    src = (_PUBLISH / "upload_instagram.py").read_text(encoding="utf-8")
    assert "getAttribute('aria-disabled')==='true'" in src
    assert "[role=gridcell]:not([aria-disabled=true])" in src
