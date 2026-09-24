"""Auto-generate chapters, show notes, and pull-quotes for Hebrew podcasts.

Pipeline: media file -> local faster-whisper transcript (cached) -> Claude
generates chapters / show notes / quotes -> formatted output.
"""

__version__ = "0.9.0"
