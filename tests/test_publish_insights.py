"""Insights rebuild and the read-only session probe.

Neither test opens a browser or the real performance log. The session probe
is checked on its platform table only: cookie values are never part of it.
"""
import importlib.util
import json
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


insights = _load("build_insights")
session = _load("session_check")


def test_build_insights_ranks_and_keeps_the_prior_pattern():
    rows = [
        {"platform": "tiktok", "avg_watch_s": 10, "hook": "long", "views": 1,
         "speaker": "s", "episode": "E", "date": "d", "clip": "c1",
         "post_url": "https://tiktok.example/1", "hook_hold_3s": 0.4},
        {"platform": "tiktok", "avg_watch_s": 2, "hook": "short", "views": 9,
         "speaker": "s", "episode": "E", "date": "d", "clip": "c2",
         "post_url": "https://tiktok.example/2"},
        {"platform": "instagram", "views": 5, "hook": "ig", "speaker": "s",
         "episode": "E", "date": "d", "clip": "c3",
         "post_url": "https://instagram.example/3"},
        {"platform": "linkedin", "hook": "li", "speaker": "s", "episode": "E",
         "date": "d", "clip": "c4"},
    ]
    log = {"status": "ok", "updated": "t", "failed": [], "failed_urls": ["u"],
           "sessions": {"tiktok": True, "instagram": False}}
    out = insights.build(
        rows, {"pattern": "keep me", "note": "n"}, log, "S1",
        note_health="digest ok")
    assert out["pattern"] == "keep me"
    assert out["note"] == "n"
    assert out["scrape_stamp"] == "S1"
    assert out["rows"] == 4
    # A row with no post_url still occupies a slot, same as the box script.
    assert out["unique_posts"] == 4
    assert out["by_platform"]["tiktok"]["top"]["hook"] == "long"
    assert out["by_platform"]["tiktok"]["top"]["hook_hold_3s"] == 0.4
    assert out["by_platform"]["tiktok"]["bottom"]["hook"] == "short"
    assert out["by_platform"]["instagram"]["metric"] == "views"
    assert out["by_platform"]["instagram"]["top"]["hook"] == "ig"
    assert out["by_platform"]["linkedin"]["note"] == "LinkedIn metrics mostly pending"
    assert out["by_platform"]["linkedin"]["top"] is None
    assert out["working_hooks"][0]["hook"] == "long"
    assert out["weak_hooks"][0]["hook"] == "short"
    assert out["scrape_health"]["session_dead"] is True
    assert out["scrape_health"]["digest_note"] == "digest ok"
    assert out["scrape_health"]["failed_urls_count"] == 1
    assert "/home/box" not in json.dumps(out)
    assert "performance.jsonl + scrape log" == out["updated_from"]

    replaced = insights.build(
        rows, {"pattern": "keep me"}, log, None, pattern="new pattern")
    assert replaced["pattern"] == "new pattern"
    assert replaced["scrape_stamp"] is None


def test_session_check_lists_publishers_without_cookie_values(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "_cfg", lambda: {
        "li_company_id": "123", "fb_asset_id": "456"})
    rows = session.checks()
    names = [row[0] for row in rows]
    assert names == ["tiktok", "instagram", "youtube", "linkedin", "facebook"]
    cookies = {row[0]: row[3] for row in rows}
    assert cookies["tiktok"][1] == "sessionid"
    assert cookies["youtube"][1] == "SID"
    assert cookies["linkedin"][1] == "li_at"
    assert "123" in dict(zip(names, rows))["linkedin"][1]
    assert "asset_id=456" in dict(zip(names, rows))["facebook"][1]
    # YouTube and Facebook mirror the Mac UA their uploaders send.
    assert rows[2][2] == "mac" and rows[4][2] == "mac"
    src = (_PUBLISH / "session_check.py").read_text(encoding="utf-8")
    assert "/home/box" not in src
    assert "cookie VALUES are never printed" in src or "Cookie values" in src
