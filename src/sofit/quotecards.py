#!/usr/bin/env python3
"""Branded IG carousel quote-cards from episode pull-quotes.

Pipeline: generated automatically by the CLI when quotes are produced
(--quote-cards DIR); also runnable standalone.

Usage: quotecards.py <episode> <out_dir> <quotes.json>
quotes.json: {"title": "<episode headline>", "quotes": ["...", ...]}

Cards are 1080x1350 (4:5 - IG carousel optimum). Slide 0 is a title card;
one quote per slide after. Brand assets come from ~/.sofit/assets (font.ttf,
badge.png) - same kit the video pipeline uses. Buffer data: carousels get
+12% engagement with the EXISTING audience, complementing Reels' +36% reach.
Run inside the sofit venv (needs sofit.render for the RTL word ordering).
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from .render import _load_caption_font, _bidi_word_order
from . import shows

W, H = 1080, 1350
DARK = (14, 12, 18)
ACCENT = (233, 58, 113)
ASSETS = shows.home() / "assets"


def _wrap_rtl(d, text, font, max_w):
    words = text.split()
    sp = d.textlength(" ", font=font)
    lines, cur, w = [], [], 0.0
    for word in words:
        ww = d.textlength(word, font=font)
        if cur and w + sp + ww > max_w:
            lines.append(cur)
            cur, w = [word], ww
        else:
            cur.append(word)
            w += (sp if cur[:-1] else 0) + ww
    if cur:
        lines.append(cur)
    return lines


def _draw_rtl_center(d, line_words, font, y):
    sp = d.textlength(" ", font=font)
    toks = [{"text": w} for w in line_words]
    total = sum(d.textlength(t["text"], font=font) for t in toks) + sp * (len(toks) - 1)
    px = (W - total) / 2
    for t in _bidi_word_order(toks):
        d.text((px, y), t["text"], font=font, fill=(255, 255, 255))
        px += d.textlength(t["text"], font=font) + sp


def _base_card():
    img = Image.new("RGB", (W, H), DARK)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 14], fill=ACCENT)          # top accent bar
    return img, d


def _footer(img):
    badge = ASSETS / "badge.png"
    if badge.exists():
        b = Image.open(badge).convert("RGBA")
        bw = int(W * 0.5)
        bh = int(b.height * bw / b.width)
        img.paste(b.resize((bw, bh)), ((W - bw) // 2, H - bh - 70), b.resize((bw, bh)))


def make_cards(episode: str, out_dir: Path, title: str, quotes: list[str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    outs = []
    # slide 0: title card
    img, d = _base_card()
    small = _load_caption_font(44, None)
    big = _load_caption_font(96, None)
    _draw_rtl_center(d, f"{len(quotes)} ציטוטים מהפרק".split(), small, int(H * 0.28))
    y = int(H * 0.40)
    for line in _wrap_rtl(d, title, big, W * 0.84):
        _draw_rtl_center(d, line, big, y)
        y += 118
    d.rectangle([W * 0.38, y + 30, W * 0.62, y + 40], fill=ACCENT)
    _footer(img)
    p = out_dir / f"{episode}_qcard-0.jpg"
    img.save(p, quality=93)
    outs.append(p)
    # quote slides
    for i, q in enumerate(quotes, 1):
        img, d = _base_card()
        mark = _load_caption_font(220, None)
        d.text((W - 200, 70), '"', font=mark, fill=ACCENT)
        size = 84
        font = _load_caption_font(size, None)
        lines = _wrap_rtl(d, q, font, W * 0.82)
        while len(lines) > 6 and size > 56:
            size -= 8
            font = _load_caption_font(size, None)
            lines = _wrap_rtl(d, q, font, W * 0.82)
        lh = int(size * 1.35)
        y = (H - len(lines) * lh) // 2 - 40
        for line in lines:
            _draw_rtl_center(d, line, font, y)
            y += lh
        idx = _load_caption_font(36, None)
        d.text((70, H - 120), f"{i}/{len(quotes)}", font=idx, fill=(150, 145, 160))
        _footer(img)
        p = out_dir / f"{episode}_qcard-{i}.jpg"
        img.save(p, quality=93)
        outs.append(p)
    return outs


if __name__ == "__main__":
    episode, out_dir, qfile = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    cfg = json.loads(Path(qfile).read_text(encoding="utf-8"))
    for p in make_cards(episode, out_dir, cfg["title"], cfg["quotes"]):
        print(p)
