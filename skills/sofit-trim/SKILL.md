---
name: sofit-trim
description: Cut a segment (a filler moment, a nose-touch, an "um") out of an already-rendered sofit clip via ffmpeg, keeping captions / audio / logo in sync. Use when the user wants to remove or trim a moment out of a finished clip. Part of the sofit tool (see /sofit for the full pipeline).
---

# sofit-trim

Post-render trim on a finished mp4. A clip is one start/end in the spec, so this is NOT a
spec edit — it operates on the rendered file. Captions/audio/logo stay in sync (all baked into frames).

Hesitation words are not this skill. Render already cuts them: אמ / אממ / אהה / אהמ /
הממ / מממ, um / uh / hmm, stretched spellings, and an isolated or low-confidence אה / אם
("if" stays when it is just the word in a sentence). `--defiller-dry-run` lists what
will be cut; `--no-defiller` keeps the ums. Use sofit-trim for a moment the transcript
does not mark as a filler — a gesture, a nose-touch, a false start.

## Find the exact window first
Extract frames around the moment and look, so START/END are precise:
```bash
ffmpeg -v error -ss <T> -i in.mp4 -frames:v 1 f.png    # inspect a few T values to bracket the cut
```

## Cut [START, END] out (concat the before + after)
```bash
ffmpeg -v error -i in.mp4 -filter_complex \
"[0:v]trim=0:START,setpts=PTS-STARTPTS[v1];[0:v]trim=END,setpts=PTS-STARTPTS[v2];\
[0:a]atrim=0:START,asetpts=PTS-STARTPTS[a1];[0:a]atrim=END,asetpts=PTS-STARTPTS[a2];\
[v1][v2]concat=n=2:v=1:a=0[v];[a1][a2]concat=n=2:v=0:a=1[a]" \
-map "[v]" -map "[a]" -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 \
-c:a aac -b:a 128k -movflags +faststart -y out.mp4
```
Replace `START`/`END` with seconds (e.g. `15.9`/`18.0`).

**Caveat:** a re-render (`/sofit-clips` or `--render-from`) cuts filler words again on its own,
but it does not replay this trim — the cut lives only on this mp4, not in the spec. Keep the
pre-cut file and re-apply the trim after any re-render.
