"""Per-show folders: which show is active, and the one-way single-show migration."""
import json

import pytest

from sofit import generate, shows


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SOFIT_SHOW", raising=False)
    monkeypatch.delenv("WS_SCRAPER_PROFILE", raising=False)
    return tmp_path


def test_no_show_is_the_old_single_show_layout(home):
    assert shows.slug() is None
    assert shows.home() == home / ".sofit"
    assert shows.browser_profile() == home / ".ws-scraper" / "profile"


def test_env_beats_default_show_file(home, monkeypatch):
    (home / ".sofit").mkdir()
    (home / ".sofit" / "default-show").write_text("weekly-sync\n")
    assert shows.home() == home / ".sofit" / "shows" / "weekly-sync"
    monkeypatch.setenv("SOFIT_SHOW", "other")
    assert shows.home() == home / ".sofit" / "shows" / "other"


def test_each_show_names_its_own_browser_profile(home, monkeypatch):
    monkeypatch.setenv("SOFIT_SHOW", "other")
    d = home / ".sofit" / "shows" / "other"
    d.mkdir(parents=True)
    (d / "publish.json").write_text(json.dumps({"browser_profile": "~/profiles/other"}))
    assert shows.browser_profile() == home / "profiles" / "other"


def test_migrate_moves_symlinks_tags_and_is_rerunnable(home):
    r = home / ".sofit"
    (r / "plans").mkdir(parents=True)
    (r / "plans" / "p.json").write_text("{}")
    (r / "publish.json").write_text("{}")
    (r / "env").write_text("KEY=1")
    (r / "performance.jsonl").write_text(
        '{"clip": "clip-1"}\nnot json\n{"clip": "clip-2", "show": "x"}\n')

    dry = shows.migrate("weekly-sync")
    assert dry and not (r / "shows").exists()  # dry run touches nothing

    shows.migrate("weekly-sync", apply=True)
    dest = r / "shows" / "weekly-sync"
    assert (r / "plans").is_symlink() and (r / "plans" / "p.json").exists()
    assert (dest / "publish.json").exists() and not (r / "env").is_symlink()
    rows = (dest / "performance.jsonl").read_text().splitlines()
    assert json.loads(rows[0])["show"] == "weekly-sync"
    assert rows[1] == "not json"                     # kept, not dropped
    assert json.loads(rows[2])["show"] == "x"        # existing tag wins
    assert json.loads((dest / "show.json").read_text()) == shows.SHOW_TEMPLATE
    assert shows.home() == dest
    assert shows.migrate("weekly-sync", apply=True) == []


def test_brand_rule_and_blurb_come_from_show_json():
    show = {"name": "וויקלי סינק", "never": ["Weekly Sync"], "hosts": ["A", "B"],
            "about": "Hebrew tech podcast"}
    assert "'וויקלי סינק'" in generate.brand_rule(show)
    assert "'Weekly Sync'" in generate.brand_rule(show)
    assert generate.brand_rule({}) == ""
    assert generate.show_blurb(show) == \
        "a Hebrew tech podcast (וויקלי סינק / Weekly Sync, hosts A and B)"
    assert generate.show_blurb({}) == "a Hebrew podcast"
