"""Explicit direct HTTPS video links, using the same bounded cache and probe."""
from dataclasses import replace
from urllib.parse import unquote, urlsplit

from .footage import Candidate, Cue, FootageCache, canonical_url


class DirectProvider:
    name = "direct"

    def resolve(self, url: str, cache: FootageCache, *, cancelled=None) -> Candidate:
        url = canonical_url(url)
        title = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "Direct video"
        provisional = Candidate(self.name, url, url, title, 0, 0, 0, original_media_url=url)
        _, metadata = cache.retrieve(provisional, **({"cancelled": cancelled} if cancelled else {}))
        info = metadata["video"]
        return replace(provisional, duration=info["duration"], width=info["width"],
                       height=info["height"], size=metadata["size"])

    def search(self, query: str, limit: int = 8) -> list[Candidate]:
        return []  # links are supplied explicitly, never invented by the planner

    def subtitles(self, candidate: Candidate) -> list[Cue]:
        return []
