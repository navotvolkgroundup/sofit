"""Pending Instagram posts and sofit-binary lookup for the metrics scraper.

Live-reel discovery writes the performance log itself, because `sofit
publish-log` stamps today's date and the T+3 scrape would then skip a post
that went out on time. These tests stay on that decision and on finding the
sofit binary. They never open a browser or the real performance log.
"""
import datetime
import importlib.util
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "publish" / "scrape.py"
_spec = importlib.util.spec_from_file_location("sofit_scrape_pending", _SRC)
scrape = importlib.util.module_from_spec(_spec)
sys.modules["sofit_scrape_pending"] = scrape
_spec.loader.exec_module(scrape)

TODAY = datetime.date(2026, 10, 8)


def _at(hour, minute):
    return datetime.datetime(2026, 10, 8, hour, minute)


def _plan(directory, name, posts, episode="WS212", **extra):
    doc = {"episode": episode, "posts": posts, **extra}
    path = directory / name
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _post(clip, caption, date="2026-10-01", **extra):
    return {"clip": clip, "date": date, "instagram": caption, **extra}


def _pending(monkeypatch, directory, rows, now):
    monkeypatch.setattr(scrape, "PLANS_DIR", directory)
    return scrape._pending_ig_posts(rows, TODAY, now=now)


def _exe(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


class _Page:
    """Just enough of a Playwright page for live-reel discovery."""

    def __init__(self, links, bodies):
        self.links = links
        self.bodies = bodies
        self.gotos = []
        self.wheels = 0
        self.mouse = self
        self.current = None

    def wheel(self, x, y):
        self.wheels += 1

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.current = url
        if "BOOM" in url:
            raise RuntimeError("navigation failed")

    def wait_for_timeout(self, n):
        pass

    def eval_on_selector_all(self, sel, expr):
        return list(self.links)

    def inner_text(self, sel):
        return self.bodies.get(self.current, "")


def test_clip_base_collapses_variants_and_keeps_bonus_ids():
    assert scrape._clip_base("clip-18.pers") == "clip-18"
    assert scrape._clip_base("clip-5.hook1.pers") == "clip-5"
    assert scrape._clip_base("WS212_clip-5.hook1.mp4") == "clip-5"
    assert scrape._clip_base("clip-5") == "clip-5"
    assert scrape._clip_base("clip-5") != scrape._clip_base("clip-50")
    assert scrape._clip_base("car.ab-accent-box") == "car"
    assert scrape._clip_base("pins") == "pins"
    assert scrape._clip_base(None) == ""
    assert scrape._clip_base("") == ""


def test_pending_ignores_facebook_youtube_and_non_ig_posts(tmp_path, monkeypatch):
    _plan(tmp_path, "publish-plan-facebook.json",
          [_post("clip-1", "פייסבוק")])
    _plan(tmp_path, "publish-plan-212-yt.json",
          [_post("clip-2", "יוטיוב")])
    _plan(tmp_path, "publish-plan-ttyt.json",
          [_post("clip-3", "טיקטוק ויוטיוב")])
    _plan(tmp_path, "publish-plan-ws212.json", [
        {"clip": "clip-4", "date": "2026-10-01", "tiktok": "רק טיקטוק"},
        {"clip": "clip-4b", "date": "2026-10-01", "tiktok": "בלי אינסטגרם"},
        {"clip": "clip-4c", "date": "2026-10-01", "instagram": 12},
        _post("clip-5", "כן אינסטגרם"),
        {"date": "2026-10-01", "instagram": "בלי מזהה קליפ"},
    ])
    # A plan with no episode used to throw once it reached the append.
    (tmp_path / "publish-plan-no-episode.json").write_text(
        json.dumps({"posts": [_post("clip-8", "בלי פרק")]}), encoding="utf-8")
    (tmp_path / "publish-plan-broken.json").write_text("{", encoding="utf-8")

    pending = _pending(monkeypatch, tmp_path, [], _at(18, 0))
    assert [p["clip"] for p in pending] == ["clip-5"]


def test_pending_waits_for_the_local_slot_on_the_same_day(tmp_path, monkeypatch):
    _plan(tmp_path, "publish-plan-ws212.json", [
        _post("clip-1", "אתמול", date="2026-10-07"),
        _post("clip-2", "היום", date="2026-10-08"),
        _post("clip-3", "מחר", date="2026-10-09"),
        {"clip": "clip-4", "date": "not-a-date", "instagram": "תאריך שבור"},
    ], time_local="13:00")

    morning = [p["clip"] for p in _pending(monkeypatch, tmp_path, [], _at(12, 59))]
    assert morning == ["clip-1"]

    on_time = [p["clip"] for p in _pending(monkeypatch, tmp_path, [], _at(13, 0))]
    assert on_time == ["clip-1", "clip-2"]

    _plan(tmp_path, "publish-plan-early.json", [
        _post("clip-9", "מוקדם", date="2026-10-08"),
    ], episode="WS213", time_local="09:30")
    assert "clip-9" in [p["clip"] for p in _pending(monkeypatch, tmp_path, [], _at(9, 30))]
    assert "clip-9" not in [p["clip"] for p in _pending(monkeypatch, tmp_path, [], _at(9, 29))]

    # A clock we cannot parse must not hide a post that is otherwise due.
    _plan(tmp_path, "publish-plan-bad-clock.json", [
        _post("clip-7", "שעון שבור", date="2026-10-08"),
    ], episode="WS214", time_local="noon")
    assert "clip-7" in [p["clip"] for p in _pending(monkeypatch, tmp_path, [], _at(8, 0))]


def test_pending_matches_the_base_clip_and_dedupes(tmp_path, monkeypatch):
    _plan(tmp_path, "publish-plan-a.json", [
        _post("clip-5.hook1.pers", "וריאנט ראשון", speaker="נועה"),
        _post("clip-5", "אותו קליפ שוב"),
        _post("clip-50", "קליפ אחר"),
    ])
    _plan(tmp_path, "publish-plan-b.json", [
        _post("clip-5.pers", "אותו בסיס בתוכנית אחרת"),
    ], episode="WS300")

    rows = [
        {"episode": "WS212", "clip": "clip-5", "platform": "tiktok"},
        {"episode": "WS212", "clip": "clip-50", "platform": "instagram"},
        {"episode": "WS299", "clip": "clip-5", "platform": "instagram"},
    ]
    pending = _pending(monkeypatch, tmp_path, rows, _at(18, 0))
    assert [(p["episode"], p["clip"]) for p in pending] == [
        ("WS212", "clip-5.hook1.pers"),
        ("WS300", "clip-5.pers"),
    ]

    # The base id already logged on Instagram is not queued again.
    rows.append({"episode": "WS212", "clip": "clip-5.hook2", "platform": "instagram"})
    pending = _pending(monkeypatch, tmp_path, rows, _at(18, 0))
    assert [p["episode"] for p in pending] == ["WS300"]


def test_pending_skips_cancelled_and_deleted_posts(tmp_path, monkeypatch):
    # A cancelled variant must not occupy the dedupe slot of a live sibling.
    _plan(tmp_path, "publish-plan-ws212.json", [
        _post("clip-1", "בוטל", status="cancelled"),
        _post("clip-2", "נמחק", status="Deleted"),
        _post("clip-3", "אינסטגרם בוטל", instagram_status="cancelled"),
        _post("clip-5.hook1.pers", "וריאנט בוטל", status="canceled"),
        _post("clip-5", "הוריאנט החי"),
        _post("clip-6", "בוטל רק באינסטגרם",
              status={"instagram": "cancelled", "tiktok": "posted"}),
        _post("clip-7", "טיקטוק בוטל אבל אינסטגרם חי",
              status={"tiktok": "deleted", "instagram": "posted"}),
        _post("clip-8", "עדיין באוויר", status="scheduled"),
    ])
    # Status nested on the instagram object, which is not a caption string.
    assert scrape._ig_post_dropped(
        {"instagram": {"status": "deleted", "text": "נמחק"}})
    assert not scrape._ig_post_dropped({"status": {"tiktok": "deleted"}})

    pending = _pending(monkeypatch, tmp_path, [], _at(18, 0))
    assert [p["clip"] for p in pending] == ["clip-5", "clip-7", "clip-8"]


def test_pending_strips_bidi_marks_and_keeps_the_plan_fields(tmp_path, monkeypatch):
    caption = "\u202bשלום\u202c עולם " + ("א" * 40)
    _plan(tmp_path, "publish-plan-ws212.json", [
        _post("clip-1.pers", caption, speaker="תור"),
    ])
    pending = _pending(monkeypatch, tmp_path, [], _at(18, 0))
    assert len(pending) == 1
    post = pending[0]
    assert "\u202b" not in post["head"] and "\u202c" not in post["head"]
    assert post["head"].startswith("שלום עולם ")
    assert len(post["head"]) == 40
    assert post["date"] == "2026-10-01"
    assert post["speaker"] == "תור"
    assert post["time_local"] == "13:00"  # plan omitted it; the slot defaults


def test_sofit_bin_checks_env_then_venv_then_path_then_interpreter(monkeypatch, tmp_path):
    env_bin = _exe(tmp_path, "env/sofit")
    venv_bin = _exe(tmp_path, "venv/sofit")
    path_bin = _exe(tmp_path, "path/sofit")
    py = _exe(tmp_path, "py/python")
    interp = py.resolve().parent / "sofit"
    monkeypatch.setattr(scrape, "_repo_sofit", lambda: venv_bin)
    monkeypatch.setattr(scrape.shutil, "which", lambda name: str(path_bin))
    monkeypatch.setattr(scrape.sys, "executable", str(py))

    monkeypatch.setenv("SOFIT_BIN", str(env_bin))
    assert scrape._sofit_bin() == env_bin

    monkeypatch.setenv("SOFIT_BIN", str(tmp_path / "missing" / "sofit"))
    assert scrape._sofit_bin() == venv_bin

    venv_bin.unlink()
    assert scrape._sofit_bin() == path_bin

    monkeypatch.setattr(scrape.shutil, "which", lambda name: None)
    interp.write_text("x", encoding="utf-8")
    assert scrape._sofit_bin() == interp

    interp.unlink()
    assert scrape._sofit_bin() == interp
    assert "/home/box" not in str(scrape._sofit_bin())


def test_sofit_bin_expands_a_home_relative_override(monkeypatch, tmp_path):
    _exe(tmp_path, "sofit")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SOFIT_BIN", "~/sofit")
    monkeypatch.setattr(scrape, "_repo_sofit", lambda: tmp_path / "no" / "sofit")
    monkeypatch.setattr(scrape.shutil, "which", lambda name: None)
    assert scrape._sofit_bin() == tmp_path / "sofit"


def test_sofit_bin_candidates_follow_the_lookup_order(monkeypatch, tmp_path):
    py = _exe(tmp_path, "py/python")
    monkeypatch.setenv("SOFIT_BIN", "/opt/custom/sofit")
    monkeypatch.setattr(scrape, "_repo_sofit", lambda: Path("/repo/.venv/bin/sofit"))
    monkeypatch.setattr(scrape.shutil, "which", lambda name: "/usr/bin/sofit")
    monkeypatch.setattr(scrape.sys, "executable", str(py))
    assert scrape._sofit_bin_candidates() == [
        Path("/opt/custom/sofit"),
        Path("/repo/.venv/bin/sofit"),
        Path("/usr/bin/sofit"),
        py.resolve().parent / "sofit",
    ]

    monkeypatch.delenv("SOFIT_BIN", raising=False)
    monkeypatch.setattr(scrape.shutil, "which", lambda name: None)
    assert scrape._sofit_bin_candidates() == [
        Path("/repo/.venv/bin/sofit"),
        py.resolve().parent / "sofit",
    ]


def test_repo_sofit_is_the_checkout_venv():
    root = Path(scrape.__file__).resolve().parents[2]
    assert scrape._repo_sofit() == root / ".venv" / "bin" / "sofit"


def test_scraper_has_no_machine_specific_path():
    src = Path(scrape.__file__).read_text(encoding="utf-8")
    assert "/home/box" not in src
    assert "sofit-venv" not in src
    assert "/home/" not in src


def test_ig_reel_row_keeps_the_plan_date_and_base_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "CLIPS_DIR", tmp_path)
    rows = [
        {"episode": "WS212", "clip": "clip-5", "platform": "tiktok",
         "hook": "זו הכותרת", "variant": 2, "duration": 21.5,
         "hook_style": "flash", "views": 900, "post_url": "https://tiktok.example/1"},
        {"episode": "WS212", "clip": "clip-5.hook1", "platform": "youtube",
         "speaker": "מאחורה", "hook": "הוק אחר"},
    ]
    post = {"episode": "WS212", "clip": "clip-5.hook1.pers", "date": "2026-10-01",
            "speaker": "נועה", "time_local": "13:00"}
    row = scrape._ig_reel_row(post, rows, "https://www.instagram.com/reel/ABC/", TODAY)
    assert row["date"] == "2026-10-01"
    assert row["clip"] == "clip-5"
    assert row["variant"] == 2
    assert row["hook"] == "זו הכותרת"
    assert row["speaker"] == "נועה"
    assert row["hook_style"] == "persistent"
    assert row["duration"] == 21.5
    assert row["time_local"] == "13:00"
    assert row["platform"] == "instagram"
    assert "views" not in row

    # No plan speaker: the sibling's speaker, else a later row for the same clip.
    post.pop("speaker")
    assert scrape._ig_reel_row(post, rows, "https://ig.example/a/", TODAY)["speaker"] == "מאחורה"
    rows[1].pop("speaker")
    bare = dict(rows[0])
    bare.pop("speaker", None)
    assert "speaker" not in scrape._ig_reel_row(
        post, [bare], "https://ig.example/a/", TODAY)


def test_ig_reel_row_needs_a_sibling_hook_and_can_probe_duration(tmp_path, monkeypatch):
    assert scrape._ig_reel_row(
        {"episode": "WS212", "clip": "clip-5", "date": "2026-10-01"},
        [{"episode": "WS212", "clip": "clip-5", "platform": "tiktok"}],
        "https://ig.example/x/", TODAY) is None

    probed = []

    def _probe(path):
        probed.append(path.name)
        return 12.25

    monkeypatch.setattr(scrape, "CLIPS_DIR", tmp_path)
    monkeypatch.setattr(scrape, "_probe_duration", _probe)
    (tmp_path / "WS212_clip-5.mp4").write_bytes(b"not a video")
    post = {"episode": "WS212", "clip": "clip-5.hook1.pers", "date": "2026-10-01",
            "time_local": "13:00"}
    rows = [{"episode": "WS212", "clip": "clip-5", "hook": "הוק", "variant": 0}]
    row = scrape._ig_reel_row(post, rows, "https://ig.example/x/", TODAY)
    assert probed == ["WS212_clip-5.mp4"]
    assert row["duration"] == 12.25
    assert row["date"] == "2026-10-01"

    # The rendered filename wins over the base id when both files are there.
    (tmp_path / "WS212_clip-5.hook1.pers.mp4").write_bytes(b"x")
    scrape._ig_reel_row(post, rows, "https://ig.example/x/", TODAY)
    assert probed[-1] == "WS212_clip-5.hook1.pers.mp4"


def test_discover_logs_the_plan_date_once_and_reports_what_is_missing(tmp_path, monkeypatch, capsys):
    plans = tmp_path / "plans"
    plans.mkdir()
    _plan(plans, "publish-plan-facebook.json",
          [_post("clip-1", "פייסבוק בלבד")])
    _plan(plans, "publish-plan-ws212.json", [
        _post("clip-5.hook1.pers", "שלום\u202b עולם", speaker="נועה"),
        _post("clip-9", "אין את זה בשום מקום"),
    ])
    log = tmp_path / "performance.jsonl"
    monkeypatch.setattr(scrape, "PLANS_DIR", plans)
    monkeypatch.setattr(scrape, "PERF_LOG", log)
    monkeypatch.setattr(scrape, "IG_PROFILE", "weeklysync")

    known = "https://www.instagram.com/reel/KNOWN/"
    new = "https://www.instagram.com/reel/NEW/?igsh=1"
    page = _Page(
        links=[
            "https://www.instagram.com/reel/BOOM/?igsh=1",
            known + "?igsh=1",
            new,
        ],
        bodies={new: "פתיח שלום\u202b עולם סיום"},
    )
    rows = [
        {"episode": "WS212", "clip": "clip-5", "platform": "tiktok",
         "hook": "הוק אח", "variant": 1, "speaker": "אחר", "duration": 18.0,
         "views": 10, "post_url": "https://www.tiktok.com/video/1"},
        {"episode": "WS212", "clip": "clip-1", "platform": "instagram",
         "hook": "פייסבוק", "post_url": known},
    ]
    logged = scrape._discover_live_ig_reels(page, rows, TODAY)
    assert logged == [{
        "clip": "clip-5", "url": "https://www.instagram.com/reel/NEW/",
        "episode": "WS212", "date": "2026-10-01",
    }]
    assert page.gotos[0].endswith("/reels/")
    assert any("BOOM" in url for url in page.gotos)
    assert not any("KNOWN" in url for url in page.gotos)
    assert any(url == new for url in page.gotos)
    assert page.wheels == 4

    written = json.loads(log.read_text(encoding="utf-8"))
    assert written["date"] == "2026-10-01"
    assert written["clip"] == "clip-5"
    assert written["hook"] == "הוק אח"
    assert written["speaker"] == "נועה"
    assert written["hook_style"] == "persistent"
    assert written["post_url"] == "https://www.instagram.com/reel/NEW/"
    assert "הוק אח" in log.read_text(encoding="utf-8")  # not escaped
    assert "views" not in written
    assert rows[-1]["post_url"] == written["post_url"]

    again = scrape._discover_live_ig_reels(page, rows, TODAY)
    assert again == []
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1

    err = capsys.readouterr().err
    assert "WS212/clip-9(2026-10-01)" in err
    assert "clip-5" not in err.split("still missing")[-1]
