"""The SEO gate is what stops a full episode shipping without tags/hashtags."""
import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "publish" / "seo.py"
_spec = importlib.util.spec_from_file_location("seo", _SRC)
seo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seo)

GOOD = "תיאור הפרק\n\n0:00 פתיחה\n3:10 נושא\n12:40 סיום\n\n#פודקאסט #הייטק"
TAGS = "וויקלי סינק, Weekly Sync"


def test_complete_metadata_passes():
    assert seo.check("youtube", "כותרת | וויקלי סינק פרק 214", GOOD, TAGS) == []
    assert seo.check("spotify", "כותרת", GOOD) == []


def test_missing_tags_hashtags_or_chapters_block():
    assert "no youtube_tags" in seo.check("youtube", "t", GOOD, " , ")
    assert "no hashtags in the description (house rule)" in seo.check("spotify", "t", "0:00 x")
    assert any("chapter" in p for p in seo.check("youtube", "t", "#a", "x"))


def test_more_than_15_hashtags_is_a_house_rule_not_a_youtube_limit():
    many = " ".join(f"#t{i}" for i in range(16))
    spotify = " ".join(seo.check("spotify", "t", many))
    youtube = " ".join(seo.check(
        "youtube", "t", many + "\n0:00 a\n1:00 b\n2:00 c", "tag"))
    assert "house rule" in spotify and "house rule" in youtube
    assert "ignores" not in spotify and "ignores" not in youtube
    assert "drops" not in youtube.lower()
    # YouTube Help 6390658 allows more than 60; the message has to say so.
    assert "60" in youtube


def test_hebrew_hashtags_are_found():
    assert seo.hashtags("#וויקלי_סינק and #AI, not a#b") == ["#וויקלי_סינק", "#AI"]


def test_url_fragment_is_not_a_hashtag():
    assert seo.hashtags("https://x.com/#frag") == []
    assert seo.hashtags("see https://x.com/#frag #real") == ["#real"]
    assert seo.hashtags("https://x.com/#frag\n#אחר") == ["#אחר"]


def test_title_and_description_limits():
    assert any("100" in p for p in seo.check("youtube", "x" * 101, GOOD, TAGS))
    spotify = seo.check("spotify", "x" * 201, GOOD)
    assert any("unverified" in p for p in spotify)
    long_spotify = "א" * 4001
    assert any("4000" in p and "chars" in p for p in seo.check("spotify", "t", long_spotify + " #a"))


def test_youtube_description_limit_is_utf8_bytes():
    base = "0:00 a\n1:00 b\n2:00 c\n#a\n"
    room = 5000 - len(base.encode("utf-8"))
    assert seo.check("youtube", "t", base + ("x" * room), "tag") == []
    over = seo.check("youtube", "t", base + ("x" * (room + 1)), "tag")
    assert any("bytes" in p for p in over)
    # 3000 Hebrew letters are well under 5000 characters and over 5000 bytes.
    hebrew = ("א" * 3000) + "\n0:00 א\n1:00 ב\n2:00 ג\n#ת"
    assert len(hebrew) < 5000
    assert len(hebrew.encode("utf-8")) > 5000
    probs = seo.check("youtube", "כותרת", hebrew, "tag")
    assert any("bytes" in p and "stricter" in p for p in probs)
    assert not any(p.startswith("description") and "chars" in p for p in probs)


def test_angle_brackets_rejected_on_youtube_only():
    assert any("<" in p and ">" in p for p in seo.check("youtube", "a < b", GOOD, TAGS))
    assert any("<" in p for p in seo.check("youtube", "t", GOOD + "\n1 < 2", TAGS))
    assert seo.check("spotify", "a < b", GOOD) == []


def test_chapter_count_is_three_to_ten():
    def chapters(n: int) -> str:
        lines = ["0:00 start"] + [f"{i}:00 part" for i in range(1, n)]
        return "\n".join(lines) + "\n#a"

    assert seo.check("youtube", "t", chapters(3), "tag") == []
    assert seo.check("youtube", "t", chapters(10), "tag") == []
    two = " ".join(seo.check("youtube", "t", chapters(2), "tag"))
    assert "at least 3" in two
    eleven = " ".join(seo.check("youtube", "t", chapters(11), "tag"))
    assert "house cap" in eleven
    not_zero = " ".join(seo.check("youtube", "t", "1:00 a\n2:00 b\n3:00 c\n#a", "tag"))
    assert "0:00" in not_zero


def test_bidi_mark_before_zero_timestamp_still_counts():
    desc = "\u200f0:00 פתיחה\n\u200e3:10 נושא\n\u202a12:00 סיום\n#a"
    assert seo.chapter_lines(desc) == ["0:00 פתיחה", "3:10 נושא", "12:00 סיום"]
    assert seo.check("youtube", "t", desc, "tag") == []
    # U+202E RLO, U+202B RLE's sibling, U+202C PDF — the whole U+202A–U+202E block.
    marked = "\u202e0:00 a\n\u202b1:00 b\n\u202c2:00 c\n#a"
    assert seo.check("youtube", "t", marked, "tag") == []
    # Merged onto one line is a single chapter line, not three.
    assert seo.chapter_lines("0:00 פתיחה 3:10 נושא 12:00 סיום") == [
        "0:00 פתיחה 3:10 נושא 12:00 סיום"]


def test_spotify_episode_number_from_plan_episode():
    import sys
    sys.path.insert(0, str(_SRC.parent))
    sys.modules.setdefault("seo", seo)
    spec = importlib.util.spec_from_file_location("upload_spotify", _SRC.parent / "upload_spotify.py")
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    assert sp.episode_number("WS214") == "214"
    assert sp.episode_number("WS_holiday") == ""
