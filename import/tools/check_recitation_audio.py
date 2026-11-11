#!/usr/bin/env python3
"""Check a recitation set's ayah timings against its decoded audio, and audit its long gaps.

    <venv python> -I tools/check_recitation_audio.py --set juhani-hafs-v1            # write content/recitations/<id>/audio_check.json
    <venv python> -I tools/check_recitation_audio.py --set huthaifi-hafs-v1 --read 74 --report-only     # MP3Quran's draft on the same files
    <venv python> -I tools/check_recitation_audio.py --set juhani-hafs-v1 --locate-clips https://everyayah.com/data/Abdullaah_3awwaad_Al-Juhaynee_128kbps/ --whole-surahs 4 --measure 4:87
    <venv python> -I tools/check_recitation_audio.py --set hassan-saleh-hafs-v2 --locate-ayah-clips     # the set's timings, from ayah clips cut from its own files

Needs numpy and miniaudio. The staged MP3s are untrusted downloads: run with -I; this script only decodes them. Envelopes are
cached under <staging>/analysis/<set id>/ (never beside the MP3s: the publish script uploads every MP3 of that folder).

Evidence: a 10 ms RMS envelope (20 ms window) of the file decoded to 16 kHz, both channels. "Quiet" follows tools/build_warsh.py:
below floor + 0.3 x (speech - floor), floor/speech being the file's 5th/60th percentile dB (the floor no lower than -75 dBFS,
because some files hold digital silence). A timing made for these bytes puts ayah starts on pauses, at the same place in every
surah; ayah lengths follow the ayah's letter count. Wasl (an ayah joined to the next) leaves no pause, so a flag is a place to
listen, not an error.

Gap audit: every gap over 10 s between one ayah's end and the next ayah's start. The app skips a gap over 20 s from 1 s after
the end to 0.2 s before the next start (AyahTimeline.skipTarget), so that stretch is what must be silent. "audible" counts voiced
runs of at least 300 ms; a run above -45 dBFS is called sound, whatever the file's own threshold says.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_recitation_set as prs  # noqa: E402

HOP = 10              # ms per envelope frame
RATE = 16000
MIN_QUIET = 8         # frames: a pause is at least 80 ms
NEAR = 5              # frames of tolerance: a start within 50 ms of a pause is on it
FLAG_MS = 1000        # a start further than this from any pause is listed
LONG_GAP = 10000
SKIP_GAP = 20000      # AyahTimeline.SKIP_BREAK_MS
SKIP_AFTER_END = 1000
SKIP_LEAD = 200
AUDIBLE_RUN = 30      # frames
LOUD_DB = -45.0


def envelope(path: Path, cache: Path):
    import numpy as np
    if cache.is_file():
        return np.load(cache)
    import miniaudio
    block = RATE * HOP // 1000
    energies, rest = [], np.zeros(0, dtype=np.int16)
    both = mono = 0.0
    # Two channels, summed as energy, never as samples: some files have one channel inverted (Hassan Saleh's 19 surahs), and a
    # mono downmix of them is 50 dB down: what MP3Quran's own timing tool seems to have heard there.
    for chunk in miniaudio.stream_file(str(path), output_format=miniaudio.SampleFormat.SIGNED16, nchannels=2,
                                       sample_rate=RATE, frames_to_read=RATE * 20):
        x = np.concatenate([rest, np.frombuffer(chunk, dtype=np.int16)])
        n = len(x) // (2 * block)
        if n:
            y = x[:n * 2 * block].astype(np.float64) / 32768.0
            energies.append((y.reshape(n, 2 * block) ** 2).sum(axis=1) / 2)
            both += float((y ** 2).sum()) / 2
            mono += float(((y[::2] + y[1::2]) / 2) ** 2 @ np.ones(len(y) // 2))
        rest = x[n * 2 * block:]
    e = np.concatenate(energies)
    window = (e + np.append(e[1:], e[-1])) / (2 * block)      # frame i covers [i * 10 ms, i * 10 ms + 20 ms)
    db = (10 * np.log10(np.maximum(window, 1e-12))).astype(np.float32)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.save(cache, db)
    # How much quieter the file is when its channels are mixed: about 0 dB normally, far more if one channel is inverted.
    cache.with_suffix(".json").write_text(json.dumps({"monoDropDb": round(10 * math.log10(max(both, 1e-12) / max(mono, 1e-12)), 1)}), encoding="utf-8")
    return db


def levels(db) -> tuple[float, float, float]:
    import numpy as np
    floor = max(float(np.percentile(db, 5)), -75.0)
    speech = float(np.percentile(db, 60))
    return floor + 0.3 * (speech - floor), floor, speech


def runs_of(mask) -> list[tuple[int, int]]:
    import numpy as np
    edges = np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(np.int8), [0]])))
    return list(zip(edges[::2].tolist(), edges[1::2].tolist()))


def corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    a = sum((x - mx) ** 2 for x in xs)
    b = sum((y - my) ** 2 for y in ys)
    if not a or not b:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (a * b) ** 0.5


def stretch(db, thr: float, a_ms: int, b_ms: int) -> dict:
    """What can be heard in [a_ms, b_ms)."""
    import numpy as np
    x = db[max(0, a_ms // HOP):max(0, b_ms // HOP)]
    if not len(x):
        return {"ms": 0, "audibleMs": 0, "longestAudibleRunMs": 0, "maxDb": None, "medianDb": None, "audibleRuns": []}
    voiced = runs_of(x >= thr)
    audible = [(s, e) for s, e in voiced if e - s >= AUDIBLE_RUN]
    return {"ms": len(x) * HOP, "audibleMs": sum(e - s for s, e in audible) * HOP,
            "longestAudibleRunMs": max((e - s for s, e in audible), default=0) * HOP,
            "maxDb": round(float(x.max()), 1), "medianDb": round(float(np.median(x)), 1),
            "audibleRuns": [[a_ms + s * HOP, a_ms + e * HOP, round(float(x[s:e].max()), 1)] for s, e in audible[:12]]}


WINDOW = 15            # ayat: a run of this many starts is judged together
UNFIT = 0.5            # ... and is "unfit" when under half of them lie on a pause, even shifted by up to 300 ms


def unfit_windows(items: list[dict], quiet) -> list[dict]:
    """Runs of ayat whose starts do not fit the audio: where a timing was made for another edit of the file."""
    import numpy as np
    out = []
    for i in range(0, max(1, len(items) - WINDOW + 1), WINDOW):
        part = np.array([r["startMs"] // HOP for r in items[i:i + WINDOW]])
        if len(part) < WINDOW:
            continue

        def share(d: int) -> float:
            return float(quiet[(part + d // HOP).clip(0, len(quiet) - 1)].mean())

        best = max((share(d), -abs(d), d) for d in range(-300, 301, 20))
        if best[0] < UNFIT:
            out.append({"from": f"{items[i]['surah']}:{items[i]['ayah']}", "to": f"{items[i + WINDOW - 1]['surah']}:{items[i + WINDOW - 1]['ayah']}",
                        "onPause": round(share(0), 2), "bestWithin300ms": round(best[0], 2)})
    return out


def letters_of(riwayah: str) -> dict[tuple[int, int], int]:
    if riwayah == "warsh":
        text = prs.ROOT / "android/app/src/main/assets/warsh/quran-warsh-plain.txt"
    else:
        text = prs.ROOT / "android/app/src/main/assets/content/quran-simple-clean.txt"
    out = {}
    for line in text.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            s, a, words = line.split("|", 2)
            out[int(s), int(a)] = len(words.replace(" ", ""))
    return out


def mp3quran_rows(staging: Path, read: int) -> list[dict]:
    rows = []
    for surah in range(1, 115):
        for e in json.loads((staging / "mp3quran-api" / f"ayat_timing-read{read}" / f"{surah:03}.json").read_text(encoding="utf-8")):
            if e["ayah"]:
                rows.append({"surah": surah, "ayah": e["ayah"], "startMs": e["start_time"], "endMs": e["end_time"]})
    return rows


def check(set_id: str, rows: list[dict], staging: Path, label: str) -> dict:
    import numpy as np
    spec = prs.SETS[set_id]
    letters = letters_of(spec.get("riwayah", "hafs"))
    by_surah: dict[int, list[dict]] = {}
    for row in rows:
        by_surah.setdefault(row["surah"], []).append(row)
    per, flagged, gaps, lead, tail, unfit, inverted = [], [], [], [], [], [], []
    totals = {"starts": 0, "on": 0, "w250": 0, "w1000": 0, "ends": 0, "endsOn": 0}
    onsets_all: list[float] = []
    for surah, items in sorted(by_surah.items()):
        db = envelope(staging / set_id / f"{surah:03}.mp3", staging / "analysis" / set_id / f"{surah:03}.npy")
        thr, floor, speech = levels(db)
        drop = json.loads((staging / "analysis" / set_id / f"{surah:03}.json").read_text(encoding="utf-8"))["monoDropDb"]
        if drop > 6:
            inverted.append({"surah": surah, "monoDropDb": drop})
        quiet_runs = [(a, b) for a, b in runs_of(db < thr) if b - a >= MIN_QUIET]
        quiet = np.zeros(len(db) + 2, bool)
        for a, b in quiet_runs:
            quiet[max(0, a - NEAR):b + NEAR + 1] = True
        ends = np.array([b for _, b in quiet_runs]) if quiet_runs else np.zeros(1, int)
        starts_f = np.array([r["startMs"] // HOP for r in items])
        ends_f = np.array([r["endMs"] // HOP for r in items])
        inside = (starts_f >= 0) & (starts_f < len(quiet))

        def on_pause(shift: int) -> float:
            f = starts_f + shift // HOP
            f = f[(f >= 0) & (f < len(quiet))]
            return float(quiet[f].mean()) if len(f) else 0.0

        unfit += unfit_windows(items, quiet)
        base = on_pause(0)
        best = max((on_pause(d), -abs(d), d) for d in range(-3000, 3001, 50))
        onsets = []
        for r, f in zip(items, starts_f.tolist()):
            totals["starts"] += 1
            if not 0 <= f < len(quiet):
                flagged.append({"ayah": f"{surah}:{r['ayah']}", "startMs": r["startMs"], "problem": "outside the file"})
                continue
            if quiet[f]:
                totals["on"] += 1
                totals["w250"] += 1
                totals["w1000"] += 1
                # Where the voice resumes after this pause, relative to the timed start.
                i = int(np.searchsorted(ends, f - NEAR))
                if i < len(ends) and r["ayah"] > 1:
                    onsets.append((int(ends[i]) - f) * HOP)
                continue
            dist = min((min(abs(f - a), abs(f - b)) for a, b in quiet_runs), default=10 ** 7) * HOP
            totals["w250"] += dist <= 250
            totals["w1000"] += dist <= FLAG_MS
            if dist > FLAG_MS:
                flagged.append({"ayah": f"{surah}:{r['ayah']}", "startMs": r["startMs"], "nearestPauseMs": dist})
        e_in = ends_f[(ends_f >= 0) & (ends_f < len(quiet))]
        totals["ends"] += len(e_in)
        totals["endsOn"] += int(quiet[e_in].sum())
        xs = [float(letters[surah, r["ayah"]]) for r in items if (surah, r["ayah"]) in letters]
        ys = [float(r["endMs"] - r["startMs"]) for r in items if (surah, r["ayah"]) in letters]
        r_corr = corr(xs, ys)
        if onsets:
            onsets_all += onsets
        per.append({"surah": surah, "ayat": len(items), "startsOnPause": round(base, 3), "bestShiftMs": best[2],
                    "startsOnPauseAtBestShift": round(best[0], 3),
                    "medianVoiceAfterStartMs": statistics.median(onsets) if onsets else None,
                    "lengthVsLettersR": round(r_corr, 3) if r_corr is not None else None,
                    "floorDb": round(floor, 1), "speechDb": round(speech, 1)})
        lead.append({"surah": surah, "ayah1StartMs": items[0]["startMs"],
                     "audibleBeforeAyah1Ms": stretch(db, thr, 0, items[0]["startMs"])["audibleMs"]})
        duration = len(db) * HOP
        after = stretch(db, thr, items[-1]["endMs"], duration)
        tail.append({"surah": surah, "lastEndMs": items[-1]["endMs"], "decodedMs": duration,
                     "audibleAfterLastAyahMs": after["audibleMs"], "afterMs": after["ms"], "longestAudibleRunMs": after["longestAudibleRunMs"]})
        for a, b in zip(items, items[1:]):
            gap = b["startMs"] - a["endMs"]
            if gap > LONG_GAP:
                whole = stretch(db, thr, a["endMs"], b["startMs"])
                record = {"after": f"{surah}:{a['ayah']}", "before": f"{surah}:{b['ayah']}", "endMs": a["endMs"], "nextStartMs": b["startMs"], "gapMs": gap,
                          "thresholdDb": round(thr, 1), "audibleMs": whole["audibleMs"], "longestAudibleRunMs": whole["longestAudibleRunMs"],
                          "maxDb": whole["maxDb"], "medianDb": whole["medianDb"], "audibleRuns": whole["audibleRuns"]}
                if gap > SKIP_GAP:
                    cut = stretch(db, thr, a["endMs"] + SKIP_AFTER_END, b["startMs"] - SKIP_LEAD)
                    record["skipped"] = {"fromMs": a["endMs"] + SKIP_AFTER_END, "toMs": b["startMs"] - SKIP_LEAD, "audibleMs": cut["audibleMs"],
                                         "longestAudibleRunMs": cut["longestAudibleRunMs"], "maxDb": cut["maxDb"], "audibleRuns": cut["audibleRuns"],
                                         "verdict": "silent" if not cut["audibleMs"] and (cut["maxDb"] or -99) < LOUD_DB else "SOUND"}
                gaps.append(record)
    judged = [p for p in per if p["ayat"] >= 5]
    setting = statistics.median(p["medianVoiceAfterStartMs"] for p in judged if p["medianVoiceAfterStartMs"] is not None)
    all_gaps = sorted(((b["startMs"] - a["endMs"], f"{s}:{a['ayah']}") for s, items in by_surah.items() for a, b in zip(items, items[1:])), reverse=True)
    summary = {
        "label": label, "ayat": totals["starts"],
        "startsOnPause": round(totals["on"] / totals["starts"], 4), "startsWithin250msOfPause": round(totals["w250"] / totals["starts"], 4),
        "startsWithin1sOfPause": round(totals["w1000"] / totals["starts"], 4),
        "endsOnPause": round(totals["endsOn"] / max(1, totals["ends"]), 4),
        "medianVoiceAfterStartMs": statistics.median(onsets_all) if onsets_all else None,
        "medianVoiceAfterStartPerSurahRangeMs": [min(p["medianVoiceAfterStartMs"] for p in judged if p["medianVoiceAfterStartMs"] is not None),
                                                 max(p["medianVoiceAfterStartMs"] for p in judged if p["medianVoiceAfterStartMs"] is not None)],
        "surahsOffTheSetMedianBy400msOrMore": [{"surah": p["surah"], "ms": p["medianVoiceAfterStartMs"]} for p in judged
                                               if p["medianVoiceAfterStartMs"] is not None and abs(p["medianVoiceAfterStartMs"] - setting) >= 400],
        "surahsAShiftImprovesBy25PercentPoints": [{"surah": p["surah"], "startsOnPause": p["startsOnPause"], "bestShiftMs": p["bestShiftMs"], "atBest": p["startsOnPauseAtBestShift"]}
                                                  for p in judged if p["startsOnPauseAtBestShift"] - p["startsOnPause"] >= 0.25],
        "lengthVsLettersMedianR": statistics.median(p["lengthVsLettersR"] for p in judged if p["lengthVsLettersR"] is not None),
        "surahsLengthVsLettersRBelow0.6": [{"surah": p["surah"], "r": p["lengthVsLettersR"], "ayat": p["ayat"]} for p in judged
                                           if p["lengthVsLettersR"] is not None and p["ayat"] >= 8 and p["lengthVsLettersR"] < 0.6],
        "startsOver1sFromAnyPause": flagged,
        "windowsOf15AyatThatDoNotFit": unfit,
        "surahsWithOneChannelInverted": inverted,
        "gapsOver10s": sum(1 for g in gaps if g["gapMs"] > LONG_GAP), "gapsOver20s": sum(1 for g in gaps if g["gapMs"] > SKIP_GAP),
        "gapsOver2s": sum(1 for g, _ in all_gaps if g > 2000),
        "longestGap": {"after": all_gaps[0][1], "ms": all_gaps[0][0]} if all_gaps else None,
        "gapAudit": gaps,
        "surahsWithAudioAfterLastAyahOver2s": [t for t in tail if t["audibleAfterLastAyahMs"] > 2000],
        "surahsWithAudioBeforeAyah1Over15s": [x for x in lead if x["audibleBeforeAyah1Ms"] > 15000],
        "tawbahAudibleBeforeAyah1Ms": next((x["audibleBeforeAyah1Ms"] for x in lead if x["surah"] == 9), None),
    }
    return {"summary": summary, "perSurah": per, "leadIn": lead, "tail": tail}


def locate_clips(set_id: str, staging: Path, base: str, whole_surahs: list[int], measure: list[tuple[int, int]]) -> dict:
    """Find per-ayah clips of the same recording (everyayah) inside the set's files, by normalized cross-correlation of the envelopes.

    An independent measure of where each ayah is in these bytes: the first, middle and last ayah of every surah, every ayah of
    `whole_surahs`. `measure` lists (surah, first ayah) ranges to write as rows from these positions: "apply", which
    tools/prepare_recitation_set.py uses in place of the timing source's numbers there.
    """
    import numpy as np
    spec = prs.SETS[set_id]
    counts = prs.ayah_counts(spec)
    rows = {(r["surah"], r["ayah"]): r for r in json.loads((prs.OUTPUT / set_id / spec["timingFile"]).read_text(encoding="utf-8"))["rows"]}
    folder = staging / "clips" / set_id
    folder.mkdir(parents=True, exist_ok=True)
    log: dict = {}
    clips, found = [], {}
    for surah in range(1, 115):
        count = counts[surah - 1]
        ayat = range(1, count + 1) if surah in whole_surahs else sorted({1, (count + 1) // 2, count})
        y = envelope(staging / set_id / f"{surah:03}.mp3", staging / "analysis" / set_id / f"{surah:03}.npy").astype(np.float64)
        cum = np.concatenate([[0.0], np.cumsum(y)])
        cum2 = np.concatenate([[0.0], np.cumsum(y ** 2)])
        size = 1 << int(np.ceil(np.log2(len(y) + 200000)))
        spectrum = np.fft.rfft(y, size)
        for ayah in ayat:
            name = f"{surah:03}{ayah:03}.mp3"
            if not (folder / name).is_file():
                prs._fetch(base + name, folder / name, log)
            e = envelope(folder / name, staging / "analysis" / "clips" / set_id / name.replace(".mp3", ".npy"))
            x = (e - e.mean()) / (e.std() + 1e-9)
            m = len(x)
            cc = np.fft.irfft(spectrum * np.conj(np.fft.rfft(x, size)), size)[:len(y) - m + 1]
            std = np.sqrt(np.maximum((cum2[m:] - cum2[:-m]) / m - ((cum[m:] - cum[:-m]) / m) ** 2, 1e-9))
            r = cc / (m * std)
            k = int(np.argmax(r))
            row = rows[surah, ayah]
            found[surah, ayah] = (k * HOP, m * HOP, float(r[k]))
            clips.append({"ayah": f"{surah}:{ayah}", "clipMs": m * HOP, "foundAtMs": k * HOP, "r": round(float(r[k]), 3),
                          "startDeltaMs": k * HOP - row["startMs"], "endDeltaMs": k * HOP + m * HOP - row["endMs"]})
    prs._write_log(staging, f"clips-{set_id}", log)
    apply = []
    for surah, first in measure:
        for ayah in range(first, counts[surah - 1] + 1):
            at, length, r = found[surah, ayah]
            assert r >= 0.9, (surah, ayah, r)
            following = found.get((surah, ayah + 1))
            apply.append({"surah": surah, "ayah": ayah, "startMs": at, "endMs": following[0] if following else at + length})
    good = [c for c in clips if c["r"] >= 0.9]
    deltas = sorted(abs(c["startDeltaMs"]) for c in good)
    return {"source": base + "SSSAAA.mp3", "method": "normalized cross-correlation of 10 ms envelopes", "clips": len(clips), "matchedAtR0.9": len(good),
            "startDeltaMsPercentiles": {"p50": deltas[len(deltas) // 2], "p90": deltas[len(deltas) * 9 // 10], "p99": deltas[len(deltas) * 99 // 100], "max": deltas[-1]},
            "over250ms": [c for c in clips if abs(c["startDeltaMs"]) > 250], "weakMatches": [c for c in clips if c["r"] < 0.9],
            "apply": apply, "perClip": clips}


WAVE_RATE = 8000       # Hz, mono: enough to tell whether two decodes are the same audio
WAVE_LOCKED = 0.8      # a clip located at this waveform correlation or above fixes its ayah's start
CLIP_LEAD_MS = 50      # a clip's encoder and decoder delay: measured at surah starts (24, 43, 112), a clip's audio lies 50 ms later in the clip than in the file


def _wave(path: Path):
    import miniaudio
    import numpy as np
    decoded = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=WAVE_RATE)
    return np.frombuffer(decoded.samples, dtype=np.int16).astype(np.float32) / 32768.0


def _wave_ncc(y, x):
    """Normalized correlation of x at every position of y."""
    import numpy as np
    m = len(x)
    size = 1 << int(np.ceil(np.log2(len(y) + m)))
    z = (x - x.mean()) / (x.std() + 1e-9)
    cc = np.fft.irfft(np.fft.rfft(y, size) * np.conj(np.fft.rfft(z, size)), size)[:len(y) - m + 1]
    wide = y.astype(np.float64)
    cum = np.concatenate([[0.0], np.cumsum(wide)])
    cum2 = np.concatenate([[0.0], np.cumsum(wide ** 2)])
    std = np.sqrt(np.maximum((cum2[m:] - cum2[:-m]) / m - ((cum[m:] - cum[:-m]) / m) ** 2, 1e-9))
    return cc / (m * std)


def locate_ayah_clips(set_id: str, staging: Path) -> dict:
    """Times for a set from ayah clips cut from its own files: each clip is found by the correlation of the waveform, near where the last one ended.

    A clip cut from these very files correlates near 1 at one exact place, even re-encoded, which a recording that only sounds alike never does.
    The clips' first 0.15 s and last 0.3 s are left out of the comparison (their encoder's ramp; at a surah's start a clip reaches before the file), and the last
    1.2 s of a surah's last clip, which fades out and may reach past the end of the file. An ayah's start is where its clip's audio lies, CLIP_LEAD_MS after the
    clip's first sample; its end is the next ayah's start, the last one's the end of its clip, at most the file's end. A clip under WAVE_LOCKED is not used:
    its ayah starts where the clip before it ended (`ayahStartsNotLocked` counts them).
    """
    spec = prs.SETS[set_id]
    counts = prs.ayah_counts(spec)
    folder = staging / "clips" / set_id
    rows, clips, per_surah = [], [], []
    for surah in range(1, 115):
        y = _wave(staging / set_id / f"{surah:03}.mp3")
        duration = round(len(y) * 1000 / WAVE_RATE)
        found: dict[int, tuple[int, int, float]] = {}
        pos = 0
        names = sorted(folder.glob(f"{surah:03}???.mp3"))
        for path in names:
            ayah = int(path.stem[3:])
            x = _wave(path)
            m = len(x)
            head = WAVE_RATE * 15 // 100 if m > WAVE_RATE else 0
            tail = (WAVE_RATE * 12 // 10 if m > 3 * WAVE_RATE else WAVE_RATE * 3 // 10) if path == names[-1] and m > WAVE_RATE else (WAVE_RATE * 3 // 10 if m > WAVE_RATE else 0)
            piece = x[head:m - tail]
            best = (-1.0, 0)
            for reach in (2, 20, 120):          # seconds around where the last clip ended
                lo, hi = max(0, pos + head - reach * WAVE_RATE), min(len(y), pos + head + len(piece) + reach * WAVE_RATE)
                if hi - lo <= len(piece):
                    continue
                r = _wave_ncc(y[lo:hi], piece)
                k = int(r.argmax())
                if float(r[k]) > best[0]:
                    best = (float(r[k]), lo + k - head)
                if best[0] >= WAVE_LOCKED:
                    break
            found[ayah] = (round(best[1] * 1000 / WAVE_RATE), round(m * 1000 / WAVE_RATE), round(best[0], 3))
            clips.append({"clip": path.name, "atMs": found[ayah][0], "lenMs": found[ayah][1], "r": found[ayah][2], "jumpMs": round((best[1] - pos) * 1000 / WAVE_RATE)})
            pos = best[1] + m if best[0] >= WAVE_LOCKED else pos + m     # not locked: the next one is expected right after this clip's length
        count = counts[surah - 1]
        assert sorted(a for a in found if a) == list(range(1, count + 1)), (surah, sorted(found))
        starts: dict[int, int] = {}
        unlocked = []
        for ayah in range(1, count + 1):
            at, _, r = found[ayah]
            if r >= WAVE_LOCKED:
                starts[ayah] = max(0, at + CLIP_LEAD_MS)
                continue
            unlocked.append(ayah)
            before = found.get(ayah - 1)
            starts[ayah] = max(starts.get(ayah - 1, 0), before[0] + before[1] + CLIP_LEAD_MS if before else 0)
        for ayah in range(1, count + 1):
            end = starts[ayah + 1] if ayah < count else min(starts[ayah] + found[ayah][1] - CLIP_LEAD_MS, duration)
            rows.append({"surah": surah, "ayah": ayah, "startMs": starts[ayah], "endMs": end})
        per_surah.append({"surah": surah, "fileMs": duration, "clips": len(found), "startsNotLocked": unlocked, "lastClipR": found[count][2],
                          "afterLastAyahMs": duration - rows[-1]["endMs"], "ayah1StartMs": starts[1]})
        print(surah, len(found), "r min", min(v[2] for v in found.values()), "not locked", unlocked, flush=True)
    locked = sorted(c["r"] for c in clips if c["r"] >= WAVE_LOCKED)
    # A join: how far a clip lies from where the one before it ended. Small all the way means the clips are this file, cut in order, with nothing left out.
    joins = sorted(abs(c["jumpMs"]) for c in clips if c["r"] >= WAVE_LOCKED and not c["clip"].endswith("001.mp3"))
    over = [{"clip": c["clip"], "jumpMs": c["jumpMs"], "r": c["r"]} for c in clips if abs(c["jumpMs"]) > 300 and not c["clip"].endswith("001.mp3")]
    return {"source": f"https://archive.org/download/{spec['ayahClips']}/SSSAAA.mp3", "method": "normalized cross-correlation of the waveform, 8 kHz mono, near where the previous clip ended",
            "clips": len(clips), "lockedAtR0.8": len(locked), "lockedRPercentiles": {"p1": locked[len(locked) // 100], "p50": locked[len(locked) // 2], "lowest": locked[0]},
            "joinMsPercentiles": {"p50": joins[len(joins) // 2], "p99": joins[len(joins) * 99 // 100], "max": joins[-1]}, "joinsOver300ms": over,
            "ayahStartsNotLocked": sum(len(p["startsNotLocked"]) for p in per_surah), "apply": [], "rows": rows, "perSurah": per_surah, "perClip": clips}


def seek_report(path: Path) -> dict:
    """What a player that seeks by the first frame's bitrate does to this file, and what the manifest's `durationMs` fixes.

    Walks every frame: the true time of each frame's first byte against the constant-bitrate estimate (bytes from the first frame
    x 8 / first frame bitrate), as Media3's ConstantBitrateSeeker and AVFoundation estimate it. `uncorrectedS` is the worst gap;
    `correctedS` the worst gap once the estimate is scaled to make the file's end exact (`SeekScale`), which is the app's seek."""
    data = path.read_bytes()
    pos = 0
    while data[pos:pos + 3] == b"ID3":
        pos += 10 + ((data[pos + 6] << 21) | (data[pos + 7] << 14) | (data[pos + 8] << 7) | data[pos + 9])
    frames = []
    end = len(data)
    t = 0.0
    first = None
    tag_frame = False
    while pos < end:
        header = prs._frame(data, pos, end)
        if header is None or pos + header[3] > end:
            pos += 1
            continue
        version, bitrate, rate, length, mode = header
        if first is None:
            first = (version, bitrate, rate, pos)
            side = (32 if mode != 3 else 17) if version == 3 else (17 if mode != 3 else 9)
            if data[pos + 4 + side:pos + 8 + side] in (b"Xing", b"Info"):
                tag_frame = True
                pos += length          # a tag frame carries no audio; the player's map still starts at it (the app blanks it)
                continue
        frames.append((pos, t))
        t += (1152 if version == 3 else 576) / rate
        pos += length
    version, bitrate, rate, start = first
    est = lambda p: (p - start) * 8 / (bitrate * 1000)
    uncorrected = max((abs(est(p) - ft) for p, ft in frames), default=0.0)
    factor = t / est(end) if est(end) > 0 else 1.0
    corrected = max((abs(est(p) * factor - ft) for p, ft in frames), default=0.0)
    return {"mpeg": "MPEG-1" if version == 3 else "MPEG-2", "kbps": bitrate, "rate": rate, "durationS": round(t, 3),
            "uncorrectedS": round(uncorrected, 3), "correctedS": round(corrected, 3), "xingOrInfo": tag_frame}


def seek_check(set_id: str, staging: Path) -> int:
    """Every file of a set: the manifest's durationMs equals the audio's real length (1 ms), and the corrected seek is within
    `SEEK_LIMIT_S` of the audio everywhere; also lists the files a plain constant-bitrate seek would put off by more than 1 s."""
    manifest_path, folder = prs.manifest_location(staging, set_id)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems, flagged, worst_c, worst_u = [], 0, 0.0, 0.0
    for track in manifest["tracks"]:
        info = seek_report(folder / track["file"])
        want = round(info["durationS"] * 1000)
        if track.get("durationMs") != want:
            problems.append(f"{track['file']}: manifest durationMs {track.get('durationMs')}, audio {want}")
        flagged += info["uncorrectedS"] > 1.0
        worst_c, worst_u = max(worst_c, info["correctedS"]), max(worst_u, info["uncorrectedS"])
        if info["correctedS"] > SEEK_LIMIT_S:
            problems.append(f"{track['file']}: even scaled, a seek is {info['correctedS']} s off ({info['kbps']} kbps {info['mpeg']} {info['rate']} Hz, "
                            f"{'Xing/Info header' if info['xingOrInfo'] else 'no header'}): the frames do not run at one rate")
    print(f"{set_id}: {len(manifest['tracks'])} files, plain bitrate seek worst {worst_u} s ({flagged} files over 1 s), with the manifest's durations worst {worst_c} s")
    for problem in problems:
        print(f"  FAIL {problem}")
    return len(problems)


SEEK_LIMIT_S = 1.5


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seek-check", action="store_true", help="check the manifest's durationMs and what a seek lands on, for every file of the set; writes nothing")
    parser.add_argument("--set", required=True, choices=sorted(prs.SETS) + sorted(prs.EXTRA_MANIFESTS))
    parser.add_argument("--staging", type=Path, default=prs.DEFAULT_STAGING)
    parser.add_argument("--read", type=int, help="check MP3Quran's ayat_timing read=N instead of the set's own timing file")
    parser.add_argument("--report-only", action="store_true", help="print the summary, write nothing")
    parser.add_argument("--locate-clips", metavar="BASE_URL", help="locate per-ayah clips of the same recording: writes clip_check.json")
    parser.add_argument("--locate-ayah-clips", action="store_true", help="time the set from ayah clips cut from its own files (<staging>/clips/<set id>/SSSAAA.mp3): writes clip_check.json")
    parser.add_argument("--whole-surahs", default="", help="with --locate-clips: comma-separated surahs to do every ayah of")
    parser.add_argument("--measure", default="", help="with --locate-clips: surah:ayah[,surah:ayah] ranges to write as measured rows, up to the surah's end")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.seek_check:
        raise SystemExit(1 if seek_check(args.set, args.staging) else 0)
    spec = prs.SETS[args.set]
    if args.locate_ayah_clips:
        result = locate_ayah_clips(args.set, args.staging)
        (prs.OUTPUT / args.set).mkdir(parents=True, exist_ok=True)
        (prs.OUTPUT / args.set / "clip_check.json").write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({k: v for k, v in result.items() if k not in ("perClip", "perSurah", "rows", "apply")}, ensure_ascii=False))
        return
    if args.locate_clips:
        whole = [int(x) for x in args.whole_surahs.split(",") if x]
        measure = [tuple(map(int, x.split(":"))) for x in args.measure.split(",") if x]
        result = locate_clips(args.set, args.staging, args.locate_clips, whole, measure)
        (prs.OUTPUT / args.set / "clip_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({k: v for k, v in result.items() if k not in ("perClip", "apply")}, ensure_ascii=False)[:3000])
        return
    unsynced = spec.get("synced") is False       # the rows it was rejected over: the record of why
    if args.read or unsynced:
        args.read = args.read or spec["moshaf"]
        rows = mp3quran_rows(args.staging, args.read)
        label = f"MP3Quran ayat_timing read={args.read}"
    else:
        doc = json.loads((prs.OUTPUT / args.set / spec["timingFile"]).read_text(encoding="utf-8"))
        rows = doc["rows"]
        label = doc["source"]
    result = check(args.set, rows, args.staging, label)
    print(json.dumps({args.set: result["summary"]}, ensure_ascii=False, indent=1))
    if not args.report_only and (unsynced or not args.read):
        (prs.OUTPUT / args.set / "audio_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
