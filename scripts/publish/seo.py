"""SEO gate for full-episode uploads (YouTube long-form + Spotify for Creators).

An episode must never go out without the tags and hashtags the house
requires. The gate is the uploader's refusal, not a reminder in a skill.
Pure functions, no browser.

15 description hashtags is a house rule, not a platform limit. YouTube Help
(answer 6390658) allows more than 60 hashtags and ignores only the ones past
that; the old "more than 15 and YouTube drops every hashtag" behavior is
gone. Spotify has no per-episode tags field. Requiring hashtags there, and
capping either platform at 15, is a house rule.

YouTube chapters here are 3 to 10 lines, the first at 0:00. At least 3 and a
0:00 opener are YouTube's own rules. The cap of 10 is a house rule (the
`sofit` CLI still defaults `--max-chapters` to 12 for other callers; the
/sofit-seo skill passes 10).

YouTube's description field is treated as 5000 UTF-8 bytes. A 5000-character
Hebrew description is roughly twice that, so a character count would let
through text that a byte limit would cut. Spotify's 200-character title cap
is NOT verified against Spotify's docs; it is the cap this gate enforces.

The monthly playbook (~/.sofit/seo-playbook.md, written by the /sofit-seo
skill) carries the soft advice.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

PLAYBOOK = Path.home() / ".sofit" / "seo-playbook.md"
MAX_AGE_DAYS = 31

# A hashtag is a # at the start of the text or after whitespace.
# `https://x.com/#frag` is a URL fragment, not a hashtag.
# Hebrew letters, geresh, and underscore count: #וויקלי_סינק
HASHTAG = re.compile(r"(?:^|(?<=\s))#[\w֐-׿'״׳]+")

# Invisible bidi controls. A line that is really "0:00 …" but starts with
# U+200E/U+200F or U+202A–U+202E is invisible to a ^-anchored timestamp
# regex, and YouTube's own chapter parser drops those lines too. Isolates
# (U+2066–U+2069) are the same class of mark.
_BIDI = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_CHAPTER_LINE = re.compile(r"^(?:\d{1,2}:)?\d{1,2}:\d{2}\b")

# House cap. YouTube will accept more; we don't ship more.
YOUTUBE_CHAPTER_MIN = 3
YOUTUBE_CHAPTER_MAX = 10

LIMITS = {
    "youtube": {
        "title": 100,
        # UTF-8 bytes, not characters. See the module docstring.
        "description": 5000,
        "description_unit": "bytes",
        "tags": 500,
        "hashtags": 15,
    },
    # Spotify's title limit is unverified. 200 is what we enforce, not a
    # figure confirmed against Spotify's documentation. Description length
    # and the hashtag cap are house rules too.
    "spotify": {
        "title": 200,
        "title_verified": False,
        "description": 4000,
        "description_unit": "chars",
        "hashtags": 15,
    },
}


def hashtags(text: str) -> list[str]:
    """Hashtags in `text`. A # after a letter or inside a URL does not count."""
    return HASHTAG.findall(_BIDI.sub("", text or ""))


def chapter_lines(description: str) -> list[str]:
    """Timestamp lines, leading bidi marks removed, internal space collapsed.

    Several timestamps on one line come back as one entry, so a readback can
    tell that a rich editor merged the chapter block.
    """
    out = []
    for raw in (description or "").splitlines():
        line = " ".join(_BIDI.sub("", raw).split())
        if _CHAPTER_LINE.match(line):
            out.append(line)
    return out


def _starts_at_zero(line: str) -> bool:
    stamp = re.match(r"^((?:\d{1,2}:)?\d{1,2}:\d{2})", line)
    if not stamp:
        return False
    return all(int(part) == 0 for part in stamp.group(1).split(":"))


def _chapter_problems(description: str) -> list[str]:
    lines = chapter_lines(description)
    n = len(lines)
    if n < YOUTUBE_CHAPTER_MIN:
        return [f"{n} chapter lines; YouTube needs at least "
                f"{YOUTUBE_CHAPTER_MIN}, the first at 0:00"]
    if n > YOUTUBE_CHAPTER_MAX:
        return [f"{n} chapter lines > {YOUTUBE_CHAPTER_MAX} (house cap)"]
    if not _starts_at_zero(lines[0]):
        return ["first chapter line is not 0:00"]
    return []


def check(platform: str, title: str, description: str, tags: str = "") -> list[str]:
    """Problems that must block a submit. Empty list = OK to upload."""
    lim = LIMITS[platform]
    probs = []
    title, description = (title or "").strip(), (description or "").strip()
    if not title:
        probs.append("no title")
    elif len(title) > lim["title"]:
        if platform == "spotify" and not lim.get("title_verified", True):
            probs.append(
                f"title {len(title)} > {lim['title']} chars "
                "(Spotify's title limit is unverified; this is the cap we enforce)")
        else:
            probs.append(f"title {len(title)} > {lim['title']} chars")
    if platform == "youtube" and ("<" in title + description or ">" in title + description):
        probs.append("title or description contains < or >")
    if not description:
        probs.append("no description")
    elif lim.get("description_unit") == "bytes":
        n = len(description.encode("utf-8"))
        if n > lim["description"]:
            probs.append(
                f"description {n} bytes > {lim['description']} "
                "(UTF-8 byte cap; Hebrew is multi-byte, so this is stricter "
                "than a 5000-character count)")
    elif len(description) > lim["description"]:
        probs.append(f"description {len(description)} > {lim['description']} chars")
    tags_in_desc = hashtags(description)
    if not tags_in_desc:
        # Neither platform requires hashtags. The house does.
        probs.append("no hashtags in the description (house rule)")
    elif len(tags_in_desc) > lim["hashtags"]:
        extra = ""
        if platform == "youtube":
            extra = "; YouTube's own limit is above 60"
        probs.append(
            f"{len(tags_in_desc)} hashtags > {lim['hashtags']} "
            f"(house rule, not a YouTube limit{extra})")
    if platform == "youtube":
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        if not tag_list:
            probs.append("no youtube_tags")
        elif len(tags) > lim["tags"]:
            probs.append(f"tags {len(tags)} > {lim['tags']} chars")
        if description:
            probs.extend(_chapter_problems(description))
    return probs


def playbook_age_days(path: Path = PLAYBOOK) -> float | None:
    """Days since the playbook was refreshed, None if it doesn't exist."""
    try:
        return (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return None
