"""Render vertical (or any-aspect) video clips with burned Hebrew captions.

Self-contained port of SocialClipper's clipper.py video-rendering path, adapted
for sofit. No dependency on socialclipper.

The public entry point is `render_clips`. It takes a source video and a list of
clip dicts in the sofit clips.json shape and writes one cropped-to-fill
mp4 per clip with the clip's Hebrew word timings burned in as captions.

Stack: stdlib + ffmpeg/ffprobe (external) + Pillow. python-bidi is used when
importable so mixed Hebrew/Latin captions render in correct visual order; if it
is missing the raw text is drawn instead (no crash).

Caption rendering has two paths:
  * libass -- used when the local ffmpeg was built with the `subtitles` filter.
  * Pillow per-frame overlay -- the fallback used when ffmpeg lacks libass. This
    is the path that runs on machines without libass and renders Hebrew
    correctly. Ported faithfully from SocialClipper.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:  # optional; captions still render (in logical order) without it
    from bidi.algorithm import get_display as _bidi_display
except Exception:  # pragma: no cover - depends on env
    _bidi_display = None


# ---------------------------------------------------------------------------
# ffmpeg capability probe
# ---------------------------------------------------------------------------

def _check_ffmpeg_subtitles_support() -> bool:
    """Check if ffmpeg was built with the subtitles filter (requires libass)."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True, text=True, timeout=10,
        )
        return "subtitles" in result.stdout
    except Exception:
        return False


_HAS_SUBTITLES_FILTER = _check_ffmpeg_subtitles_support()

# Wall clock for the ENCODE paths (not the fast capability probes). A fixed 300s
# killed a real render: clip-5 of WS204 starts at 41:45 in an 8GB 1080p50 file and
# was encoding alongside five sibling renders. Encode time scales with clip length,
# resolution, fps and how many renders share the machine, so the ceiling is
# generous and overridable rather than tuned.
FFMPEG_TIMEOUT = int(os.environ.get("SOFIT_FFMPEG_TIMEOUT") or 1800)

# Peak ceiling for clip audio. Rendered clips were coming out at 0.0 dBFS (WS205
# clip-1 and clip-2), which clips on playback and again when a platform re-encodes.
# level=disabled matters: alimiter's default auto-level RAISES quiet material up to
# the ceiling, so only peaks above it get touched this way and quieter clips are
# left exactly as they were. 0.89 lands around -1 dBFS after AAC overshoot.
_AUDIO_LIMITER = "alimiter=limit=0.89:level=disabled"


# ---------------------------------------------------------------------------
# Fonts (Hebrew-capable)
# ---------------------------------------------------------------------------

# macOS ships Arial variants with full Hebrew glyph coverage. These are tried in
# order for the Pillow path. `font` (a path) overrides this list.
_HEBREW_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/ArialHB.ttc",
    "/Library/Fonts/Arial.ttf",
    # Linux fallbacks
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansHebrew-Bold.ttf",
]

# Default family name for the libass force_style path. Arial carries Hebrew on
# macOS; libass resolves it via fontconfig.
_DEFAULT_LIBASS_FONT = "Arial"


def _resolve_pillow_font_path(font: str | None):
    """Return a font file path for Pillow, preferring an explicit override."""
    candidates = []
    if font and os.path.exists(font):
        candidates.append(font)
    candidates.extend(_HEBREW_FONT_CANDIDATES)
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def _libass_font_name(font: str | None) -> str:
    """Family name to hand libass' force_style."""
    if not font:
        return _DEFAULT_LIBASS_FONT
    if os.path.exists(font):
        return Path(font).stem
    return font


def _shape_bidi(text: str) -> str:
    """Reorder a caption line for correct visual RTL/LTR display.

    Uses python-bidi when available; otherwise returns the text unchanged.
    """
    if _bidi_display is None:
        return text
    try:
        return _bidi_display(text)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Aspect / crop
# ---------------------------------------------------------------------------

ASPECT_RESOLUTIONS = {
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "9:16": (1080, 1920),
}


def _target_resolution(aspect_ratio: str) -> tuple[int, int]:
    """Resolve an aspect string like "9:16" to a concrete even (w, h)."""
    if aspect_ratio in ASPECT_RESOLUTIONS:
        return ASPECT_RESOLUTIONS[aspect_ratio]
    try:
        w_str, h_str = aspect_ratio.split(":")
        ratio = float(w_str) / float(h_str)
    except Exception:
        return ASPECT_RESOLUTIONS["9:16"]

    def _even(n: float) -> int:
        n = int(round(n))
        return n - (n % 2)

    if ratio < 1:          # portrait
        return (_even(1920 * ratio), 1920)
    if ratio > 1:          # landscape
        return (1920, _even(1920 / ratio))
    return (1080, 1080)    # square


def _zoom_out_factor() -> float:
    """How much WIDER than a fill-crop to frame a portrait clip (1.0 = fill).

    A 16:9 source cropped to fill 9:16 shows only ~32% of the frame width, so
    two people at a desk end up uncomfortably close. Values above 1.0 widen
    the crop and letterbox the leftover height over a blurred copy of the same
    shot - the usual podcast-clip look. `SOFIT_ZOOM_OUT=1.35` etc; 1.0 keeps
    the old behaviour.
    """
    raw = os.environ.get("SOFIT_ZOOM_OUT")
    if raw is None and os.environ.get("SOFIT_BRAND") != "off":
        try:  # brand-kit default, same place as caption_div
            cfg = json.loads((Path.home() / ".sofit" / "brand.json").read_text())
            raw = cfg.get("zoom_out")
        except (OSError, ValueError):
            raw = None
    try:
        v = float(raw) if raw is not None else 1.0
    except (TypeError, ValueError):
        return 1.0
    return max(1.0, min(2.4, v))


def _build_crop_vf(aspect_ratio: str, crop_position: float = 0.5) -> str:
    """Build an ffmpeg -vf string that crops (instead of padding) to fill the frame.

    crop_position: 0.0 = left/top edge, 0.5 = center, 1.0 = right/bottom edge.
    Crop-to-fill for every aspect -- never letterbox, unless SOFIT_ZOOM_OUT
    asks for a wider frame than a fill-crop can give (see _zoom_out_factor).
    """
    tw, th = _target_resolution(aspect_ratio)
    target_ratio = tw / th
    cp = max(0.0, min(1.0, crop_position))

    z = _zoom_out_factor()
    if target_ratio < 1 and z > 1.0:
        # Widen the crop by z, then fit it to the frame width and centre it
        # over a blurred, frame-filling copy of the same crop.
        cw = f"min(iw\\,ih*{tw}/{th}*{z})"
        crop = f"crop={cw}:ih:(iw-{cw})*{cp}:0"
        return (
            f"{crop},split=2[zb][zf];"
            f"[zb]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},gblur=sigma=28[zbb];"
            f"[zf]scale={tw}:-2[zff];"
            f"[zbb][zff]overlay=(W-w)/2:(H-h)/2,scale={tw}:{th}"
        )

    if target_ratio > 1:
        # Target is landscape: crop height, keep full width
        crop = f"crop=iw:iw*{th}/{tw}:0:(ih-iw*{th}/{tw})*{cp}"
    elif target_ratio < 1:
        # Target is portrait: crop width, keep full height
        crop = f"crop=ih*{tw}/{th}:ih:(iw-ih*{tw}/{th})*{cp}:0"
    else:
        # Target is square: crop to the smaller dimension
        crop = f"crop=min(iw\\,ih):min(iw\\,ih):(iw-min(iw\\,ih))*{cp}:(ih-min(iw\\,ih))*{cp}"

    return f"{crop},scale={tw}:{th}"


# ---------------------------------------------------------------------------
# Face-aware crop position
# ---------------------------------------------------------------------------

_FACE_MODEL = Path(__file__).parent / "data" / "face_detection_yunet_2023mar.onnx"


def _crop_position_for_face(face_cx: float, crop_w_frac: float) -> float:
    """Map a face center-x fraction [0,1] to the crop_position [0,1] that centers
    a crop of width `crop_w_frac` (fraction of source width) on that face.

    crop_position places the crop's LEFT edge at (iw-crop_w)*cp, so to center the
    crop on face_cx we solve (iw-crop_w)*cp + crop_w/2 == face_cx*iw.
    """
    if not 0.0 < crop_w_frac < 1.0:
        return 0.5
    cp = (face_cx - crop_w_frac / 2) / (1.0 - crop_w_frac)
    return max(0.0, min(1.0, cp))


def _detect_face_center(video_path: Path, start: float, end: float,
                        samples: int = 8) -> tuple[float, float] | None:
    """Detect the dominant face across sampled frames of the clip.

    Returns (face_center_x_frac, source_aspect_h_over_w) or None when OpenCV /
    the model / ffmpeg is unavailable or no face is found. Frames are downscaled
    (fractions and the h/w ratio are scale-invariant) so detection is cheap.
    """
    try:
        import cv2  # optional: the `crop` extra (opencv-python-headless)
    except Exception:
        return None
    if not _FACE_MODEL.exists():
        return None

    span = max(end - start, 0.001)
    faces: list[tuple[float, float]] = []  # (center_x_frac, area*confidence weight)
    aspect_hw = 0.0
    with tempfile.TemporaryDirectory(prefix="hc_face_") as tmp:
        pattern = str(Path(tmp) / "f_%03d.png")
        cmd = [
            "ffmpeg", "-v", "error",
            "-ss", str(max(0.0, start)), "-t", str(span),
            "-i", str(video_path),
            "-vf", f"fps={samples}/{span},scale=640:-1",
            "-frames:v", str(samples), "-y", pattern,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, check=True)
        except Exception:
            return None

        detector = None
        for fp in sorted(Path(tmp).glob("f_*.png")):
            img = cv2.imread(str(fp))
            if img is None:
                continue
            h, w = img.shape[:2]
            aspect_hw = h / w
            if detector is None:
                detector = cv2.FaceDetectorYN.create(
                    str(_FACE_MODEL), "", (w, h), score_threshold=0.7,
                )
            else:
                detector.setInputSize((w, h))
            _, dets = detector.detect(img)
            if dets is None:
                continue
            for d in dets:
                fw, fh, conf = float(d[2]), float(d[3]), float(d[-1])
                cx = (float(d[0]) + fw / 2) / w
                faces.append((cx, fw * fh * conf))

    if not faces or not aspect_hw:
        return None

    # Cluster face centers by x; pick the cluster with the most total weight — so a
    # side-by-side two-shot resolves to the more prominent person, not their midpoint.
    faces.sort()
    clusters: list[list[tuple[float, float]]] = [[faces[0]]]
    for f in faces[1:]:
        if f[0] - clusters[-1][-1][0] > 0.12:
            clusters.append([f])
        else:
            clusters[-1].append(f)
    best = max(clusters, key=lambda c: sum(wt for _, wt in c))
    face_cx = sum(cx * wt for cx, wt in best) / sum(wt for _, wt in best)
    return face_cx, aspect_hw


def _smart_crop_position(video_path: Path, start: float, end: float,
                         aspect_ratio: str) -> float:
    """Crop center [0,1] that frames a detected face for a portrait/square target.

    Fixes the off-center-speaker misframe (a centered crop of a speaker sitting
    left/right of frame catches empty background beside them). Returns 0.5
    (center) for landscape targets, when OpenCV is unavailable, or when no face
    is found — so wide/no-face shots are unchanged.
    """
    tw, th = _target_resolution(aspect_ratio)
    if tw >= th:
        return 0.5
    found = _detect_face_center(video_path, start, end)
    if found is None:
        return 0.5
    face_cx, aspect_hw = found
    crop_w_frac = (tw / th) * aspect_hw  # crop width as a fraction of source width
    return _crop_position_for_face(face_cx, crop_w_frac)


# ---------------------------------------------------------------------------
# SRT generation from word timings
# ---------------------------------------------------------------------------

def generate_srt(transcript: dict, start_time: float, end_time: float, output_path: Path) -> Path:
    """Generate an SRT subtitle file from transcript segments within a time range.

    Timestamps are offset so the clip starts at 0:00.
    """
    word_entries = _subtitle_entries_from_words(transcript, start_time, end_time)
    if word_entries:
        lines = []
        for idx, entry in enumerate(word_entries, 1):
            lines.append(str(idx))
            lines.append(f"{_srt_time(entry['start'])} --> {_srt_time(entry['end'])}")
            lines.append(entry["text"])
            lines.append("")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines), encoding="utf-8")
        return output_path

    lines = []
    idx = 1

    for seg in transcript.get("segments", []):
        seg_start = seg["start"]
        seg_end = seg["end"]

        # Skip segments outside the clip range
        if seg_end <= start_time or seg_start >= end_time:
            continue

        # Clamp to clip boundaries
        s = max(seg_start, start_time) - start_time
        e = min(seg_end, end_time) - start_time

        text = seg["text"].strip()
        if not text:
            continue

        # Break long segments into shorter chunks (max ~10 words per subtitle)
        words = text.split()
        chunk_size = 10
        seg_duration = e - s
        word_count = len(words)

        if word_count <= chunk_size:
            lines.append(str(idx))
            lines.append(f"{_srt_time(s)} --> {_srt_time(e)}")
            lines.append(text)
            lines.append("")
            idx += 1
        else:
            # Split into chunks with proportional timing
            for i in range(0, word_count, chunk_size):
                chunk_words = words[i:i + chunk_size]
                chunk_start = s + (i / word_count) * seg_duration
                chunk_end = s + (min(i + chunk_size, word_count) / word_count) * seg_duration
                lines.append(str(idx))
                lines.append(f"{_srt_time(chunk_start)} --> {_srt_time(chunk_end)}")
                lines.append(" ".join(chunk_words))
                lines.append("")
                idx += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def _subtitle_entries_from_words(
    transcript: dict,
    start_time: float,
    end_time: float,
) -> list[dict]:
    """Build subtitle entries from word-level timestamps when available."""
    words = []
    for segment in transcript.get("segments", []):
        for word in segment.get("words", []):
            word_start = word.get("start")
            word_end = word.get("end")
            text = (word.get("text") or "").strip()
            if word_start is None or word_end is None or not text:
                continue
            if word_end <= start_time or word_start >= end_time:
                continue
            words.append({
                "start": max(word_start, start_time) - start_time,
                "end": min(word_end, end_time) - start_time,
                "text": text,
            })

    if not words:
        return []

    entries = []
    chunk = []
    punctuation = (".", "!", "?", ",", ";", ":", "...")
    max_words = 7
    max_span = 3.0

    def flush_chunk() -> None:
        if not chunk:
            return
        entries.append({
            "start": chunk[0]["start"],
            "end": max(chunk[-1]["end"], chunk[0]["start"] + 0.25),
            "text": _join_word_tokens(item["text"] for item in chunk),
        })
        chunk.clear()

    for word in words:
        if not chunk:
            chunk.append(word)
            continue

        prospective_span = word["end"] - chunk[0]["start"]
        chunk.append(word)
        if (
            len(chunk) >= max_words
            or prospective_span >= max_span
            or word["text"].endswith(punctuation)
        ):
            flush_chunk()

    flush_chunk()
    return entries


def _join_word_tokens(tokens) -> str:
    text = ""
    for token in tokens:
        if not text:
            text = token
        elif token[:1] in ",.!?;:":
            text += token
        else:
            text += " " + token
    return text.strip()


def _srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# SRT parsing (used by the Pillow burn path)
# ---------------------------------------------------------------------------

def _parse_srt(srt_path: Path) -> list[dict]:
    """Parse an SRT file into a list of {start, end, text} dicts (times in seconds)."""
    content = srt_path.read_text(encoding="utf-8").strip()
    if not content:
        return []

    entries = []
    blocks = content.split("\n\n")
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        # Line 1: index, Line 2: timestamps, Line 3+: text
        time_line = lines[1]
        text = " ".join(lines[2:]).strip()
        if " --> " not in time_line or not text:
            continue

        start_str, end_str = time_line.split(" --> ")
        entries.append({
            "start": _parse_srt_time(start_str.strip()),
            "end": _parse_srt_time(end_str.strip()),
            "text": text,
        })
    return entries


def _parse_srt_time(ts: str) -> float:
    """Parse HH:MM:SS,mmm to seconds."""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


# ---------------------------------------------------------------------------
# Pillow per-frame subtitle burn (libass-free fallback)
# ---------------------------------------------------------------------------

def _burn_subtitles_pillow(video_path: Path, srt_path: Path, output_path: Path,
                           width: int, height: int, speed: float = 1.0,
                           font: str | None = None) -> Path:
    """Burn subtitles onto a video using Pillow to render text + ffmpeg overlay.

    Creates a transparent subtitle video track from the SRT, then composites it
    onto the input video. Works without libass/libfreetype in ffmpeg.

    Hebrew is reordered for display with python-bidi when available.
    """
    from PIL import Image, ImageDraw, ImageFont

    entries = _parse_srt(srt_path)
    if not entries:
        shutil.copy2(str(video_path), str(output_path))
        return output_path

    # Adjust subtitle timing for speed-up (video is already faster, so
    # subtitle timestamps need to be compressed by the same factor)
    if speed and speed != 1.0:
        for entry in entries:
            entry["start"] /= speed
            entry["end"] /= speed

    # Font setup -- prefer an explicit override, then Hebrew-capable candidates.
    font_size = max(22, height // 30)
    pil_font = None
    font_path = _resolve_pillow_font_path(font)
    if font_path:
        try:
            pil_font = ImageFont.truetype(font_path, font_size)
        except Exception:
            pil_font = None
    if pil_font is None:
        pil_font = ImageFont.load_default()

    outline_width = max(2, font_size // 11)
    margin_bottom = height // 10
    max_text_width = int(width * 0.85)

    def _wrap_text(text: str, draw: "ImageDraw.ImageDraw") -> list[str]:
        """Word-wrap text to fit within max_text_width (logical order)."""
        words = text.split()
        lines = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), test, font=pil_font)
            if bbox[2] - bbox[0] > max_text_width and current:
                lines.append(current)
                current = word
            else:
                current = test
        if current:
            lines.append(current)
        return lines or [text]

    # Pre-compute the empty transparent frame (reused for all non-subtitle frames)
    _empty_frame = Image.new("RGBA", (width, height), (0, 0, 0, 0)).tobytes("raw", "RGBA")

    def _render_frame(text: str | None) -> bytes:
        """Render a single transparent frame with optional subtitle text."""
        if not text:
            return _empty_frame
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Pillow (with libraqm) already shapes RTL/bidi correctly, so we draw the
        # logical-order text as-is. Applying python-bidi here would double-reverse it.
        wrapped = _wrap_text(text, draw)
        line_height = font_size + 4
        block_height = len(wrapped) * line_height

        y = height - margin_bottom - block_height
        for line in wrapped:
            bbox = draw.textbbox((0, 0), line, font=pil_font)
            tw = bbox[2] - bbox[0]
            x = (width - tw) // 2

            # Draw outline
            for dx in range(-outline_width, outline_width + 1):
                for dy in range(-outline_width, outline_width + 1):
                    if dx != 0 or dy != 0:
                        draw.text((x + dx, y + dy), line, font=pil_font, fill=(0, 0, 0, 255))
            # Draw text
            draw.text((x, y), line, font=pil_font, fill=(255, 255, 255, 255))
            y += line_height

        return img.tobytes("raw", "RGBA")

    # Probe + frame piping live in the shared helpers (same pattern as
    # _burn_captions_pillow) - this path only supplies the per-frame image.
    def make_frame(t: float) -> bytes:
        active_text = None
        for entry in entries:
            if entry["start"] <= t < entry["end"]:
                active_text = entry["text"]
                break
        return _render_frame(active_text)

    return _overlay_pillow_frames(video_path, output_path, width, height, make_frame)


# ---------------------------------------------------------------------------
# Social captions: heavy Hebrew font + word-by-word highlight (Pillow)
# ---------------------------------------------------------------------------

_BUNDLED_FONT = Path(__file__).parent / "data" / "fonts" / "Rubik.ttf"

# Platform safe areas. TikTok and Reels are the SAME video format (1080x1920 9:16) —
# what differs is how much of the frame their UI covers. Measured against a real
# render: captions at 0.90 width come within 54px of the edge, which slides under
# TikTok's right-hand like/comment/share rail (~120px). Bottom clearance (22%)
# already clears both platforms' bottom chrome, so only the width changes.
# (caption_width_frac, caption_bottom_frac, logo_edge_frac)
# The logo fraction is vertical and generous on purpose: at the old 4% (43px on a
# 1080-wide frame) the wordmark rendered UNDER the iPhone status-bar clock on Reels.
# It has to clear the status bar (~110px) and, per platform, the app's own header
# (Reels' back-arrow/title row is the tallest).
_SAFE_AREAS = {
    "none":   (0.90, 0.22, 0.09),   # clears the iPhone status bar
    "tiktok": (0.76, 0.22, 0.13),   # + TikTok's top area; captions clear the right rail
    "reels":  (0.85, 0.24, 0.15),   # + the "Reels" header row
}
_ACCENT = (255, 214, 10, 255)     # active word — punchy yellow
_WHITE = (255, 255, 255, 255)
_OUTLINE = (0, 0, 0, 255)


def _is_ltr_word(w: dict) -> bool:
    """A word is LTR if it has Latin letters/digits and no Hebrew — e.g. an
    English brand embedded in a Hebrew line ("OpenAI", "Thoma")."""
    text = w.get("text", "")
    has_hebrew = any("֐" <= c <= "׿" for c in text)
    has_latin = any(c.isascii() and c.isalnum() for c in text)
    return has_latin and not has_hebrew


def _bidi_word_order(words: list[dict]) -> list[dict]:
    """Reorder one caption line (logical order) into visual left-to-right for a
    base-RTL line: reverse the word sequence, but keep runs of consecutive LTR
    words (a multi-word English brand like "Thoma Bravo") in their own order so
    they don't come out reversed. Hebrew-only lines are simply reversed, as
    before. Internal glyph shaping of each word is still Pillow/libraqm's job.
    """
    # Base direction is RTL only when the line actually contains RTL letters.
    # An all-Latin line can still hold neutral-only tokens ("&", "-", "|"),
    # which are not _is_ltr_word, so they would form their own run and flip the
    # line: "Under the Guides & Onboarding" -> "Onboarding & Under the Guides".
    if not any("\u0590" <= c <= "\u05ff" or "\u0600" <= c <= "\u06ff"
               for w in words for c in (w.get("text") or "")):
        return list(words)

    runs: list[list] = []  # [is_ltr, [words]]
    for w in words:
        ltr = _is_ltr_word(w)
        if runs and runs[-1][0] == ltr:
            runs[-1][1].append(w)
        else:
            runs.append([ltr, [w]])
    out: list[dict] = []
    for ltr, run in reversed(runs):
        out.extend(run if ltr else list(reversed(run)))
    return out


def _load_caption_font(size: int, font: str | None = None):
    """Load a heavy Hebrew-capable caption font at `size`. Prefers an explicit
    override, then the bundled Rubik (pushed to its Black weight), then system
    Hebrew fonts, then Pillow's default."""
    from PIL import ImageFont

    candidates: list[str] = []
    # SOFIT_FONT: set-once brand font (same pattern as SOFIT_LOGO) - an
    # explicit `font` argument still wins.
    font = font or os.environ.get("SOFIT_FONT")
    if font and os.path.exists(font):
        candidates.append(font)
    if _BUNDLED_FONT.exists():
        candidates.append(str(_BUNDLED_FONT))
    candidates.extend(_HEBREW_FONT_CANDIDATES)
    for path in candidates:
        try:
            f = ImageFont.truetype(path, size)
        except Exception:
            continue
        try:  # Rubik is a variable font — Black is the default social weight
            f.set_variation_by_name(
                os.environ.get("SOFIT_CAPTION_WEIGHT", "Black"))
        except Exception:
            pass
        return f
    return ImageFont.load_default()


def _merge_continuations(words: list[dict]) -> list[dict]:
    """Glue back tokens the transcriber split mid-word.

    faster-whisper emits "ה-AI" as ["ה", "-AI"], "CTO-CPO" as ["CTO", "-CPO"]
    and "20.9" as ["20", ".9"]. Rendered as separate words each piece is laid
    out on its own, so the hyphen ends up on the wrong side of a Latin token
    and a one-letter Hebrew prefix can wrap to the previous line entirely -
    "ה-AI והכלי-AI" came out as "AI- והכלי AI-" (2026-09-03). A token that
    starts with a hyphen or a decimal point is a continuation, never a word.

    Same for a thousands comma and a geresh (2026-09-10): "80,000" arrives as
    ["80", ",000"] and renders "80 ,000", and "דיליג'נס" / "נינג'ה" / "פיצ'ר"
    arrive split at the apostrophe and render with a space before it.
    """
    out: list[dict] = []
    for w in words:
        txt = w["text"]
        cont = len(txt) > 1 and (
            txt[0] == "-"
            or (txt[0] in ".," and txt[1].isdigit())
            or txt[0] in "'׳’"
        )
        # a bare one-letter Hebrew prefix immediately followed by a hyphen token
        if out and cont:
            out[-1] = {"text": out[-1]["text"] + txt,
                       "start": out[-1]["start"], "end": w["end"]}
        else:
            out.append(dict(w))
    return out


def _caption_entries(transcript: dict, start_time: float, end_time: float) -> list[dict]:
    """Group the clip's words into short caption chunks, KEEPING per-word timings
    (clip-relative) so the burn-in can highlight the word being spoken.

    Returns [{start, end, words: [{text, start, end}, ...]}, ...].
    """
    words: list[dict] = []
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            ws, we = w.get("start"), w.get("end")
            txt = (w.get("text") or "").strip()
            if ws is None or we is None or not txt:
                continue
            if we <= start_time or ws >= end_time:
                continue
            words.append({
                "text": txt,
                "start": max(ws, start_time) - start_time,
                "end": min(we, end_time) - start_time,
            })
    if not words:
        return []
    words = _merge_continuations(words)

    entries: list[dict] = []
    chunk: list[dict] = []
    punct = (".", "!", "?", ",", ";", ":", "…")
    # Defaults are tuned for speech: short chunks read better in short-form.
    # A spec written by hand (static labels over non-speech footage) needs a
    # caption to stay up for its whole span, which these thresholds would
    # otherwise split into one chunk per word.
    max_words = int(os.environ.get("SOFIT_MAX_WORDS", "6"))
    max_span = float(os.environ.get("SOFIT_MAX_SPAN", "2.6"))

    def flush() -> None:
        if not chunk:
            return
        entries.append({
            "start": chunk[0]["start"],
            "end": max(chunk[-1]["end"], chunk[0]["start"] + 0.25),
            "words": [dict(c) for c in chunk],
        })
        chunk.clear()

    for w in words:
        chunk.append(w)
        span = w["end"] - chunk[0]["start"]
        if len(chunk) >= max_words or span >= max_span or w["text"].endswith(punct):
            flush()
    flush()
    return entries


def _probe_fps_frames(video_path: Path) -> tuple[float, int]:
    """Return (fps, total_frames) for a video via ffprobe."""
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, timeout=10,
    )
    fps_str = probe.stdout.strip()
    if "/" in fps_str:
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    else:
        fps = float(fps_str) if fps_str else 30.0
    dur = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, timeout=10,
    )
    total = float(dur.stdout.strip() or 0.0)
    return fps, int(total * fps)


def _overlay_pillow_frames(video_path: Path, output_path: Path, width: int,
                           height: int, make_frame) -> Path:
    """Composite Pillow-rendered transparent frames onto the video via ffmpeg.
    `make_frame(t)` returns raw RGBA bytes for the overlay at time t (seconds)."""
    fps, total_frames = _probe_fps_frames(video_path)
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}",
        "-r", str(fps), "-i", "pipe:0",
        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart", "-shortest", "-y", str(output_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    # `total_frames` comes from the CONTAINER duration, which an audio stream
    # can extend past the video. ffmpeg then finishes on `-shortest` and closes
    # the pipe while we are still writing, so a short write here is a normal
    # finish, not an error. stdin is detached afterwards because communicate()
    # would otherwise re-flush the closed pipe and mask ffmpeg's own stderr.
    try:
        for frame_idx in range(total_frames):
            proc.stdin.write(make_frame(frame_idx / fps))
        proc.stdin.close()
    except (BrokenPipeError, ValueError):
        pass
    proc.stdin = None
    _, stderr = proc.communicate(timeout=FFMPEG_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"Caption overlay failed: {stderr.decode()[-500:]}")
    return output_path


def _fit_hook_card(hook: str, height: int, max_w: int, font: str | None = None):
    """Wrap the hook card, shrinking the font until it fits 2 lines.

    Returns (pil_font, space_width, lines, size) where lines is a list of lists of
    {"text": tok}.

    Real hooks are whole sentences (7-11 words). At the headline size those wrap to
    3-4 lines and blanket the speaker's face — seen on the first WS203 batch. Two
    lines keeps the frame readable; a very long hook bottoms out at the floor size
    and is allowed to take a third line rather than shrinking into illegibility.
    """
    from PIL import Image, ImageDraw

    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    # NEVER truncate. A word cap used to live here and it silently cut the payoff
    # off long hooks mid-clause on WS204 ("...ואף אחד לא" — losing "היה שם לב"),
    # which destroys the hook. Shrinking is the only lever; the floor bounds it.
    toks = hook.split()
    size = max(34, height // 16)
    floor = max(22, height // 28)
    # The constraint that actually matters is how much of the frame the card
    # covers, so budget its HEIGHT rather than its line count. Line-count targets
    # were a bad proxy: capping at 2 lines over-shrank long hooks, and raising the
    # floor to compensate pushed a 15-word hook to 4 lines across the speaker's
    # eyes. A height budget lets a short hook keep the full headline size on 2
    # lines while a long one settles at 3 smaller ones. 0.16 measured against the
    # real WS203/WS204 hooks: short hooks land on 2 big lines, 15-word ones on 3
    # readable lines, and nothing is squeezed to the floor.
    budget = int(height * 0.16)
    while True:
        f = _load_caption_font(size, font)
        space_w = measure.textlength(" ", font=f)
        lines: list[list[dict]] = []
        cur: list[dict] = []
        cur_w = 0.0
        for tok in toks:
            ww = measure.textlength(tok, font=f)
            add = ww + (space_w if cur else 0)
            if cur and cur_w + add > max_w:
                lines.append(cur)
                cur, cur_w = [{"text": tok}], ww
            else:
                cur.append({"text": tok})
                cur_w += add
        if cur:
            lines.append(cur)
        if len(lines) * int(size * 1.3) <= budget or size <= floor:
            return f, space_w, lines, size
        size -= 2


def _burn_captions_pillow(video_path: Path, entries: list[dict], output_path: Path,
                          width: int, height: int, font: str | None = None,
                          speed: float = 1.0, hook: str | None = None,
                          hook_dur: float = 1.8, hook_style: str = "flash",
                          speaker_tags: list[dict] | None = None,
                          safe_area: str = "none",
                          hook_top_min: int = 0,
                          accent: tuple[int, int, int, int] | None = None,
                          cta: str | None = None, clip_dur: float | None = None,
                          cta_dur: float = 2.5,
                          face_band: tuple | None = None,
                          brand_pops: list[dict] | None = None) -> Path:
    """Burn word-highlighted Hebrew captions: heavy font, raised into the lower
    third, thick outline, the word being spoken popped in an accent colour.

    Words are laid out right-to-left (each word drawn in its own box, so Hebrew
    reads in the correct order and mixed digits/Latin stay upright), which also
    makes per-word colouring trivial.

    If `hook` is given, its text is drawn LARGE in the upper third for the first
    `hook_dur` seconds — a caption-first hook card, so a muted scroller (~85% of
    short-form viewers) is stopped by the frame, not a spoken line.

    If `cta` is given (and `clip_dur` known), it's drawn small in the same upper
    zone for the final `cta_dur` seconds — a follow/where-to-listen line that
    rides over the still-playing audio instead of a dead end card.
    """
    from PIL import Image, ImageDraw

    if not entries and not hook and not cta:
        shutil.copy2(str(video_path), str(output_path))
        return output_path

    if speed and speed != 1.0:
        for e in entries:
            e["start"] /= speed
            e["end"] /= speed
            for w in e["words"]:
                w["start"] /= speed
                w["end"] /= speed

    # Caption weight. The defaults are a punchy social look; a calmer deck
    # (screencast, HOWTO) can dial them down without a code change.
    size_div = int(os.environ.get("SOFIT_CAPTION_DIV")
                   or _brand_caption_div() or "22")
    outline_px = os.environ.get("SOFIT_CAPTION_OUTLINE")
    font_size = max(28, height // size_div)
    pil_font = _load_caption_font(font_size, font)
    outline = int(outline_px) if outline_px else max(3, font_size // 8)
    line_h = int(font_size * 1.28)
    w_frac, b_frac, _ = _SAFE_AREAS.get(safe_area, _SAFE_AREAS["none"])
    bottom_margin = int(height * b_frac)  # sit in the lower third, clear of the edge
    max_w = int(width * w_frac)

    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    space_w = measure.textlength(" ", font=pil_font)

    def word_w(txt: str) -> float:
        return measure.textlength(txt, font=pil_font)

    def wrap(words: list[dict]) -> list[list[dict]]:
        lines: list[list[dict]] = []
        cur: list[dict] = []
        cur_w = 0.0
        for w in words:
            ww = word_w(w["text"])
            add = ww + (space_w if cur else 0)
            if cur and cur_w + add > max_w:
                lines.append(cur)
                cur, cur_w = [w], ww
            else:
                cur.append(w)
                cur_w += add
        if cur:
            lines.append(cur)
        return lines

    # Opening hook card: big bold text up top for the first hook_dur seconds.
    # Precompute the wrapped lines once (the text is static).
    hook_lines: list[list[dict]] = []
    hook_font = None
    hook_line_h = hook_outline = 0
    space_w_hook = 0.0
    # The hook card must clear a top-corner logo. Raising the logo out of the
    # iPhone status bar pushed it INTO this band, so the card starts below it.
    top_margin = max(int(height * 0.16), hook_top_min)
    persistent_card = None
    card_pos = (0, 0)
    if hook and hook.strip() and hook_style == "persistent":
        # Variant B (A/B vs the flash card): a smaller boxed quote that stays
        # up the WHOLE clip, so a viewer landing mid-clip still has context.
        # White rounded card, dark text; pre-rendered ONCE and pasted per
        # frame (cheap). Faces sit at different heights and sizes per crop,
        # so no fixed position is safe — the card goes into the larger
        # face-free gap (face_band from the crop's face tracker): between the
        # chin and the karaoke block, or between the logo band and the
        # forehead. The font shrinks stepwise if the gap is tight.
        caption_top = height - bottom_margin - 3 * line_h - line_h // 2
        if face_band:
            m = int(height * 0.015)
            gaps = [(int(face_band[1] * height) + m, caption_top),
                    (top_margin, int(face_band[0] * height) - m)]
            gaps = [g for g in gaps if g[1] > g[0]]
            gap = max(gaps, key=lambda g: g[1] - g[0]) if gaps \
                else (top_margin, caption_top)
        else:
            gap = (top_margin, caption_top)

        for div in (34, 38, 42, 46):
            p_size = max(20, int(height / div))
            p_font = _load_caption_font(p_size, font)
            p_space = measure.textlength(" ", font=p_font)
            p_max = int(width * 0.84)
            p_lines: list[list[dict]] = []
            cur: list[dict] = []
            cur_w = 0.0
            for tok in [{"text": w} for w in hook.split()]:
                twd = measure.textlength(tok["text"], font=p_font)
                add = twd + (p_space if cur else 0)
                if cur and cur_w + add > p_max:
                    p_lines.append(cur)
                    cur, cur_w = [tok], twd
                else:
                    cur.append(tok)
                    cur_w += add
            if cur:
                p_lines.append(cur)
            p_line_h = int(p_size * 1.35)
            pad = int(p_size * 0.75)
            widths = [sum(measure.textlength(w["text"], font=p_font) for w in ln)
                      + p_space * (len(ln) - 1) for ln in p_lines]
            card_w = int(max(widths)) + 2 * pad
            card_h = len(p_lines) * p_line_h + 2 * pad - (p_line_h - p_size) // 2
            if card_h <= gap[1] - gap[0]:
                break
        persistent_card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        pcd = ImageDraw.Draw(persistent_card)
        pcd.rounded_rectangle([0, 0, card_w - 1, card_h - 1],
                              radius=int(p_size * 0.55),
                              fill=(255, 255, 255, 247))
        py = pad
        for ln, lw in zip(p_lines, widths):
            order = _bidi_word_order(ln)
            px = (card_w - lw) / 2
            for w in order:
                pcd.text((px, py), w["text"], font=p_font,
                         fill=(22, 22, 22, 255))
                px += measure.textlength(w["text"], font=p_font) + p_space
            py += p_line_h
        # Below-face gap: hug the captions (bottom-aligned). Above-face gap:
        # hug the logo band (top-aligned). Never float mid-gap over the set.
        card_y = gap[1] - card_h if gap[1] == caption_top else gap[0]
        card_pos = ((width - card_w) // 2, max(0, card_y))
    elif hook and hook.strip():
        hook_font, space_w_hook, hook_lines, hook_size = _fit_hook_card(
            hook, height, max_w, font)
        hook_outline = max(3, hook_size // 7)
        hook_line_h = int(hook_size * 1.3)

    def hook_w(txt: str) -> float:
        return measure.textlength(txt, font=hook_font)

    # Speaker lower-thirds: name + title on a small rounded card, shown for a
    # few seconds the first time that person appears. Pre-rendered once each.
    # Card sits on the right (RTL show) at ~58% height: below the hook zone,
    # above the karaoke band, off the centered face.
    tag_cards: list[tuple[Image.Image, tuple[int, int], float, float]] = []
    for tag in (speaker_tags or []):
        name = (tag.get("name") or "").strip()
        if not name:
            continue
        title_txt = (tag.get("title") or "").strip()
        n_size = max(28, int(height / 28))
        t_size = max(22, int(height / 40))
        n_font = _load_caption_font(n_size, font)
        t_font = _load_caption_font(t_size, font)

        def _line_w(txt, f):
            toks = [{"text": w} for w in txt.split()]
            sp = measure.textlength(" ", font=f)
            return (toks, sp,
                    sum(measure.textlength(w["text"], font=f) for w in toks)
                    + sp * (len(toks) - 1))
        n_toks, n_sp, n_w = _line_w(name, n_font)
        t_toks, t_sp, t_w = _line_w(title_txt, t_font) if title_txt else ([], 0.0, 0.0)
        pad = int(n_size * 0.6)
        gap = int(n_size * 0.25) if title_txt else 0
        card_w = int(max(n_w, t_w)) + 2 * pad
        card_h = int(n_size * 1.2) + (int(t_size * 1.2) + gap if title_txt else 0) + 2 * pad
        card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        cd.rounded_rectangle([0, 0, card_w - 1, card_h - 1],
                             radius=int(n_size * 0.45),
                             fill=(255, 255, 255, 247))
        # accent bar on the right edge (RTL reading side)
        bar = max(6, n_size // 5)
        cd.rounded_rectangle([card_w - 1 - bar, 0, card_w - 1, card_h - 1],
                             radius=bar // 2, fill=accent or _ACCENT)
        yy = pad
        for toks, sp, lw, f, lh, fill in (
                (n_toks, n_sp, n_w, n_font, n_size, (22, 22, 22, 255)),
                (t_toks, t_sp, t_w, t_font, t_size, (95, 95, 95, 255))):
            if not toks:
                continue
            xx = card_w - pad - bar - lw  # right-aligned inside the card
            for w in _bidi_word_order(toks):
                cd.text((xx, yy), w["text"], font=f, fill=fill)
                xx += measure.textlength(w["text"], font=f) + sp
            yy += int(lh * 1.2) + gap
        at = float(tag.get("at", 0.0)) / (speed or 1.0)
        dur = float(tag.get("dur", 3.0)) / (speed or 1.0)
        # Anchor above the worst-case (3-line) karaoke block so the caption
        # never runs over the card.
        card_y = height - bottom_margin - 3 * line_h - card_h - line_h // 2
        pos = (width - card_w - int(width * 0.045), card_y)
        tag_cards.append((card, pos, at, at + dur))

    # Brand pops: a small white card with a brand/place image that flashes for
    # ~a second when the brand is SAID (word-timed). Face-aware: sits in the
    # larger face-free band, same logic as the persistent hook card.
    pop_cards: list[tuple[Image.Image, tuple[int, int], float, float]] = []
    for pop in (brand_pops or []):
        img_path = pop.get("image")
        if not img_path or not os.path.exists(img_path):
            continue
        logo_img = Image.open(img_path).convert("RGBA")
        box = int(width * 0.24)
        scale = min(box / logo_img.width, box / logo_img.height)
        logo_img = logo_img.resize((max(1, int(logo_img.width * scale)),
                                    max(1, int(logo_img.height * scale))))
        pad = int(width * 0.035)
        cw_, ch_ = logo_img.width + 2 * pad, logo_img.height + 2 * pad
        card = Image.new("RGBA", (cw_, ch_), (0, 0, 0, 0))
        cd_ = ImageDraw.Draw(card)
        cd_.rounded_rectangle([0, 0, cw_ - 1, ch_ - 1], radius=int(pad * 0.8),
                              fill=(255, 255, 255, 244))
        card.paste(logo_img, (pad, pad), logo_img)
        # News-insert placement: top-RIGHT corner (the show logo owns the
        # top-left) - faces in a centered 9:16 crop rarely reach it, unlike
        # any mid-frame band on multi-speaker cuts.
        px_ = width - cw_ - int(width * 0.045)
        py_ = max(top_margin, int(height * 0.09))
        at = float(pop.get("at", 0.0)) / (speed or 1.0)
        dur = float(pop.get("dur", 1.2)) / (speed or 1.0)
        pop_cards.append((card, (px_, py_), at, at + dur))

    # Closing CTA line: small, upper zone (empty once the hook card is gone),
    # shown over the final cta_dur seconds while the audio still plays.
    cta_lines: list[list[dict]] = []
    cta_font = None
    cta_from = None
    cta_line_h = cta_outline = 0
    space_w_cta = 0.0
    if cta and cta.strip() and clip_dur:
        cta_size = max(24, height // 30)
        cta_font = _load_caption_font(cta_size, font)
        cta_outline = max(2, cta_size // 8)
        cta_line_h = int(cta_size * 1.3)
        space_w_cta = measure.textlength(" ", font=cta_font)
        toks = [{"text": t} for t in cta.split()]
        cur: list[dict] = []
        cur_w = 0.0
        for tok in toks:
            twd = measure.textlength(tok["text"], font=cta_font)
            add = twd + (space_w_cta if cur else 0)
            if cur and cur_w + add > max_w:
                cta_lines.append(cur)
                cur, cur_w = [tok], twd
            else:
                cur.append(tok)
                cur_w += add
        if cur:
            cta_lines.append(cur)
        out_dur = clip_dur / speed if speed and speed != 1.0 else clip_dur
        cta_from = max(0.0, out_dur - cta_dur)

    def cta_w(txt: str) -> float:
        return measure.textlength(txt, font=cta_font)

    # Static captions over non-speech footage have no spoken word to track,
    # so the karaoke highlight is just noise: SOFIT_CAPTION_ACCENT=none drops it.
    if os.environ.get("SOFIT_CAPTION_ACCENT", "").lower() == "none":
        active_color = _WHITE
    else:
        active_color = accent or _ACCENT
    empty = Image.new("RGBA", (width, height), (0, 0, 0, 0)).tobytes("raw", "RGBA")

    def make_frame(t: float) -> bytes:
        img = None
        d = None

        if persistent_card is not None:
            img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            img.paste(persistent_card, card_pos, persistent_card)

        for pc, pc_pos, pc_from, pc_to in pop_cards:
            if pc_from <= t < pc_to:
                if img is None:
                    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                    d = ImageDraw.Draw(img)
                img.paste(pc, pc_pos, pc)
        for tc, tc_pos, tc_from, tc_to in tag_cards:
            if tc_from <= t < tc_to:
                if img is None:
                    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                    d = ImageDraw.Draw(img)
                img.paste(tc, tc_pos, tc)

        if hook_lines and t < hook_dur:
            if img is None:
                img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
            y = top_margin
            for line in hook_lines:
                order = _bidi_word_order(line)  # visual left-to-right (base RTL)
                lw = sum(hook_w(w["text"]) for w in order) + space_w_hook * (len(order) - 1)
                x = (width - lw) / 2
                for w in order:
                    d.text((x, y), w["text"], font=hook_font, fill=_WHITE,
                           stroke_width=hook_outline, stroke_fill=_OUTLINE)
                    x += hook_w(w["text"]) + space_w_hook
                y += hook_line_h

        if cta_lines and cta_from is not None and t >= cta_from:
            if img is None:
                img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
            y = top_margin
            for line in cta_lines:
                order = _bidi_word_order(line)  # visual left-to-right (base RTL)
                lw = sum(cta_w(w["text"]) for w in order) + space_w_cta * (len(order) - 1)
                x = (width - lw) / 2
                for w in order:
                    d.text((x, y), w["text"], font=cta_font, fill=_WHITE,
                           stroke_width=cta_outline, stroke_fill=_OUTLINE)
                    x += cta_w(w["text"]) + space_w_cta
                y += cta_line_h

        active = None
        for e in entries:
            if e["start"] <= t < e["end"]:
                active = e
                break
        if active is not None:
            if img is None:
                img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
            lines = wrap(active["words"])
            y = height - bottom_margin - len(lines) * line_h
            for line in lines:
                order = _bidi_word_order(line)  # visual left-to-right (base RTL)
                lw = sum(word_w(w["text"]) for w in order) + space_w * (len(order) - 1)
                x = (width - lw) / 2  # left edge of the centered line
                for w in order:
                    color = active_color if (w["start"] <= t < w["end"]) else _WHITE
                    d.text((x, y), w["text"], font=pil_font, fill=color,
                           stroke_width=outline, stroke_fill=_OUTLINE)
                    x += word_w(w["text"]) + space_w
                y += line_h

        if img is None:
            return empty
        return img.tobytes("raw", "RGBA")

    return _overlay_pillow_frames(video_path, output_path, width, height, make_frame)


# ---------------------------------------------------------------------------
# Speaker-tracking crop (dynamic pan following the on-screen face)
# ---------------------------------------------------------------------------


# --- Brand kit (machine-local, never committed: fonts are licensed and the
# repo is public). Assets auto-load from ~/.sofit/assets when present:
#   font.ttf  -> caption/hook/tag font        (override: SOFIT_FONT)
#   frame.mov -> alpha frame over every clip  (override: SOFIT_FRAME)
#   badge.png -> show badge under the karaoke (override: SOFIT_BADGE)
# ~/.sofit/brand.json {"caption_div": N} sets caption size (H//N px;
# SOFIT_CAPTION_DIV env still wins).
_BRAND_DIR = Path.home() / ".sofit" / "assets"


def _brand_asset(env: str, name: str) -> str | None:
    if os.environ.get("SOFIT_BRAND") == "off":
        return None
    p = os.environ.get(env)
    if p and os.path.exists(p):
        return p
    q = _BRAND_DIR / name
    return str(q) if q.exists() else None


def _brand_caption_div() -> str | None:
    if os.environ.get("SOFIT_BRAND") == "off":
        return None
    try:
        cfg = json.loads((Path.home() / ".sofit" / "brand.json").read_text())
        v = cfg.get("caption_div")
        return str(int(v)) if v else None
    except (OSError, ValueError):
        return None


def _apply_brand_overlays(output_path: Path, width: int, height: int) -> None:
    """Post-pass on a finished clip: show badge under the karaoke block +
    alpha frame video over everything. No-op when no brand assets exist."""
    frame = _brand_asset("SOFIT_FRAME", "frame.mov")
    badge = _brand_asset("SOFIT_BADGE", "badge.png")
    if not frame and not badge:
        return
    inputs = ["-i", str(output_path)]
    filters = []
    last = "0:v"
    idx = 1
    if badge:
        inputs += ["-i", badge]
        bw = int(width * 0.593)          # pill ~= caption-line width
        by = int(height * 0.797)         # just under the karaoke block
        filters.append(f"[{idx}:v]scale={bw}:-1[b]")
        filters.append(f"[{last}][b]overlay=(W-w)/2:{by}[v{idx}]")
        last = f"v{idx}"
        idx += 1
    if frame:
        inputs += ["-i", frame]
        filters.append(f"[{idx}:v]scale={width}:{height},format=yuva420p[f]")
        filters.append(f"[{last}][f]overlay=0:0:eof_action=pass[v{idx}]")
        last = f"v{idx}"
    tmp = output_path.with_suffix(".brand.mp4")
    cmd = ["ffmpeg", "-v", "error", *inputs,
           "-filter_complex", ";".join(filters),
           "-map", f"[{last}]", "-map", "0:a?",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
           "-crf", "20", "-c:a", "copy", "-movflags", "+faststart",
           "-y", str(tmp)]
    try:
        _run_ffmpeg(cmd)
        tmp.replace(output_path)
    except Exception:  # noqa: BLE001 - a failed brand pass keeps the clean clip
        tmp.unlink(missing_ok=True)
        raise


def _face_track(video_path: Path, start: float, end: float,
                step: float = 0.25) -> tuple[list, float, tuple | None]:
    """Sample the dominant face x-fraction over the clip. Returns (samples,
    aspect_hw, face_band) where samples is [(t_rel_sec, cx_frac_or_None), ...],
    face_band is the (top_frac, bottom_frac) vertical band the face occupies
    across the clip (10th/90th percentile, so a lone detector glitch doesn't
    stretch it), or ([], 0, None) if OpenCV / model / ffmpeg is unavailable or
    no face is ever found. Vertical fractions survive the 9:16 crop unchanged
    (the crop only trims width), so overlays can use face_band to stay off
    the face.

    Sampled ~4x/sec so a real ~0.6s shot registers as multiple samples (and gets
    followed) while a single-frame detector glitch stays a lone sample (ignored).
    """
    try:
        import cv2
    except Exception:
        return [], 0.0, None, None
    if not _FACE_MODEL.exists():
        return [], 0.0, None, None

    span = max(end - start, 0.001)
    n = max(3, min(int(span / step) + 1, 500))
    samples: list[tuple[float, float | None]] = []
    tops: list[float] = []
    bots: list[float] = []
    pair_mids: list[float] = []
    pair_seps: list[float] = []
    two_face = 0
    total_det = 0
    aspect_hw = 0.0
    with tempfile.TemporaryDirectory(prefix="hc_track_") as tmp:
        pattern = str(Path(tmp) / "f_%04d.png")
        cmd = [
            "ffmpeg", "-v", "error",
            "-ss", str(max(0.0, start)), "-t", str(span),
            "-i", str(video_path),
            "-vf", f"fps={n}/{span},scale=480:-1",
            "-frames:v", str(n), "-y", pattern,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180, check=True)
        except Exception:
            return [], 0.0, None, None
        detector = None
        files = sorted(Path(tmp).glob("f_*.png"))
        for i, fp in enumerate(files):
            img = cv2.imread(str(fp))
            if img is None:
                continue
            h, w = img.shape[:2]
            aspect_hw = h / w
            if detector is None:
                detector = cv2.FaceDetectorYN.create(
                    str(_FACE_MODEL), "", (w, h), score_threshold=0.7,
                )
            else:
                detector.setInputSize((w, h))
            _, dets = detector.detect(img)
            t_rel = span * (i + 0.5) / n
            if dets is None or len(dets) == 0:
                samples.append((t_rel, None))
                continue
            best = max(dets, key=lambda d: float(d[2]) * float(d[3]) * float(d[-1]))
            cx = (float(best[0]) + float(best[2]) / 2) / w
            samples.append((t_rel, cx))
            tops.append(float(best[1]) / h)
            bots.append((float(best[1]) + float(best[3])) / h)
            total_det += 1
            if len(dets) >= 2:
                # second-largest face: a real second person, not a glitch,
                # when it is at least a third of the main face's size.
                rest = sorted(dets, key=lambda d: float(d[2]) * float(d[3]),
                              reverse=True)[1]
                if float(rest[2]) * float(rest[3]) >= \
                        0.33 * float(best[2]) * float(best[3]):
                    cx2 = (float(rest[0]) + float(rest[2]) / 2) / w
                    two_face += 1
                    pair_mids.append((cx + cx2) / 2)
                    pair_seps.append(abs(cx - cx2))

    if not aspect_hw or all(cx is None for _, cx in samples):
        return [], 0.0, None, None
    ts, bs = sorted(tops), sorted(bots)
    band = (ts[int(len(ts) * 0.1)], bs[min(len(bs) - 1, int(len(bs) * 0.9))])
    pair = None
    if total_det and two_face / total_det >= 0.5:
        import statistics as _st
        pair = {"frac": round(two_face / total_det, 2),
                "mid": _st.median(pair_mids), "sep": _st.median(pair_seps)}
    return samples, aspect_hw, band, pair



def _split_crop_vf(aspect_ratio: str, cx_left: float, cx_right: float) -> str:
    """Two-person stacked split for shots where both faces can't fit one
    vertical crop: each panel is a full-height crop centered on one face,
    scaled to W x H/2, stacked leftmost-on-top."""
    tw, th = _target_resolution(aspect_ratio)
    panel_ar = tw / (th / 2)                     # e.g. 1080/960 = 9:8
    parts = []
    for i, cx in enumerate(sorted((cx_left, cx_right))):
        parts.append(
            f"[s{i}]crop='min(iw,ih*{panel_ar:.6f})':ih:"
            f"'max(0,min(iw-min(iw,ih*{panel_ar:.6f}),"
            f"iw*{cx:.4f}-min(iw,ih*{panel_ar:.6f})/2))':0,"
            f"scale={tw}:{th // 2}[p{i}]")
    return (f"split=2[s0][s1];{parts[0]};{parts[1]};"
            f"[p0][p1]vstack=inputs=2")


def _dominant_position(values: list, prev: float, tol: float) -> float:
    """Dominant crop position among `values`: cluster by `tol`, pick the biggest
    cluster (ties broken toward the one nearest `prev` for continuity)."""
    import statistics
    s = sorted(values)
    groups: list[list[float]] = [[s[0]]]
    for v in s[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    best = max(groups, key=lambda g: (len(g), -abs(statistics.median(g) - prev)))
    return statistics.median(best)


def _pan_keyframes(samples: list, cut_delta: float = 0.12,
                   min_run: int = 2) -> list:
    """Turn raw face samples into (t, cx) keyframes for a stable pan.

    The crop should FOLLOW a genuine camera cut to another speaker, but must not
    chase isolated single-sample blips (detector noise, or a sub-second flash) —
    that was the jumpiness. So we group consecutive samples into runs at the same
    position and only COMMIT a move to a new position when its run lasts at least
    `min_run` samples (a real, sustained shot). Shorter runs are transient and
    inherit the last committed position (the crop holds). Committed positions are
    piecewise-constant with a near-instant snap between them.
    """
    import statistics

    ts = [t for t, _ in samples]
    xs: list = [cx for _, cx in samples]
    # forward then backward fill the None (no-face) gaps by holding
    last = None
    for i in range(len(xs)):
        if xs[i] is None:
            xs[i] = last
        else:
            last = xs[i]
    nxt = None
    for i in range(len(xs) - 1, -1, -1):
        if xs[i] is None:
            xs[i] = nxt
        else:
            nxt = xs[i]
    if any(v is None for v in xs):
        return []

    # Group consecutive samples into runs at (roughly) the same position.
    runs: list[list] = []  # [t_start, [values]]
    for t, x in zip(ts, xs):
        if runs and abs(x - statistics.median(runs[-1][1])) < cut_delta:
            runs[-1][1].append(x)
        else:
            runs.append([t, [x]])
    runs_r = [(t0, statistics.median(v), len(v)) for t0, v in runs]

    # Build committed positions. A run that persists as a clearly SUSTAINED shot
    # (>= stable_len samples, ~1.5s) is followed — the crop moves to frame that
    # speaker. Everything else is a BUSY region (rapid cuts / a multi-person
    # exchange): collapse the whole contiguous busy region to its DOMINANT face
    # position and hold that one value across it. This keeps the crop on a real
    # person (never the empty gap) without bouncing: clip-2's flurry collapses to
    # the main speaker, clip-6's exchange to its dominant participant. Busy regions
    # shorter than min_run samples are ignored as detector noise (hold).
    stable_len = 6
    committed: list[tuple[float, float]] = [(runs_r[0][0], runs_r[0][1])]
    i = 1
    while i < len(runs_r):
        t0, cx, cnt = runs_r[i]
        if cnt >= stable_len:
            if abs(cx - committed[-1][1]) >= cut_delta:
                committed.append((t0, cx))  # sustained shot -> follow it
            i += 1
            continue
        j = i
        bvals: list[float] = []
        while j < len(runs_r) and runs_r[j][2] < stable_len:
            bvals += [runs_r[j][1]] * runs_r[j][2]
            j += 1
        if len(bvals) >= min_run:
            dom = _dominant_position(bvals, committed[-1][1], cut_delta)
            if abs(dom - committed[-1][1]) >= cut_delta:
                committed.append((t0, dom))
        i = j

    # Piecewise-constant keyframes with a near-instant snap at each commit.
    kf: list[tuple[float, float]] = []
    for t, cx in committed:
        if kf:
            kf.append((max(t - 0.001, kf[-1][0] + 0.001), kf[-1][1]))  # hold, then snap
        kf.append((t, cx))
    kf.append((ts[-1], committed[-1][1]))
    return kf


def _piecewise_expr(kfs: list) -> str:
    """ffmpeg expression for cx(t): piecewise-linear through the keyframes."""
    kfs = sorted(kfs)
    expr = f"{kfs[-1][1]:.5f}"
    for (t0, c0), (t1, c1) in reversed(list(zip(kfs, kfs[1:]))):
        dt = max(t1 - t0, 1e-3)
        seg = f"({c0:.5f}+({(c1 - c0):.5f})*(t-{t0:.5f})/{dt:.5f})"
        expr = f"if(lt(t,{t1:.5f}),{seg},{expr})"
    return f"if(lt(t,{kfs[0][0]:.5f}),{kfs[0][1]:.5f},{expr})"


def _dynamic_crop_vf(aspect_ratio: str, keyframes: list) -> str:
    """Crop-to-fill vf whose x offset pans over time to follow the face track."""
    tw, th = _target_resolution(aspect_ratio)
    r = tw / th
    cw = f"ih*{r:.6f}"
    cx = _piecewise_expr(keyframes)
    # single-quote the expression values so their commas aren't parsed as filter
    # separators; eval=frame recomputes x every frame.
    # crop x/y expressions are evaluated per-frame (they can reference `t`), so the
    # window pans over time. Single-quote the values so their commas aren't parsed
    # as filtergraph separators.
    x = f"clip(({cx})*iw-({cw})/2,0,iw-({cw}))"
    crop = f"crop=w='{cw}':h=ih:x='{x}':y=0"
    return f"{crop},scale={tw}:{th}"


# ---------------------------------------------------------------------------
# Audiogram canvas (audio-only sources: mp3 podcasts with no video track)
# ---------------------------------------------------------------------------

def _is_audio_only(source: Path) -> bool:
    """True when the source has audio but no real video stream. An embedded
    cover image (mp3 attached_pic) does not count as video."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)],
            capture_output=True, text=True, timeout=30)
        streams = json.loads(result.stdout or "{}").get("streams", [])
    except Exception:
        return False
    has_audio = False
    for s in streams:
        ctype = s.get("codec_type")
        if ctype == "audio":
            has_audio = True
        elif ctype == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return False
    return has_audio


def _extract_embedded_art(source: Path, work_dir: str) -> str | None:
    """Pull the attached-pic cover art out of an mp3/m4a, if there is one."""
    out = Path(work_dir) / "_embedded_art.jpg"
    try:
        _run_ffmpeg(["ffmpeg", "-i", str(source), "-map", "0:v",
                     "-frames:v", "1", "-y", str(out)])
    except Exception:
        return None
    return str(out) if out.exists() and out.stat().st_size > 0 else None


def _prep_audiogram_assets(cover: str | None, tw: int, th: int,
                           work_dir: str) -> tuple[str, str | None]:
    """One-time Pillow prep for the audiogram canvas: a blurred/darkened
    background PNG at frame size, plus a rounded square art card (~55% of frame
    width). Blurring the still once here (instead of gblur per frame in ffmpeg)
    keeps the render fast. Returns (bg_path, art_path); no cover -> a dark
    gradient background and no card."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    bg_path = str(Path(work_dir) / "_ag_bg.png")
    if not cover:
        bg = Image.new("RGB", (tw, th))
        d = ImageDraw.Draw(bg)
        for y in range(th):  # subtle vertical near-black gradient
            v = 34 - round(20 * y / th)
            d.line([(0, y), (tw, y)], fill=(v, v, v + 8))
        bg.save(bg_path)
        return bg_path, None

    im = Image.open(cover).convert("RGB")
    # Background: crop-to-fill the frame, blur hard, darken. The blur also
    # hides zoompan's integer-rounding jitter during the slow push-in.
    scale = max(tw / im.width, th / im.height)
    bg = im.resize((round(im.width * scale), round(im.height * scale)))
    left, top = (bg.width - tw) // 2, (bg.height - th) // 2
    bg = bg.crop((left, top, left + tw, top + th))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=max(20, tw // 27)))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    bg.save(bg_path)

    # Art card: square center-crop with rounded corners.
    side = min(im.width, im.height)
    art = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                   (im.width + side) // 2, (im.height + side) // 2))
    card = round(tw * 0.55)
    art = art.resize((card, card))
    mask = Image.new("L", (card, card), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, card - 1, card - 1], radius=card // 18, fill=255)
    art_rgba = Image.new("RGBA", (card, card))
    art_rgba.paste(art, (0, 0), mask)
    art_path = str(Path(work_dir) / "_ag_art.png")
    art_rgba.save(art_path)
    return bg_path, art_path


def _accent_from_art(image_path: str | None) -> tuple[int, int, int, int] | None:
    """Pick the artwork's dominant vibrant hue as the caption accent colour, so
    the active-word highlight matches the podcast's branding. The hue is
    re-lit for readability (full brightness, saturation backed off until the
    colour is light enough over a dark background). Returns None — keep the
    default accent — when there's no art or it's essentially monochrome."""
    if not image_path:
        return None
    import colorsys
    try:
        from PIL import Image
        im = Image.open(image_path).convert("RGB").resize((64, 64))
    except Exception:
        return None
    bins = [0.0] * 12  # 30° hue bins, votes weighted by saturation*brightness
    for r, g, b in im.getdata():
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.25 or v < 0.25:
            continue  # grays / blacks / near-whites don't vote
        bins[int(h * 12) % 12] += s * v
    if sum(bins) < 64:  # <~1.5% of pixels carry colour: monochrome art
        return None
    hue = (bins.index(max(bins)) + 0.5) / 12
    r = g = b = 1.0
    for s in (0.85, 0.65, 0.45):  # back off saturation until light enough
        r, g, b = colorsys.hsv_to_rgb(hue, s, 1.0)
        if 0.2126 * r + 0.7152 * g + 0.0722 * b >= 0.55:
            break
    return (round(r * 255), round(g * 255), round(b * 255), 255)


def _logo_geometry(logo: str, tw: int, th: int, logo_pos: str,
                   safe_area: str) -> tuple[int, str, int]:
    """Logo overlay width, ffmpeg overlay position expression, and the minimum
    top offset a hook card needs to clear a top-corner logo."""
    lw = round(tw * 0.22)
    mx = round(tw * 0.04)
    # Vertical inset is much larger than horizontal: platform chrome (status
    # bar, app header) lives at the top edge, not the sides.
    my = round(th * _SAFE_AREAS.get(safe_area, _SAFE_AREAS["none"])[2])
    hook_top_min = 0
    if logo_pos.startswith("top"):
        try:
            from PIL import Image
            with Image.open(logo) as _li:
                _lh = round(_li.height * lw / _li.width)
        except Exception:
            _lh = round(th * 0.05)          # conservative guess
        hook_top_min = my + _lh + round(th * 0.025)   # logo bottom + a gap
    pos = {
        "top-left": f"{mx}:{my}",
        "top-right": f"W-w-{mx}:{my}",
        "bottom-left": f"{mx}:H-h-{my}",
        "bottom-right": f"W-w-{mx}:H-h-{my}",
    }.get(logo_pos, f"{mx}:{my}")
    return lw, pos, hook_top_min


def _audiogram_cmd(source_video: Path, padded_start: float, duration: float,
                   tw: int, th: int, bg_png: str, art_png: str | None,
                   logo: str | None, logo_pos: str, safe_area: str,
                   speed: float, af: str, temp_clip: Path,
                   card_fade_at: float | None = None,
                   music: str | None = None) -> tuple[list[str], int]:
    """ffmpeg command for an audio-only source: blurred cover background with a
    slow push-in, optional rounded art card, and a subtle waveform strip (the
    captions burned by the later Pillow pass are the hero element — research
    says over-designed waveforms hurt retention). `card_fade_at` delays the art
    card until the opening hook card leaves, so the two never collide.

    `music` mixes a bed (the show's own theme — rights-cleared by definition)
    under the voice: looped, quiet, and sidechain-ducked so speech stays clear.
    Returns (cmd, hook_top_min)."""
    fps = 30
    frames = max(1, round(duration * fps))
    cmd = ["ffmpeg", "-ss", str(padded_start), "-t", str(duration),
           "-i", str(source_video), "-loop", "1", "-i", bg_png]
    # Slow 10% push-in over the clip; the pre-blurred background hides
    # zoompan's integer-rounding jitter.
    fc = [
        "[0:a]asplit=2[a_wv][a_raw]",
        f"[1:v]zoompan=z='1+0.10*on/{frames}':x='(iw-iw/zoom)/2'"
        f":y='(ih-ih/zoom)/2':d=1:s={tw}x{th}:fps={fps}[bg]",
    ]
    last = "bg"
    idx = 2
    if art_png:
        cmd += ["-loop", "1", "-i", art_png]
        art_in = f"{idx}:v"
        if card_fade_at:
            fc.append(f"[{idx}:v]format=rgba,"
                      f"fade=t=in:st={card_fade_at}:d=0.4:alpha=1[fad]")
            art_in = "fad"
        fc.append(f"[{last}][{art_in}]overlay=(W-w)/2:{round(th * 0.27)}"
                  f":format=auto[art]")
        last = "art"
        idx += 1
    wave_h = 2 * round(th * 0.0375)   # even height for yuv420p
    fc.append(f"[a_wv]aformat=channel_layouts=mono,"
              f"showwaves=s={tw}x{wave_h}:mode=cline:rate={fps}"
              f":colors=white@0.75[wv]")
    fc.append(f"[{last}][wv]overlay=0:{round(th * 0.60)}:format=auto[wav]")
    last = "wav"
    hook_top_min = 0
    if logo and os.path.exists(logo):
        lw, pos, hook_top_min = _logo_geometry(logo, tw, th, logo_pos, safe_area)
        cmd += ["-loop", "1", "-i", str(logo)]
        fc.append(f"[{idx}:v]scale={lw}:-1[lg]")
        fc.append(f"[{last}][lg]overlay={pos}:format=auto[lgo]")
        last = "lgo"
        idx += 1
    if speed and speed != 1.0:
        fc.append(f"[{last}]setpts={1.0/speed}*PTS[spd]")
        last = "spd"
    if music and os.path.exists(music):
        # Theme bed: looped input, quiet, ducked by the voice (sidechain), then
        # mixed without amix's default per-input attenuation. Voice splits into
        # the duck key and the mix feed.
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        fc.append(f"[a_raw]{af},asplit=2[a_key][a_mix]")
        # loudnorm pins the bed to a fixed loudness (~10dB under typical
        # speech) whatever the theme file's mastering; the duck then only has
        # to make room during words, not rescue a bad level.
        fc.append(f"[{idx}:a]loudnorm=I=-28:TP=-3:LRA=7[bed0]")
        fc.append("[bed0][a_key]sidechaincompress="
                  "threshold=0.03:ratio=4:attack=20:release=500[bed]")
        fc.append("[a_mix][bed]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        fc.append(f"[a_raw]{af}[aout]")
    cmd += [
        "-filter_complex", ";".join(fc),
        "-map", f"[{last}]", "-map", "[aout]", "-shortest",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "medium",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-y",
        str(temp_clip),
    ]
    return cmd, hook_top_min


# ---------------------------------------------------------------------------
# ffmpeg runner + clip extraction
# ---------------------------------------------------------------------------

def _run_ffmpeg(cmd: list[str]) -> None:
    """Run an ffmpeg command and raise with a useful error on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        error_lines = [
            line for line in stderr.splitlines()
            if not line.startswith((" ", "ffmpeg version", "  ", "lib"))
            and "Copyright" not in line
            and "configuration:" not in line
            and "built with" not in line
        ]
        error_msg = "\n".join(error_lines[-10:]) if error_lines else stderr[-500:]
        raise RuntimeError(f"ffmpeg failed: {error_msg}")


def extract_clip(
    source_video: Path,
    start_time: float,
    end_time: float,
    output_path: Path,
    aspect_ratio: str = "9:16",
    crop_position: float = 0.5,
    subtitles_path: Path | None = None,
    speed: float = 1.0,
    pad_seconds: float = 0.0,
    font: str | None = None,
    crop_vf: str | None = None,
    caption_entries: list | None = None,
    logo: str | None = None,
    logo_pos: str = "top-left",
    hook: str | None = None,
    hook_style: str = "flash",
    safe_area: str = "none",
    audiogram: tuple[str, str | None] | None = None,
    accent: tuple[int, int, int, int] | None = None,
    cta: str | None = None,
    music: str | None = None,
    cutaways: list[dict] | None = None,
    speaker_tags: list[dict] | None = None,
    face_band: tuple | None = None,
    brand_pops: list[dict] | None = None,
    zoom: float = 1.0,
    audio_edge_fade: bool = False,
) -> Path:
    """Extract a clip, crop-to-fill the target aspect, and burn captions.

    `audiogram` is (bg_png, art_png|None) from `_prep_audiogram_assets`: the
    source is audio-only, so the video canvas is generated instead of cropped.

    Crop-to-fill is always used (never letterbox). `crop_vf` overrides the crop
    filtergraph (used for the speaker-tracking dynamic pan); otherwise a static
    crop at `crop_position` is built.

    Captions: if `caption_entries` (word-level) is given, the word-highlight
    Pillow renderer is used (heavy font, active word popped). Else `subtitles_path`
    burns via libass when available, otherwise the plain Pillow fallback.

    pad_seconds defaults to 0: caption timestamps are relative to `start_time`,
    so any pre-roll pad would desync them.
    """
    padded_start = max(0, start_time - pad_seconds)
    padded_end = end_time + pad_seconds
    duration = padded_end - padded_start

    output_path.parent.mkdir(parents=True, exist_ok=True)

    tw, th = _target_resolution(aspect_ratio)
    hook_top_min = 0   # raised below when a top-corner logo would collide
    vf = crop_vf if crop_vf else _build_crop_vf(aspect_ratio, crop_position)
    # Punch-in: a slight zoom on alternating narrative spans makes the cut
    # read as a deliberate camera change. Footage only — captions/brand
    # overlays render after this and keep their size.
    if zoom and zoom != 1.0 and not audiogram:
        vf += (f",scale=ceil(iw*{zoom}/2)*2:ceil(ih*{zoom}/2)*2,"
               f"crop={tw}:{th}")

    # Word-highlight captions (and/or the opening hook card / closing CTA line)
    # render in a second Pillow pass over the cropped clip.
    burn_captions = (bool(caption_entries) or bool(hook and hook.strip())
                     or bool(cta and cta.strip()))

    # Burn subtitles with libass BEFORE speed-up so subtitle timing matches
    # the original video timestamps (both get sped up together by setpts)
    _sub_symlink = None
    burn_with_pillow = False
    if not burn_captions and subtitles_path and subtitles_path.exists():
        if _HAS_SUBTITLES_FILTER and not audiogram:
            _sub_symlink = Path(tempfile.mktemp(suffix=".srt", prefix="hc_sub_"))
            _sub_symlink.symlink_to(subtitles_path.resolve())
            safe_sub_path = str(_sub_symlink).replace("\\", "\\\\").replace(":", "\\:")
            font_name = _libass_font_name(font)
            sub_style = (
                f"FontName={font_name},FontSize=22,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,Outline=2,Bold=1"
            )
            vf += f",subtitles={safe_sub_path}:force_style='{sub_style}'"
        else:
            burn_with_pillow = True

    # Add speed-up filter AFTER subtitles (setpts for video)
    if speed and speed != 1.0:
        vf += f",setpts={1.0/speed}*PTS"
        af = f"atempo={speed},{_AUDIO_LIMITER}"
    else:
        af = _AUDIO_LIMITER
    if audio_edge_fade:
        # 30ms fades kill the click at narrative-cut joins; inaudible as a fade.
        fade_out_at = max(0.0, duration / (speed or 1.0) - 0.035)
        af += f",afade=t=in:d=0.03,afade=t=out:st={fade_out_at:.3f}:d=0.03"

    # Step 1: Extract and scale/crop the clip
    if burn_with_pillow or burn_captions:
        temp_clip = output_path.with_suffix(".tmp.mp4")
    else:
        temp_clip = output_path

    if audiogram:
        # Audio-only source: generate the canvas (blurred cover + art card +
        # waveform + logo) instead of cropping a video track. Audio filtering
        # rides inside the filter_complex, so af is fully consumed here.
        # ponytail: music bed is audiogram-only; wire it into the video path
        # when a video show asks for it.
        cmd, hook_top_min = _audiogram_cmd(
            source_video, padded_start, duration, tw, th,
            audiogram[0], audiogram[1], logo, logo_pos, safe_area,
            speed, af, temp_clip,
            card_fade_at=(1.8 if hook and hook.strip() else None),
            music=music)
    else:
        cmd = ["ffmpeg", "-ss", str(padded_start), "-i", str(source_video)]
        # Cutaways: short generated scenes spliced over the footage while the
        # audio keeps running - each is a Ken-Burnsed still shifted to its
        # window. Times are relative to this span's start.
        # ponytail: assumes speed==1.0 (cutaway windows aren't setpts-scaled);
        # scale the times if a sped-up show ever uses cutaways.
        cw = [c for c in (cutaways or []) if os.path.exists(c["image"])]
        if (logo and os.path.exists(logo)) or cw:
            fc_parts = [f"[0:v]{vf}[base]"]
            last, idx = "base", 1
            for k, c in enumerate(cw):
                t0, t1 = float(c["start"]), float(c["end"])
                d = t1 - t0
                vid = c.get("video")
                if vid and os.path.exists(vid):
                    # Animated cutaway shot: cover-crop, freeze-hold if the
                    # shot is shorter than the window, shift into place.
                    cmd += ["-i", str(vid)]
                    fc_parts.append(
                        f"[{idx}:v]scale={tw}:{th}:force_original_aspect_ratio"
                        f"=increase,crop={tw}:{th},fps=30,"
                        f"tpad=stop_mode=clone:stop_duration=15,"
                        f"trim=duration={d},setpts=PTS-STARTPTS+{t0}/TB[cw{k}]")
                else:
                    frames = max(1, round(d * 30))
                    cmd += ["-framerate", "30", "-loop", "1", "-t", str(d),
                            "-i", str(c["image"])]
                    z = (f"1+0.08*on/{frames}" if k % 2 == 0
                         else f"1.08-0.08*on/{frames}")
                    fc_parts.append(
                        f"[{idx}:v]zoompan=z='{z}':x='(iw-iw/zoom)/2'"
                        f":y='(ih-ih/zoom)/2':d=1:s={tw}x{th}:fps=30,"
                        f"trim=duration={d},setpts=PTS-STARTPTS+{t0}/TB[cw{k}]")
                fc_parts.append(
                    f"[{last}][cw{k}]overlay=0:0:enable="
                    f"'between(t,{t0},{t1})':eof_action=pass[ov{k}]")
                last, idx = f"ov{k}", idx + 1
            if logo and os.path.exists(logo):
                # Overlay a fixed logo AFTER the crop (so it doesn't pan with
                # the face track) and above cutaways, before captions. Sized to
                # a fraction of frame width; PNG aspect preserved.
                lw, pos, hook_top_min = _logo_geometry(logo, tw, th, logo_pos,
                                                       safe_area)
                cmd += ["-loop", "1", "-i", str(logo)]
                fc_parts.append(f"[{idx}:v]scale={lw}:-1[lg]")
                fc_parts.append(f"[{last}][lg]overlay={pos}:format=auto[vout]")
                last = "vout"
            cmd += ["-t", str(duration),
                    "-filter_complex", ";".join(fc_parts),
                    "-map", f"[{last}]", "-map", "0:a?"]
        else:
            cmd += ["-t", str(duration), "-vf", vf]
        if af:
            cmd += ["-af", af]
        cmd += [
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "medium",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-y",
            str(temp_clip),
        ]

    try:
        _run_ffmpeg(cmd)
    finally:
        if _sub_symlink and _sub_symlink.exists():
            _sub_symlink.unlink()

    # Step 2: Burn captions in a Pillow pass if needed
    if burn_captions:
        try:
            _burn_captions_pillow(temp_clip, caption_entries or [], output_path, tw, th,
                                  font=font, speed=speed, hook=hook,
                                  hook_style=hook_style, speaker_tags=speaker_tags,
                                  safe_area=safe_area, hook_top_min=hook_top_min,
                                  accent=accent, cta=cta, clip_dur=duration,
                                  face_band=face_band, brand_pops=brand_pops)
        finally:
            if temp_clip.exists():
                temp_clip.unlink()
    elif burn_with_pillow:
        try:
            _burn_subtitles_pillow(temp_clip, subtitles_path, output_path, tw, th,
                                   speed=speed, font=font)
        finally:
            if temp_clip.exists():
                temp_clip.unlink()

    return output_path


# ---------------------------------------------------------------------------
# Transcript synthesis + public entry point
# ---------------------------------------------------------------------------

def _clip_transcript(clip: dict) -> dict:
    """Synthesize the transcript shape the caption functions expect from a clip.

    sofit clips carry word timings relative to the clip start:
        {"t": <sec from clip start>, "d": <dur sec>, "w": <word text>}
    The caption code expects one segment with ABSOLUTE word start/end/text, so
    we offset each word by the clip's absolute start.
    """
    clip_start = float(clip["start"])
    words = []
    for w in clip.get("words", []):
        t = float(w["t"])
        d = float(w.get("d", 0.0))
        words.append({
            "start": clip_start + t,
            "end": clip_start + t + d,
            "text": w.get("w", ""),
        })
    seg_end = words[-1]["end"] if words else float(clip["end"])
    return {
        "segments": [
            {"start": clip_start, "end": seg_end, "words": words}
        ]
    }


def _prep_logo(logo_path: str, work_dir: str) -> str | None:
    """Trim a logo PNG to its opaque bounding box (the source often has big
    transparent margins) so it sits tight in the corner. Returns the trimmed
    path, or None if it can't be read."""
    try:
        from PIL import Image
    except Exception:
        return logo_path  # no Pillow: use as-is, ffmpeg still overlays it
    try:
        im = Image.open(logo_path).convert("RGBA")
    except Exception:
        return None
    bbox = im.getchannel("A").getbbox()  # bounds of non-transparent pixels
    if bbox:
        im = im.crop(bbox)
    out = Path(work_dir) / "_logo_trimmed.png"
    im.save(out)
    return str(out)


def _concat_parts(parts: list[Path], output_path: Path) -> None:
    """Losslessly join rendered segment files (identical codec settings) into
    `output_path` with the concat demuxer. Stream copy: no second encode."""
    lst = output_path.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    try:
        _run_ffmpeg(["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(lst),
                     "-c", "copy", "-movflags", "+faststart", "-y", str(output_path)])
    finally:
        lst.unlink(missing_ok=True)
        for p in parts:
            p.unlink(missing_ok=True)


def render_clips(video_path: str, clips: list[dict], out_dir: str,
                 aspect: str = "9:16", subtitles: bool = True,
                 speed: float = 1.0, font: str | None = None,
                 logo: str | None = None, logo_pos: str = "top-left",
                 hook_card: bool = True, hook_variant: int = 0,
                 hook_style: str = "flash",
                 safe_area: str = "none", cover: str | None = None,
                 cta: str | None = None, music: str | None = None) -> list[str]:
    """Render each clip to out_dir/<id>.mp4, cropped-to-fill `aspect` with
    burned Hebrew captions from the clip's word timings. If `logo` is given, it's
    trimmed once and overlaid in the `logo_pos` corner of every clip. When
    `hook_card` is set, each clip's `hook` is burned large in the upper third for
    the opening ~1.8s (a caption-first hook for muted viewers).

    `hook_variant` picks which hook line the card uses: 0 = the primary `hook`
    (default), N>0 = `hook_variants[N-1]` from the clip spec, written to
    `<id>.hookN.mp4` so A/B renders of the same clip don't overwrite each other.

    Audio-only sources (mp3 podcasts) render in audiogram mode automatically:
    a blurred cover-art canvas with a slow push-in, rounded art card, and a
    subtle waveform strip — the karaoke captions remain the hero. Cover art:
    `cover` arg, else SOFIT_COVER env, else the mp3's embedded art, else a
    plain dark gradient.
    Returns the list of output paths."""
    source = Path(video_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Env-var defaults so per-show branding can be set once and applied to
    # every render without flags. Explicit args always win.
    logo = logo or os.environ.get("SOFIT_LOGO") or None
    cta = cta or os.environ.get("SOFIT_CTA") or None
    music = music or os.environ.get("SOFIT_MUSIC") or None
    logo_dir = None
    logo_ready = None
    if logo:
        logo_dir = tempfile.mkdtemp(prefix="hc_logo_")
        logo_ready = _prep_logo(logo, logo_dir)

    tw, th = _target_resolution(aspect)

    # Audio-only source -> audiogram mode: build the canvas assets once.
    audiogram_assets = None
    ag_dir = None
    if _is_audio_only(source):
        ag_dir = tempfile.mkdtemp(prefix="hc_ag_")
        cover = (cover or os.environ.get("SOFIT_COVER")
                 or _extract_embedded_art(source, ag_dir))
        audiogram_assets = _prep_audiogram_assets(cover, tw, th, ag_dir)
        print(f"audio-only source: rendering audiograms "
              f"({'cover art' if cover else 'no cover art — gradient'})",
              file=sys.stderr)

    # Brand-match the active-word caption colour to the show's artwork: the
    # cover in audiogram mode, else the logo. Monochrome art keeps the default.
    accent = _accent_from_art(cover if audiogram_assets else logo)

    outputs: list[str] = []
    for clip in clips:
        clip_id = str(clip.get("id") or f"clip-{len(outputs) + 1}")

        # Hook line for the card: primary, or an alternate for A/B testing. An
        # out-of-range variant falls back to the primary rather than rendering
        # a silently card-less clip.
        hook_text = clip.get("hook")
        suffix = ""
        if hook_variant > 0:
            alts = clip.get("hook_variants") or []
            if hook_variant <= len(alts):
                hook_text = alts[hook_variant - 1]
                suffix = f".hook{hook_variant}"
            else:
                print(f"warning: {clip_id} has no hook variant {hook_variant}; "
                      "using the primary hook", file=sys.stderr)
        # .pers marks the persistent-card style so A/B renders of the same
        # clip coexist and publish-log can attribute the style.
        if hook_style == "persistent" and hook_card:
            suffix += ".pers"
        output_path = out / f"{clip_id}{suffix}.mp4"

        # A narrative edit carries `segments` (kept spans, gaps cut). A plain
        # clip is one span. Each span renders through the same single-range
        # pipeline below; multi-span outputs are then losslessly concatenated.
        ranges = clip.get("segments") or [
            {"start": clip["start"], "end": clip["end"], "words": clip.get("words")}
        ]
        parts: list[Path] = []
        for ri, rng in enumerate(ranges):
            start = float(rng["start"])
            end = float(rng["end"])
            part_path = (output_path if len(ranges) == 1
                         else out / f"{clip_id}{suffix}.part{ri}.tmp.mp4")

            # Word-level caption entries (kept for the highlight burn-in).
            caption_entries = None
            if subtitles and rng.get("words"):
                pseudo = {"start": start, "end": end, "words": rng["words"]}
                caption_entries = _caption_entries(_clip_transcript(pseudo), start, end)

            # Framing: explicit `focus` [0,1] wins. Otherwise track the on-screen
            # face over this span — pan the crop to follow it (fixes speakers going
            # out of frame when the camera cuts between people); if the face barely
            # moves, settle on a single face-centered crop; no face -> center.
            # Per-span on purpose: the shot can change across a narrative cut.
            focus = clip.get("focus")
            crop_vf = None
            crop_position = 0.5
            face_band = None
            if audiogram_assets:
                pass  # generated canvas: nothing to crop or track
            elif isinstance(focus, (int, float)):
                crop_position = float(focus)
            elif tw < th:
                samples, aspect_hw, face_band, pair = _face_track(source, start, end)
                kf = _pan_keyframes(samples) if samples else []
                crop_w_frac = (tw / th) * aspect_hw if aspect_hw else 0.0
                if pair and crop_w_frac:
                    # Two people persistently in shot. If both fit one crop,
                    # center on the PAIR MIDPOINT (nobody clipped at the
                    # edge); if they are too far apart, stacked split-screen.
                    if pair["sep"] <= crop_w_frac * 0.62:
                        crop_position = _crop_position_for_face(
                            pair["mid"], crop_w_frac)
                    else:
                        half = pair["sep"] / 2
                        crop_vf = _split_crop_vf(
                            aspect, pair["mid"] - half, pair["mid"] + half)
                elif kf:
                    spread = max(c for _, c in kf) - min(c for _, c in kf)
                    if spread > 0.08:
                        crop_vf = _dynamic_crop_vf(aspect, kf)        # speaker-tracking pan
                    else:
                        mid = sorted(c for _, c in kf)[len(kf) // 2]  # steady: face-centered
                        crop_position = _crop_position_for_face(mid, crop_w_frac)

            extract_clip(
                source_video=source,
                start_time=start,
                end_time=end,
                output_path=part_path,
                aspect_ratio=aspect,
                crop_position=crop_position,
                crop_vf=crop_vf,
                caption_entries=caption_entries,
                speed=speed,
                pad_seconds=0.0,
                font=font,
                logo=logo_ready,
                logo_pos=logo_pos,
                # Flash card belongs to second zero only — the first span.
                # Persistent style stays up the WHOLE clip, so every span
                # burns it (spans are separate ffmpeg renders).
                hook=(hook_text if hook_card and
                      (ri == 0 or hook_style == "persistent") else None),
                hook_style=hook_style,
                safe_area=safe_area,
                audiogram=audiogram_assets,
                accent=accent,
                # The CTA belongs to the closing seconds — the last span only.
                # ponytail: the music bed restarts at narrative cuts; add
                # per-span -ss offsets if it's ever audible.
                cta=(cta if ri == len(ranges) - 1 else None),
                music=music,
                cutaways=[c for c in (clip.get("cutaways") or [])
                          if int(c.get("span", 0)) == ri],
                # Lower-thirds: {"name","title","at","dur","span"} in the clip
                # spec; "at" is seconds within its span (like cutaways).
                speaker_tags=[s for s in (clip.get("speaker_tags") or [])
                              if int(s.get("span", 0)) == ri],
                # Brand pops: {"term","image","at","dur","span"} - "at" is
                # span-local seconds, like cutaways and speaker tags.
                brand_pops=[b for b in (clip.get("brand_pops") or [])
                            if int(b.get("span", 0)) == ri],
                face_band=face_band,
                # Smoother narrative cuts: alternating punch-in + 30ms audio
                # edge fades at the joins (concat stays lossless).
                zoom=(1.07 if len(ranges) > 1 and ri % 2 else 1.0),
                audio_edge_fade=len(ranges) > 1,
            )
            parts.append(part_path)

        if len(parts) > 1:
            _concat_parts(parts, output_path)
        if not audiogram_assets:
            _apply_brand_overlays(output_path, tw, th)
        outputs.append(str(output_path))

    if logo_dir:
        shutil.rmtree(logo_dir, ignore_errors=True)
    if ag_dir:
        shutil.rmtree(ag_dir, ignore_errors=True)
    return outputs
