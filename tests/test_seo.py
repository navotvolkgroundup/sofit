"""The SEO gate is what stops a full episode shipping without tags/hashtags."""
import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "publish" / "seo.py"
_spec = importlib.util.spec_from_file_location("seo", _SRC)
seo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seo)

GOOD = "תיאור הפרק\n\n0:00 פתיחה\n3:10 נושא\n\n#פודקאסט #הייטק"


def test_complete_metadata_passes():
    assert seo.check("youtube", "כותרת | וויקלי סינק פרק 214", GOOD, "וויקלי סינק, Weekly Sync") == []
    assert seo.check("spotify", "כותרת", GOOD) == []


def test_missing_tags_hashtags_or_chapters_block():
    assert "no youtube_tags" in seo.check("youtube", "t", GOOD, " , ")
    assert "no hashtags in the description" in seo.check("spotify", "t", "0:00 x")
    assert any("chapter" in p for p in seo.check("youtube", "t", "#a", "x"))


def test_more_than_15_hashtags_blocks():
    many = " ".join(f"#t{i}" for i in range(16))
    assert any("ignores" in p for p in seo.check("spotify", "t", many))


def test_hebrew_hashtags_are_found():
    assert seo.hashtags("#וויקלי_סינק and #AI, not a#b") == ["#וויקלי_סינק", "#AI"]


def test_spotify_episode_number_from_plan_episode():
    import sys
    sys.path.insert(0, str(_SRC.parent))
    sys.modules.setdefault("seo", seo)
    spec = importlib.util.spec_from_file_location("upload_spotify", _SRC.parent / "upload_spotify.py")
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    assert sp.episode_number("WS214") == "214"
    assert sp.episode_number("WS_holiday") == ""
