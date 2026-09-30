"""`chapters` CLI entrypoint.

  chapters episode.mp4                         # chapters to stdout
  chapters episode.mp3 --shownotes --quotes    # add show notes + pull-quotes
  chapters ep.mp4 --format youtube --out ep    # writes ep.chapters.md (youtube body)

Multi-output routing: with --out, each generator writes a sibling file
(FILE.chapters.md / FILE.shownotes.md / FILE.quotes.md); without --out, each is
printed to stdout under a labeled header.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import __version__


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sofit", description="Hebrew podcast episode kit.")
    p.add_argument("media", nargs="?",
                   help="an mp3/mp4 file, an RSS feed URL, a YouTube URL, or a direct audio URL "
                   "(optional when --render-from is used)")
    p.add_argument("--episode", type=int, default=1,
                   help="which episode from an RSS feed (1 = first/latest item); ignored for files")
    p.add_argument("--list-episodes", action="store_true",
                   help="list the episodes in an RSS feed and exit")
    p.add_argument(
        "--model",
        default="ivrit-ai/whisper-large-v3-turbo-ct2",
        help="faster-whisper model or HF ct2 repo id (default: Hebrew-tuned ivrit-ai turbo)",
    )
    p.add_argument("--lang", default="he", help="transcript language (default: he)")
    p.add_argument("--max-chapters", type=int, default=12)
    p.add_argument(
        "--format",
        choices=["md", "txt", "youtube", "spotify", "podcast"],
        default="md",
        help="chapter output: md/txt (read), youtube (description paste, >=10s), "
        "spotify (description paste for Spotify/Megaphone, >=30s), "
        "podcast (Podcasting 2.0 chapters JSON for your RSS feed)",
    )
    p.add_argument(
        "--embed-into",
        metavar="AUDIO",
        help="write chapter markers into a copy of this audio file (for Apple "
        "Podcasts etc.); output is <AUDIO stem>.chapters.<ext>",
    )
    p.add_argument(
        "--titler",
        choices=["api", "claude-cli"],
        default="api",
        help="generation backend: api (Anthropic API, needs ANTHROPIC_API_KEY) or "
        "claude-cli (`claude -p`, uses your Claude Code / Pro/Max subscription, no key)",
    )
    p.add_argument("--titler-model", metavar="MODEL",
                   help="model for the generation backend (default: claude-sonnet-5 "
                   "on --titler api; Claude Code's configured model on claude-cli)")
    p.add_argument("--shownotes", action="store_true", help="also generate Hebrew show notes")
    p.add_argument("--quotes", action="store_true", help="also extract pull-quotes")
    p.add_argument("--yt-tags", action="store_true",
                   help="also generate YouTube description hashtags + the tags field "
                        "(tags deliberately carry name variants and likely misspellings)")
    p.add_argument("--quote-cards", metavar="DIR",
                   help="also render the pull-quotes as a branded IG carousel "
                        "(implies --quotes; slide 0 title from --episode-title)")
    p.add_argument("--episode-title", help="headline for the carousel title card")
    p.add_argument("--clips-json", metavar="PATH",
                   help="write a clips.json (clip ranges + hooks + per-word timings) for a social-clip renderer")
    p.add_argument("--render-clips", metavar="DIR",
                   help="render each pull-quote to DIR as a vertical (9:16) clip with burned "
                   "Hebrew captions (needs the [render] extra + ffmpeg)")
    p.add_argument("--render-from", metavar="PATH",
                   help="render clips from an existing (possibly corrected) clips.json, skipping "
                   "transcription; renders into --render-clips DIR or the clips.json's folder")
    p.add_argument("--only", metavar="ID",
                   help="with --render-from, render just one clip by id (e.g. clip-3)")
    p.add_argument("--aspect", default="9:16", help="aspect ratio for --render-clips (default 9:16)")
    p.add_argument("--cover", metavar="PATH",
                   help="cover art image for audiogram clips from audio-only "
                        "sources (default: SOFIT_COVER env, else the mp3's "
                        "embedded art)")
    p.add_argument("--cta", metavar="TEXT",
                   help="closing call-to-action line drawn small in the upper "
                        "zone for the final ~2.5s of each clip (default: "
                        "SOFIT_CTA env)")
    p.add_argument("--music", metavar="PATH",
                   help="theme-music bed mixed quietly under the voice on "
                        "audiogram clips, ducked so speech stays clear "
                        "(default: SOFIT_MUSIC env)")
    p.add_argument("--logo", metavar="PATH",
                   help="overlay a logo (PNG, transparent) on every rendered clip")
    p.add_argument("--logo-pos", default="top-left",
                   choices=["top-left", "top-right", "bottom-left", "bottom-right"],
                   help="logo corner (default top-left)")
    p.add_argument("--no-hook-card", action="store_true",
                   help="don't burn the clip's hook as an opening title card")
    p.add_argument("--hook-style", choices=["flash", "persistent"], default="flash",
                   help="hook card style: flash = big title, first ~1.8s only "
                   "(default); persistent = smaller boxed quote that stays up "
                   "the whole clip (for A/B testing retention)")
    p.add_argument("--safe-area", choices=["none", "tiktok", "reels"], default="none",
                   help="keep captions inside a platform's UI safe zone (TikTok's "
                   "right-hand button rail covers ~120px; same 9:16 video either way)")
    p.add_argument("--hook-variant", type=int, default=0, metavar="N",
                   help="use alternate hook line N from the clip spec for the card "
                   "(0 = the primary hook; N>0 writes <id>.hookN.mp4 so A/B renders coexist)")
    p.add_argument("--storyboard", action="store_true",
                   help="with --render-from: render AI-illustrated storytime clips "
                   "(generated comic-style scenes instead of the recording; needs "
                   "GEMINI_API_KEY + a Claude backend)")
    p.add_argument("--style", metavar="TEXT",
                   help="storyboard art style (default: SOFIT_STYLE env, else a "
                   "comic-book look)")
    p.add_argument("--cutaways", action="store_true",
                   help="with --render-from: splice 1-2 short AI-illustrated "
                   "scenes over the footage at concrete visual moments "
                   "(needs GEMINI_API_KEY + a Claude backend)")
    p.add_argument("--web-cutaways", action="store_true",
                   help="with --render-from: find real footage on Commons and YouTube (youtube extra) "
                   "for useful visual beats; uses the Claude backend to plan and "
                   "inspect bounded keyframes, and caches the selected shots")
    p.add_argument("--web-cutaways-safe-only", action="store_true",
                   help="enable web cutaways, accepting only public-domain, CC0, "
                   "or CC BY metadata with no reported restrictions")
    p.add_argument("--footage-url", action="append", default=[], metavar="URL",
                   help="enable web cutaways using these YouTube or direct HTTPS video links "
                   "instead of search (repeat up to eight times)")
    p.add_argument("--footage-after", metavar="YYYY-MM-DD", default="",
                   help="enable web cutaways and require a known upload date on or after this date")
    p.add_argument("--footage-coverage", type=float, metavar="PERCENT",
                   help="target moving-footage coverage (0-100); default 85 for audio-only, "
                   "0 for sparse cutaways over video. Saved manual plans are preserved")
    p.add_argument("--progress", choices=["text", "json"], nargs="?", const="text",
                   help="with web cutaways: live progress on stderr (JSON is one object per line)")
    p.add_argument("--progress-file", metavar="JSONL", help="with web cutaways: append machine-readable pipeline timings/events")
    p.add_argument("--footage-workers", type=int, default=2, metavar="N",
                   help="bounded discovery/source worker pools (1-8; default 2)")
    p.add_argument("--animate", action="store_true",
                   help="with --storyboard: animate each scene via image-to-video "
                   "(fal.ai Kling, needs FAL_KEY; ~$0.25-0.50 per scene). Failed "
                   "scenes fall back to Ken Burns stills")
    p.add_argument("--char-ref", action="append", metavar="NAME=IMG",
                   help="recurring character for --storyboard: display name + "
                   "reference photo; repeatable. A cached character sheet keeps "
                   "them consistent across scenes")
    p.add_argument("--out", help="base path for sibling output files")
    p.add_argument("--no-cache", action="store_true", help="bypass the transcript cache")
    p.add_argument("--version", action="version", version=f"sofit {__version__}")
    return p


def _emit(kind: str, body: str, out_base: str | None, ext: str = "md") -> None:
    if out_base:
        path = f"{out_base}.{kind}.{ext}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(body + "\n")
        print(f"wrote {path}", file=sys.stderr)
    else:
        print(f"\n# {kind}\n{body}")


def _parse_char_refs(pairs: list[str] | None) -> dict[str, str]:
    """--char-ref "Name=photo.png" pairs -> {name: path}; hard-fails on typos."""
    refs: dict[str, str] = {}
    for pair in pairs or []:
        name, sep, path = pair.partition("=")
        if not sep or not name.strip() or not os.path.exists(path):
            raise SystemExit(f"error: bad --char-ref '{pair}' (want NAME=existing-image)")
        refs[name.strip()] = path
    return refs


def _render_from(clips_path: str, out_dir: str | None, aspect: str, only: str | None,
                 logo: str | None = None, logo_pos: str = "top-left",
                 hook_card: bool = True, hook_variant: int = 0,
                 hook_style: str = "flash",
                 safe_area: str = "none", cover: str | None = None,
                 cta: str | None = None, music: str | None = None,
                 storyboard: bool = False, style: str | None = None,
                 char_refs: dict[str, str] | None = None,
                 titler: str = "api", animate: bool = False,
                 cutaways: bool = False, web_cutaways: bool = False,
                 web_cutaways_safe_only: bool = False,
                 footage_urls: tuple[str, ...] = (), footage_after: str = "",
                 footage_coverage: float | None = None, progress_mode=None, progress_file=None,
                 footage_workers: int = 2) -> int:
    """Render clips from a saved (possibly corrected) clips.json, no transcription.
    Output goes to `out_dir` if given, else the clips.json's own folder."""
    import json
    from pathlib import Path

    try:
        from . import render  # noqa: F401
    except ImportError:
        print("error: --render-from needs the render extra: pip install 'sofit-cli[render]'",
              file=sys.stderr)
        return 1
    try:
        doc = json.loads(Path(clips_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"error: cannot read clips json {clips_path}: {e}", file=sys.stderr)
        return 1

    video = (doc.get("source") or {}).get("video")
    clips = doc.get("clips") or []
    if only:
        clips = [c for c in clips if c.get("id") == only]
        if not clips:
            print(f"error: clip '{only}' not found in {clips_path}", file=sys.stderr)
            return 1
    if not clips:
        print(f"error: no clips in {clips_path}", file=sys.stderr)
        return 1
    if not video or not os.path.exists(video):
        print(f"error: source video not found: {video}", file=sys.stderr)
        return 1

    out = out_dir or os.path.dirname(os.path.abspath(clips_path)) or "."
    if web_cutaways:
        if titler == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
            titler = "claude-cli" if shutil.which("claude") else titler
        from . import storyboard as sb
        try:
            if footage_coverage is None:
                default_coverage = 85 if render._is_audio_only(Path(video)) else 0
                for clip in clips:
                    clip.setdefault("footage_coverage", default_coverage)
            if any(not c.get("visual_context") for c in clips):
                from .transcribe import cached_segments
                from .generate import visual_context
                # Read the existing transcript only; rendering must never transcribe.
                try:
                    context_segments = cached_segments(video)
                except (OSError, ValueError):
                    context_segments = None
                for clip in clips:
                    if not clip.get("visual_context"):
                        context = doc.get("visual_context") or (doc.get("source") or {}).get("visual_context")
                        if not context and context_segments:
                            spans = clip.get("segments") or [clip]
                            context = visual_context(context_segments, min(s["start"] for s in spans),
                                                     max(s["end"] for s in spans))
                        if context:
                            clip["visual_context"] = context
            from .footage_progress import Progress, count, event
            from .footage_pipeline import FootageSession
            from .footage import write_json
            progress = Progress(progress_mode, progress_file)
            import time
            with progress.active():
                with FootageSession(titler=titler, workers=footage_workers,
                        judge_workers=int(os.environ.get("SOFIT_FOOTAGE_JUDGE_WORKERS", "1")),
                        safe_only=web_cutaways_safe_only) as session:
                    event("planning", clips_total=len(clips))
                    completed = time.monotonic()
                    def ready(clip):
                        nonlocal completed
                        render.render_clips(video, [clip], out, aspect=aspect, logo=logo,
                            logo_pos=logo_pos, hook_card=hook_card, hook_variant=hook_variant,
                            hook_style=hook_style, safe_area=safe_area, cover=cover, cta=cta, music=music)
                        count("rendered"); count("clips_done")
                        event("rendered", clip=str(clip.get("id")), clip_seconds=round(time.monotonic()-completed, 3),
                              coverage=clip.get("footage_coverage_report"))
                        completed = time.monotonic()
                        write_json(Path(clips_path), doc)
                    n = sb.add_web_cutaways(doc, clips_path, only=only, style=style,
                        titler=titler, generated=cutaways, animate=animate, safe_only=web_cutaways_safe_only,
                        source_urls=footage_urls, published_after=footage_after, coverage=footage_coverage,
                        session=session, on_clip=ready)
                event("complete")
                metrics = Path(out) / "footage-metrics.json"
                try:
                    write_json(metrics, progress.snapshot())
                except OSError as exc:
                    event("metrics_unavailable", reason=type(exc).__name__)
            if progress_mode != "json":
                print(f"added {n} web/generated cutaway(s); rendered clips as ready", file=sys.stderr)
            return 0
        except (OSError, ValueError) as e:
            print(f"warning: cannot save web cutaways: {e}; continuing render", file=sys.stderr)
        clips = [c for c in doc["clips"] if not only or c.get("id") == only]
    elif cutaways:
        if titler == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
            titler = "claude-cli" if shutil.which("claude") else titler
        if titler == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
            print("error: --cutaways needs ANTHROPIC_API_KEY or the claude CLI",
                  file=sys.stderr)
            return 1
        from . import storyboard as sb
        n = sb.add_cutaways(doc, clips_path, only=only, style=style,
                            titler=titler, animate=animate)
        print(f"added {n} cutaway(s); spec updated", file=sys.stderr)
        clips = [c for c in doc["clips"] if not only or c.get("id") == only]
    if storyboard:
        # Scene planning needs a Claude backend even though --render-from
        # normally doesn't; fall back to the CLI when no key is set.
        if titler == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
            titler = "claude-cli" if shutil.which("claude") else titler
        if titler == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
            print("error: --storyboard needs ANTHROPIC_API_KEY or the claude CLI",
                  file=sys.stderr)
            return 1
        from . import storyboard as sb
        outs = sb.render_storyboard_clips(video, clips, out, aspect=aspect,
                                          logo=logo, logo_pos=logo_pos,
                                          hook_card=hook_card,
                                          hook_variant=hook_variant,
                                          safe_area=safe_area, cta=cta,
                                          style=style, char_refs=char_refs,
                                          titler=titler, animate=animate)
        print(f"rendered {len(outs)} storyboard clip(s) to {out}", file=sys.stderr)
        return 0
    from . import render
    outs = render.render_clips(video, clips, out, aspect=aspect, logo=logo,
                               logo_pos=logo_pos, hook_card=hook_card,
                               hook_variant=hook_variant, hook_style=hook_style,
                               safe_area=safe_area,
                               cover=cover, cta=cta, music=music)
    if web_cutaways:
        from .footage import write_json
        try:
            write_json(Path(clips_path), doc)  # final coverage includes composition fallback
        except OSError as e:
            print(f"warning: cannot save final footage coverage: {e}", file=sys.stderr)
    print(f"rendered {len(outs)} clip(s) to {out}", file=sys.stderr)
    return 0


def _caption_check(argv: list[str]) -> int:
    """sofit caption-check <plan.json> <clips.json> - flag captions that only
    restate the clip. Exits 1 if any post is at or over the echo limit."""
    import json
    import re as _re
    from pathlib import Path
    from .format import ECHO_LIMIT, caption_echo, clip_words

    if len(argv) != 2:
        print("usage: sofit caption-check <plan.json> <clips.json>", file=sys.stderr)
        return 2
    plan = json.loads(Path(argv[0]).read_text())
    spec = json.loads(Path(argv[1]).read_text())
    clips = {c["id"]: c for c in (spec["clips"] if isinstance(spec, dict) else spec)}

    worst = 0.0
    for post in plan.get("posts", []):
        caption = post.get("instagram") or post.get("caption") or ""
        # rendered names carry the A/B tags; the spec is keyed by the bare id
        clip = clips.get(_re.sub(r"\.(hook\d+|pers)", "", post.get("clip", "")))
        if not clip or not caption:
            continue
        echo = caption_echo(caption, clip_words(clip))
        worst = max(worst, echo)
        flag = "ECHO" if echo >= ECHO_LIMIT else "ok"
        print(f"{post['clip']:22s} echo={echo:.0%} {flag}")
    return 1 if worst >= ECHO_LIMIT else 0


def main(argv: list[str] | None = None) -> int:
    # Subcommand fast path: `sofit publish-log ...` records a posted clip
    # (attribution join key for the metrics scraper). Everything else stays
    # on the flag-based parser below.
    raw = sys.argv[1:] if argv is None else argv
    if raw and raw[0] == "publish-log":
        from . import publog
        return publog.main(raw[1:])
    if raw and raw[0] == "caption-check":
        return _caption_check(raw[1:])

    if raw and raw[0] == "cache":
        return _cache_command(raw[1:])
    args = _parser().parse_args(argv)
    if args.web_cutaways_safe_only or args.footage_url or args.footage_after or args.footage_coverage is not None:
        args.web_cutaways = True
    if not 1 <= args.footage_workers <= 8:
        print("error: --footage-workers must be 1..8", file=sys.stderr)
        return 1
    if args.footage_coverage is not None and not 0 <= args.footage_coverage <= 100:
        print("error: --footage-coverage must be between 0 and 100", file=sys.stderr)
        return 1
    if args.footage_url or args.footage_after:
        from .footage import VisualIntent, FootageError
        try:
            VisualIntent("validate", "validate", 3, source_urls=tuple(args.footage_url),
                         published_after=args.footage_after)
        except (ValueError, FootageError) as e:
            print(f"error: invalid footage options: {e}", file=sys.stderr)
            return 1
    if args.web_cutaways and (not args.render_from or args.storyboard):
        print("error: web cutaways use the recording: pass --render-from clips.json "
              "without --storyboard (create the spec with --clips-json first)", file=sys.stderr)
        return 1

    # One env var is the single override channel: every call_claude_json call
    # site (chapters, notes, quotes, clips, storyboard, cutaways) resolves it.
    if args.titler_model:
        os.environ["SOFIT_TITLER_MODEL"] = args.titler_model

    from . import feed

    # Render from an existing clips.json and exit — no key or transcription
    # needed. This is the ONLY render path that honors caption corrections.
    if args.render_from:
        return _render_from(args.render_from, args.render_clips, args.aspect, args.only,
                            logo=args.logo, logo_pos=args.logo_pos,
                            hook_card=not args.no_hook_card,
                            hook_variant=args.hook_variant,
                            hook_style=args.hook_style,
                            safe_area=args.safe_area, cover=args.cover,
                            cta=args.cta, music=args.music,
                            storyboard=args.storyboard, style=args.style,
                            char_refs=_parse_char_refs(args.char_ref),
                            titler=args.titler, animate=args.animate,
                            cutaways=args.cutaways, web_cutaways=args.web_cutaways,
                            web_cutaways_safe_only=args.web_cutaways_safe_only,
                            footage_urls=tuple(args.footage_url), footage_after=args.footage_after,
                            footage_coverage=args.footage_coverage, progress_mode=args.progress,
                            progress_file=args.progress_file, footage_workers=args.footage_workers)

    if args.storyboard:
        print("error: --storyboard renders from a saved spec; run once to get a "
              "clips.json, then: sofit --render-from clips.json --storyboard",
              file=sys.stderr)
        return 1

    if not args.media:
        print("error: media argument is required (or use --render-from PATH)", file=sys.stderr)
        return 1

    # List a feed's episodes and exit — no key or transcription needed.
    if args.list_episodes:
        if not feed.is_url(args.media):
            print("error: --list-episodes needs an RSS feed URL", file=sys.stderr)
            return 1
        try:
            episodes = feed.list_episodes(args.media)
        except (feed.FeedError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not episodes:
            print("error: no episodes with an audio enclosure found", file=sys.stderr)
            return 1
        for i, ep in enumerate(episodes, 1):
            print(f"{i}\t{ep.title}")
        return 0

    # Fail fast on a missing backend BEFORE the expensive transcription step.
    if args.titler == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set (or use --titler claude-cli)", file=sys.stderr)
        return 1
    if args.titler == "claude-cli" and not shutil.which("claude"):
        print("error: claude CLI not found — install Claude Code or use --titler api", file=sys.stderr)
        return 1

    # Resolve an RSS feed / audio URL to a local file (downloads + caches). A
    # local path passes through unchanged.
    try:
        media_path = feed.resolve(args.media, episode=args.episode)
    except (feed.FeedError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    from . import format as fmt
    from . import generate, transcribe

    try:
        segments = transcribe.transcribe(
            media_path, model=args.model, lang=args.lang, use_cache=not args.no_cache
        )
    except FileNotFoundError:
        print(f"error: file not found: {args.media}", file=sys.stderr)
        return 1
    if not segments:
        print("error: no speech detected", file=sys.stderr)
        return 1

    audio_end = segments[-1].end
    # Each generator is independent: if one fails, warn and keep going so the
    # others still produce output (they're separate Claude calls by design).
    failed = 0

    try:
        chapters = generate.make_chapters(segments, max_chapters=args.max_chapters, titler=args.titler)
        ext = "md"
        if args.format in ("youtube", "spotify"):
            min_gap = 30.0 if args.format == "spotify" else 10.0
            body = fmt.render_chapters_youtube(chapters, audio_end, min_gap=min_gap)
            ext = "txt"
            if not body:
                print("warning: fewer than 3 chapters; emitting markdown instead", file=sys.stderr)
                body, ext = fmt.render_chapters_md(chapters), "md"
        elif args.format == "podcast":
            body, ext = fmt.render_chapters_podcast_json(chapters), "json"
        elif args.format == "txt":
            body, ext = fmt.render_chapters_md(chapters), "txt"
        else:
            body = fmt.render_chapters_md(chapters)
        _emit("chapters", body, args.out, ext)

        # Optionally embed the same chapters into an audio file for apps that
        # read in-file chapter markers (Apple Podcasts, etc.).
        if args.embed_into:
            from . import embed
            from pathlib import Path
            src = Path(args.embed_into)
            dst = str(src.with_suffix("")) + ".chapters" + src.suffix
            try:
                embed.embed_chapters(str(src), chapters, audio_end, dst)
                print(f"wrote {dst} (embedded chapters)", file=sys.stderr)
            except (RuntimeError, OSError) as e:
                print(f"warning: embedding failed: {e}", file=sys.stderr)
                failed += 1
    except generate.GenerationError as e:
        print(f"warning: chapters failed: {e}", file=sys.stderr)
        failed += 1

    if args.shownotes:
        try:
            _emit("shownotes", fmt.render_shownotes_md(generate.make_shownotes(segments, titler=args.titler)), args.out)
        except generate.GenerationError as e:
            print(f"warning: show notes failed: {e}", file=sys.stderr)
            failed += 1
    if args.yt_tags:
        try:
            _notes = generate.make_shownotes(segments, titler=args.titler)
            _emit("yttags", fmt.render_youtube_tags_md(
                generate.make_youtube_tags(segments, _notes, titler=args.titler)), args.out)
        except generate.GenerationError as e:
            print(f"warning: youtube tags failed: {e}", file=sys.stderr)
            failed += 1
    if args.quotes or args.quote_cards:
        try:
            _quotes = generate.make_quotes(segments, titler=args.titler)
            _emit("quotes", fmt.render_quotes_md(_quotes), args.out)
            if args.quote_cards:
                from . import quotecards
                from pathlib import Path as _P
                episode = os.path.splitext(os.path.basename(media_path))[0]
                outs = quotecards.make_cards(
                    episode, _P(args.quote_cards),
                    args.episode_title or "ציטוטים מהפרק",
                    [q.text for q in _quotes])
                print(f"rendered {len(outs)} quote cards to {args.quote_cards}",
                      file=sys.stderr)
        except generate.GenerationError as e:
            print(f"warning: quotes failed: {e}", file=sys.stderr)
            failed += 1
    # Social clips: generate the spec ONCE so a saved clips.json exactly matches
    # the rendered mp4s (same ids/ranges). When rendering without an explicit
    # --clips-json, the spec is dropped next to the clips as
    # <render_dir>/<media_stem>.clips.json — so the spec and its clips travel
    # together and it's ready for a later correct_clip / --render-from.
    if args.clips_json or args.render_clips:
        import json
        from pathlib import Path
        clips = None
        try:
            clips = generate.make_clips(segments, titler=args.titler)
        except generate.GenerationError as e:
            print(f"warning: clip generation failed: {e}", file=sys.stderr)
            failed += 1
        if clips is not None:
            doc = {"schema_version": 1,
                   "source": {"video": os.path.abspath(media_path)},
                   "clips": clips}
            json_path = args.clips_json
            if not json_path and args.render_clips:
                stem = os.path.splitext(os.path.basename(media_path))[0]
                json_path = os.path.join(args.render_clips, stem + ".clips.json")
            if json_path:
                p = Path(json_path)
                # Explicit --clips-json always writes; an auto path is written only
                # if absent, so re-running --render-clips never clobbers a spec you
                # may have corrected.
                if args.clips_json or not p.exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"wrote {json_path} ({len(clips)} clips)", file=sys.stderr)
                else:
                    print(f"note: kept existing {json_path} (may contain corrections); "
                          f"rendering freshly-generated clips, which may differ. To render "
                          f"the saved spec instead: chapters --render-from {json_path}",
                          file=sys.stderr)
            if args.render_clips:
                try:
                    from . import render
                    outs = render.render_clips(media_path, clips, args.render_clips,
                                               aspect=args.aspect, logo=args.logo,
                                               logo_pos=args.logo_pos,
                                               hook_card=not args.no_hook_card,
                                               hook_variant=args.hook_variant,
                                               hook_style=args.hook_style,
                                               safe_area=args.safe_area,
                                               cover=args.cover,
                                               cta=args.cta, music=args.music)
                    print(f"rendered {len(outs)} clips to {args.render_clips}", file=sys.stderr)
                except ImportError:
                    print("error: --render-clips needs the render extra: pip install 'sofit-cli[render]'", file=sys.stderr)
                    failed += 1
    return 1 if failed else 0


def _cache_command(argv):
    import json
    from .footage import cache_dir
    from .footage_cache import status, prune
    parser = argparse.ArgumentParser(prog="sofit cache")
    parser.add_argument("action", choices=["status", "prune", "clean"])
    parser.add_argument("--max-size-mb", type=int, default=4096)
    parser.add_argument("--max-age-days", type=float, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--temp-age-hours", type=float, default=24, help="remove temporary files older than this age")
    args = parser.parse_args(argv)
    if args.max_size_mb < 0 or not 0 <= args.max_age_days < float("inf") or not 0 <= args.temp_age_hours < float("inf"):
        parser.error("cache limits must be finite and nonnegative")
    result = status(cache_dir()) if args.action == "status" else prune(cache_dir(),
        max_bytes=args.max_size_mb * 1024**2, max_age=args.max_age_days * 86400,
        clean=args.action == "clean", dry_run=args.dry_run, temporary_age=args.temp_age_hours * 3600)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
