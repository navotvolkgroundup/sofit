"""Find hesitation sounds the transcriber never wrote down.

ivrit-ai faster-whisper drops אממ / אה even when the initial prompt mentions
them, so a word-list pass finds nothing. This pass looks only inside
inter-word gaps (and the silence before the first word or after the last)
and classifies the energy there:

* hesitation — a short, steady, voiced hum. Low pitch variance, flat
  envelope. The only kind eligible to cut, and only the hum itself, never
  the surrounding pause.
* laugh — amplitude that pulses several times a second. Never cut.
* breath — noisy and weakly autocorrelated. Never cut.
* speech — moving pitch or syllable-shaped energy: the other host, or a
  real phrase. Never cut. A hesitation in the same gap is also left in
  unless a stretch of silence separates the two.
* silence — a dramatic pause. Not reported and not cut.

Nothing here calls a model. A gap the transcriber left empty is either
silence or one of the shapes above; another ASR pass on that gap repeats
the same miss.
"""

from __future__ import annotations

import array
import math
import os
import shutil
import struct
import subprocess

# 25ms frames, 10ms hop. Long enough for a 70Hz period at 8kHz, short enough
# that a 120ms hum is several frames.
_WIN_S = 0.025
_HOP_S = 0.010
# Room tone sits under this. A 200Hz hum at amplitude 0.4 has rms ~0.28.
_ABS_RMS = 0.02
_F0_MIN = 70.0
_F0_MAX = 350.0
_VOICED_NACF = 0.50
_MIN_VOICED_RATIO = 0.68
_MIN_MEDIAN_NACF = 0.55
_MAX_F0_CV = 0.085
_MAX_SEMITONES = 3.4
_MAX_RMS_CV = 0.30
_MAX_ZCR = 0.15
_MIN_DUR = 0.12
_MAX_DUR = 1.80
# Holes shorter than this are one event (a laugh's dips, a syllable join).
_BRIDGE_S = 0.09
# A hum may be cut beside speech only when at least this much silence
# separates them. Closer than that, leave the whole gap alone.
_SEPARATION_S = 0.18


def has_scannable_gap(words: list[dict], duration: float, min_gap: float) -> bool:
    """True when some stretch between words is long enough to hide a hum."""
    return any(g1 - g0 >= min_gap - 1e-6 for g0, g1 in iter_gaps(words, duration, min_gap))


def iter_gaps(words: list[dict], duration: float, min_gap: float):
    """Yield (start, end) of inter-word gaps, including the head and the tail."""
    cursor = 0.0
    for word in sorted(words, key=lambda w: float(w["t"])):
        start = float(word["t"])
        if start - cursor >= min_gap - 1e-6:
            yield cursor, start
        cursor = max(cursor, start + float(word["d"]))
    if duration - cursor >= min_gap - 1e-6:
        yield cursor, max(cursor, duration)


def read_pcm(path, start: float, duration: float, sample_rate: int = 8000):
    """Mono float32 samples for [start, start+duration], or None.

    Seek is the fast-then-accurate pair (`-ss` before `-i`, then a short
    `-ss` after) so a hum at 50 minutes is not decoded from t=0 and is not
    a keyframe early. Failure — missing ffmpeg, no audio, a bad file — is
    None. Callers keep going with the word list.
    """
    if duration <= 0 or not path or not os.path.exists(path):
        return None
    if shutil.which("ffmpeg") is None:
        return None
    start = max(0.0, float(start))
    pre = min(1.0, start)
    rough = start - pre
    cmd = [
        "ffmpeg", "-v", "error",
        "-ss", f"{rough:.3f}", "-i", str(path),
        "-ss", f"{pre:.3f}", "-t", f"{float(duration):.3f}",
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "f32le", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    nbytes = len(proc.stdout) - (len(proc.stdout) % 4)
    if nbytes < sample_rate * 0.05 * 4:
        return None
    buf = array.array("f")
    buf.frombytes(proc.stdout[:nbytes])
    # ffmpeg's f32le matches this host. array 'f' is native endian; Linux
    # cloud agents are little-endian. Swap if that ever isn't true.
    if struct.pack("<f", 1.0) != struct.pack("=f", 1.0):
        buf.byteswap()
    return buf


def _frame_table(samples, sr: int) -> list[dict]:
    win = max(8, int(sr * _WIN_S))
    hop = max(1, int(sr * _HOP_S))
    frames = []
    i = 0
    n = len(samples)
    while i + win <= n:
        seg = samples[i:i + win]
        frames.append({
            "t": i / sr,
            "rms": _rms(seg),
            "zcr": _zcr(seg),
            "i": i,
            "win": win,
        })
        i += hop
    return frames


def _energy_threshold(frames: list[dict], words: list[dict]) -> float:
    """Above the noise floor, and not so high that a loud clip hides the hum.

    The floor comes from the quiet end of the *gaps*, so speech itself is
    not treated as noise. It is then capped at a fraction of the word
    level: a clip that is mostly vowels has a high 10th percentile, and
    `noise * 2.5` would otherwise sit above every frame — the scan would
    report nothing on a real episode. A test clip whose words are digital
    silence never takes that cap, and the absolute floor is what a hum in
    the gap has to clear.
    """
    rms = [f["rms"] for f in frames]
    gap_rms = [f["rms"] for f in frames if not _overlaps_word(f["t"], words)]
    noise = _percentile(gap_rms if len(gap_rms) >= 5 else rms, 0.10)
    speech_frames = [f["rms"] for f in frames if _overlaps_word(f["t"], words)]
    speech = _percentile(speech_frames, 0.60) if len(speech_frames) >= 5 else 0.0
    threshold = max(_ABS_RMS, noise * 2.5)
    if speech > _ABS_RMS:
        cap = max(_ABS_RMS, speech * 0.35)
        floor = max(_ABS_RMS, speech * 0.08)
        threshold = min(max(threshold, floor), cap)
    return threshold


def _overlaps_word(t: float, words: list[dict]) -> bool:
    for word in words:
        start = float(word["t"])
        if start - 0.02 <= t <= start + float(word["d"]) + 0.02:
            return True
    return False


def _regions(frames: list[dict], threshold: float, sr: int) -> list[dict]:
    active = [f["rms"] >= threshold for f in frames]
    hop = frames[1]["t"] - frames[0]["t"] if len(frames) > 1 else _HOP_S
    max_hole = max(1, int(round(_BRIDGE_S / hop))) if hop > 0 else 1
    raw = _bridge(active, max_hole)
    regions = []
    for a, b in raw:
        sl = frames[a:b + 1]
        dur = (sl[-1]["t"] + _WIN_S) - sl[0]["t"]
        if dur < 0.08:
            continue
        regions.append({
            "start": sl[0]["t"],
            "end": sl[-1]["t"] + _WIN_S,
            "frames": sl,
            "sr": sr,
        })
    return regions


def _bridge(active: list[bool], max_hole: int) -> list[tuple[int, int]]:
    spans = []
    i = 0
    n = len(active)
    while i < n:
        if not active[i]:
            i += 1
            continue
        last = i
        j = i + 1
        while j < n:
            if active[j]:
                last = j
                j += 1
                continue
            k = j
            while k < n and not active[k] and k - last <= max_hole:
                k += 1
            if k < n and active[k] and k - last <= max_hole:
                j = k
                continue
            break
        spans.append((i, last))
        i = last + 1
    return spans


def _label(region: dict) -> None:
    frames = region["frames"]
    sr = region["sr"]
    rms = [f["rms"] for f in frames]
    zcr = [f["zcr"] for f in frames]
    f0s: list[float] = []
    nacfs: list[float] = []
    # Pitch only where there is energy. A laugh's silent dips are unvoiced
    # on purpose — they are what makes the envelope bursty.
    peak = max(rms) if rms else 0.0
    floor = max(_ABS_RMS, peak * 0.25)
    for frame in frames:
        if frame["rms"] < floor:
            continue
        # The caller owns the sample buffer; recover it from the closure
        # stored on the frame by _label_with_samples. Tests and analyze_pcm
        # both go through that.
        hz, nacf = frame.get("f0", (None, 0.0))
        if hz is None:
            continue
        f0s.append(hz)
        nacfs.append(nacf)
    dur = region["end"] - region["start"]
    voiced_ratio = (len(f0s) / len(frames)) if frames else 0.0
    f0_cv = _cv(f0s)
    semis = _semitones(f0s)
    rms_cv = _cv(rms)
    zcr_mean = sum(zcr) / len(zcr) if zcr else 0.0
    nacf_med = _percentile(nacfs, 0.50) if nacfs else 0.0
    peaks = _peaks(rms, _HOP_S)
    peak_rate = ((len(peaks) - 1) / dur) if len(peaks) >= 2 and dur > 0 else 0.0
    region.update({
        "dur": dur,
        "voiced_ratio": voiced_ratio,
        "f0_cv": f0_cv,
        "semitones": semis,
        "rms_cv": rms_cv,
        "zcr_mean": zcr_mean,
        "nacf_med": nacf_med,
        "n_peaks": len(peaks),
        "peak_rate": peak_rate,
        "f0_mean": (sum(f0s) / len(f0s)) if f0s else 0.0,
        "n_voiced": len(f0s),
    })
    kind, confidence, text, detail = _decide(region)
    region["kind"] = kind
    region["confidence"] = confidence
    region["text"] = text
    region["detail"] = detail


def _decide(m: dict) -> tuple[str, float, str, str]:
    dur = m["dur"]
    # Unvoiced noise: a breath, a rustle, a cough of air. Not a hum.
    if m["voiced_ratio"] < 0.40 and m["zcr_mean"] >= 0.18:
        return ("breath", 0.85, "breath",
                "breath or noise, unvoiced — left in")
    # Pulses several times a second with a swinging envelope: laughter.
    # Checked before the hum gates so a bursty voiced sound cannot pass
    # them on the frames that happen to be steady.
    if m["n_peaks"] >= 3 and m["peak_rate"] >= 4.0 and m["rms_cv"] >= 0.35:
        return ("laugh", 0.86, "laugh",
                f"laughter-like bursts ({m['n_peaks']} peaks, "
                f"{m['peak_rate']:.1f}/s) — left in")
    steady = (
        _MIN_DUR <= dur <= _MAX_DUR
        and m["n_voiced"] >= 6
        and m["voiced_ratio"] >= _MIN_VOICED_RATIO
        and m["nacf_med"] >= _MIN_MEDIAN_NACF
        and m["f0_cv"] <= _MAX_F0_CV
        and m["semitones"] <= _MAX_SEMITONES
        and m["rms_cv"] <= _MAX_RMS_CV
        and m["n_peaks"] <= 2
        and m["zcr_mean"] <= _MAX_ZCR
    )
    if steady:
        score = 0.74
        score += 0.08 * min(1.0, (m["voiced_ratio"] - _MIN_VOICED_RATIO) / 0.32)
        score += 0.08 * min(1.0, (_MAX_F0_CV - m["f0_cv"]) / _MAX_F0_CV)
        score += 0.05 * min(1.0, (_MAX_SEMITONES - m["semitones"]) / _MAX_SEMITONES)
        score += 0.05 * min(1.0, (_MAX_RMS_CV - m["rms_cv"]) / _MAX_RMS_CV)
        score = max(0.72, min(0.97, score))
        detail = (
            f"voiced hesitation, steady pitch "
            f"({m['f0_mean']:.0f}Hz, {m['semitones']:.1f} st, {dur:.2f}s)"
        )
        return "hesitation", score, "אמ", detail
    # Everything else that was loud enough to segment is treated as speech
    # (the other host, a monotone sentence, a long held note). A hum longer
    # than _MAX_DUR lands here on purpose: two seconds of steady voice can
    # be a person talking, and we do not cut that.
    if (m["voiced_ratio"] >= 0.35 or m["semitones"] > _MAX_SEMITONES
            or dur > _MAX_DUR or m["n_peaks"] >= 3 or m["f0_cv"] > _MAX_F0_CV):
        why = []
        if dur > _MAX_DUR:
            why.append(f"{dur:.1f}s is too long for a hum")
        if m["semitones"] > _MAX_SEMITONES:
            why.append(f"pitch moves {m['semitones']:.1f} st")
        if m["n_peaks"] >= 3:
            why.append(f"{m['n_peaks']} syllables")
        if m["f0_cv"] > _MAX_F0_CV:
            why.append(f"pitch variance {m['f0_cv']:.2f}")
        if not why:
            why.append("not a steady hum")
        return ("speech", 0.80, "speech",
                "speech-like (" + ", ".join(why) + ") — left in")
    return ("uncertain", 0.40, "voiced",
            "voiced sound, not steady enough to cut — left in")


def _clamp_cut(region: dict, padding: float, min_cut: float,
               min_confidence: float) -> None:
    g0, g1 = region["gap_start"], region["gap_end"]
    cs = max(g0, region["start"] - padding)
    ce = min(g1, region["end"] + padding)
    region["cut_start"] = cs
    region["cut_end"] = ce
    eligible = (
        region["kind"] == "hesitation"
        and region["confidence"] >= min_confidence
        and ce - cs >= min_cut
    )
    region["eligible"] = eligible
    region["reason"] = "audio" if eligible else region["kind"]
    if region["kind"] == "hesitation" and region["confidence"] < min_confidence:
        region["eligible"] = False
        region["reason"] = "low_confidence"
        region["detail"] += f"; confidence {region['confidence']:.2f} below {min_confidence:.2f}"
    elif region["kind"] == "hesitation" and ce - cs < min_cut:
        region["eligible"] = False
        region["reason"] = "too_short"
        region["detail"] += f"; span under {min_cut:.2f}s — left in"


def _separate(regions: list[dict]) -> None:
    """Drop a hum that is glued to speech or laughter in the same gap."""
    blockers = [r for r in regions if r["kind"] in ("speech", "laugh")]
    if not blockers:
        return
    for region in regions:
        if region["kind"] != "hesitation" or not region["eligible"]:
            continue
        for other in blockers:
            if _gap_between(region, other) < _SEPARATION_S:
                region["eligible"] = False
                region["reason"] = "adjacent"
                region["detail"] += (
                    " — left in; speech or laughter shares this gap"
                )
                break


def _gap_between(a: dict, b: dict) -> float:
    if a["end"] <= b["start"]:
        return b["start"] - a["end"]
    if b["end"] <= a["start"]:
        return a["start"] - b["end"]
    return 0.0


def analyze_pcm(samples, sample_rate: int, words: list[dict], duration: float, *,
                gap: float, padding: float, min_cut: float,
                min_confidence: float) -> list[dict]:
    """Classify voiced events inside gaps.

    Each dict is a candidate the planner turns into a `FillerHit`:
    start/end are the sound, cut_start/cut_end the padded edit (clamped to
    the gap, so a neighbour word is never eaten), eligible is True only for
    a hesitation that cleared the confidence bar and is not glued to speech
    or laughter.
    """
    if not samples or sample_rate <= 0 or duration <= 0:
        return []
    frames = _frame_table(samples, sample_rate)
    if not frames:
        return []
    _attach_pitch(frames, samples, sample_rate)
    threshold = _energy_threshold(frames, words)
    found: list[dict] = []
    for g0, g1 in iter_gaps(words, duration, gap):
        inside = [f for f in frames if g0 - 1e-3 <= f["t"] < g1 - 1e-3]
        if len(inside) < 3:
            continue
        regions = _regions(inside, threshold, sample_rate)
        for region in regions:
            region["gap_start"] = g0
            region["gap_end"] = g1
            _label(region)
            _clamp_cut(region, padding, min_cut, min_confidence)
        _separate(regions)
        found.extend(regions)
    return found


def _attach_pitch(frames: list[dict], samples, sr: int) -> None:
    """Cache (hz, nacf) on frames that are loud enough to possibly be voiced.

    Quiet frames skip the autocorrelation. A 90s span is mostly speech the
    gap walker never asks about; this still prices every frame once so a
    gap can read f0 without slicing the buffer again.
    """
    # A cheap pre-pass: only pitch frames above a very low floor. The real
    # threshold is per-span and may be higher; computing f0 for room tone
    # would dominate and those frames are unvoiced anyway.
    for frame in frames:
        if frame["rms"] < _ABS_RMS * 0.75:
            frame["f0"] = (None, 0.0)
            continue
        seg = samples[frame["i"]:frame["i"] + frame["win"]]
        frame["f0"] = _f0(seg, sr)


def _f0(seg, sr: int) -> tuple[float | None, float]:
    n = len(seg)
    if n < 16:
        return None, 0.0
    mean = sum(seg) / n
    energy = 0.0
    xs = [0.0] * n
    for i, x in enumerate(seg):
        v = x - mean
        xs[i] = v
        energy += v * v
    if energy < 1e-8:
        return None, 0.0
    min_lag = max(1, int(sr / _F0_MAX))
    max_lag = min(n - 2, int(sr / _F0_MIN))
    if max_lag <= min_lag:
        return None, 0.0
    best_lag = 0
    best = 0.0
    for lag in range(min_lag, max_lag + 1):
        corr = 0.0
        limit = n - lag
        for i in range(limit):
            corr += xs[i] * xs[i + lag]
        corr /= energy
        if corr > best:
            best = corr
            best_lag = lag
    if best < _VOICED_NACF or best_lag <= 0:
        return None, best
    return sr / best_lag, best


def _rms(seg) -> float:
    if not seg:
        return 0.0
    acc = 0.0
    for x in seg:
        acc += x * x
    return math.sqrt(acc / len(seg))


def _zcr(seg) -> float:
    if len(seg) < 2:
        return 0.0
    n = 0
    prev = seg[0]
    for x in seg[1:]:
        if (prev >= 0.0) != (x >= 0.0):
            n += 1
        prev = x
    return n / (len(seg) - 1)


def _cv(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    if abs(mean) < 1e-8:
        return 0.0
    var = sum((x - mean) ** 2 for x in xs) / len(xs)
    return math.sqrt(var) / abs(mean)


def _semitones(f0s: list[float]) -> float:
    if len(f0s) < 2:
        return 0.0
    lo, hi = min(f0s), max(f0s)
    if lo <= 1.0:
        return 99.0
    return 12.0 * math.log2(hi / lo)


def _peaks(rms: list[float], hop: float) -> list[float]:
    if len(rms) < 3:
        return []
    med = _percentile(rms, 0.50)
    if med <= 1e-8:
        return []
    level = med * 1.35
    found = []
    last = -1.0
    for i in range(1, len(rms) - 1):
        if rms[i] >= rms[i - 1] and rms[i] > rms[i + 1] and rms[i] >= level:
            t = i * hop
            if t - last >= 0.09:
                found.append(t)
                last = t
    return found


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = int(round((len(s) - 1) * p))
    i = min(len(s) - 1, max(0, i))
    return s[i]
