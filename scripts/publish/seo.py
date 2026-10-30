"""SEO gate for full-episode uploads (YouTube long-form + Spotify for Creators).

An episode must never go out without tags and hashtags - the gate is the
uploader's refusal, not a reminder in a skill. Pure functions, no browser.

The monthly playbook (~/.sofit/seo-playbook.md, written by the /sofit-seo
skill from two /last30days runs) carries the soft advice; this file only
enforces the hard platform limits and the must-haves.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

PLAYBOOK = Path.home() / ".sofit" / "seo-playbook.md"
MAX_AGE_DAYS = 31

# Hebrew letters/geresh/underscore count as hashtag text: #וויקלי_סינק
HASHTAG = re.compile(r"(?<!\w)#[\w֐-׿'״׳]+")
CHAPTER = re.compile(r"^\s*0?0:00\b", re.M)

LIMITS = {
    # YouTube drops EVERY hashtag in a description that has more than 15.
    "youtube": {"title": 100, "description": 5000, "tags": 500, "hashtags": 15},
    "spotify": {"title": 200, "description": 4000, "hashtags": 15},
}


def hashtags(text: str) -> list[str]:
    return HASHTAG.findall(text or "")


def check(platform: str, title: str, description: str, tags: str = "") -> list[str]:
    """Problems that must block a submit. Empty list = OK to upload."""
    lim = LIMITS[platform]
    probs = []
    title, description = (title or "").strip(), (description or "").strip()
    if not title:
        probs.append("no title")
    elif len(title) > lim["title"]:
        probs.append(f"title {len(title)} > {lim['title']} chars")
    if not description:
        probs.append("no description")
    elif len(description) > lim["description"]:
        probs.append(f"description {len(description)} > {lim['description']} chars")
    tags_in_desc = hashtags(description)
    if not tags_in_desc:
        probs.append("no hashtags in the description")
    elif len(tags_in_desc) > lim["hashtags"]:
        probs.append(f"{len(tags_in_desc)} hashtags > {lim['hashtags']}"
                     " (YouTube then ignores all of them)")
    if platform == "youtube":
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        if not tag_list:
            probs.append("no youtube_tags")
        elif len(tags) > lim["tags"]:
            probs.append(f"tags {len(tags)} > {lim['tags']} chars")
        if not CHAPTER.search(description):
            probs.append("no 0:00 chapter line (YouTube chapters need one)")
    return probs


def playbook_age_days(path: Path = PLAYBOOK) -> float | None:
    """Days since the playbook was refreshed, None if it doesn't exist."""
    try:
        return (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return None

