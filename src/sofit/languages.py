"""Supported languages. Add a language by adding one line here.

code -> (name, default Whisper model). The code is what Whisper and --lang take
(ISO 639-1); the name is swapped into every generation prompt, so chapters, show
notes, quotes and hooks come out in the episode's own language.
"""

LANGUAGES = {
    "he": ("Hebrew", "ivrit-ai/whisper-large-v3-turbo-ct2"),  # Hebrew-tuned turbo
    "en": ("English", "large-v3-turbo"),
}

DEFAULT_LANG = "he"


def name(code: str) -> str:
    return LANGUAGES[code][0]


def default_model(code: str) -> str:
    return LANGUAGES[code][1]
