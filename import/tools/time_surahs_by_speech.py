#!/usr/bin/env python3
"""Time whole surahs of a set where no timing exists, by local speech recognition in the set's own files.

Used where MP3Quran's aligner rejected a surah and no other timing exists (Al-Tablawi's mujawwad 60 and 63, owner 11 Nov 2026:
"try to time it yourself"). Nothing leaves the PC: faster-whisper runs locally.

    <venv python> -I tools/time_surahs_by_speech.py --set tablawi-mujawwad-hafs-v1 --surahs 60,63 --audio <folder with 060.mp3 ...>

1. Decode the file to 16 kHz mono (miniaudio) and recognize it with word times (faster-whisper, Arabic).
2. Letters only (no vowel marks, one alif, ya and ta marbuta, as tools/check_recitation_speech.py), align the heard words to the
   surah's words in order (dynamic programming; a heard word matches a text word when their letters agree at 0.6 or more, a
   repeat by the reciter is heard words with no partner). The basmalah before ayah 1 is a segment of its own.
3. An ayah starts at its first matched word among its first three; that start moves back to the end of the pause before it
   (10 ms envelope, quiet = floor + 0.3 x (speech - floor), pauses of 80 ms or more), less 70 ms, if that pause ends within
   1.5 s. An ayah ends where the next starts; the last ends 0.3 s after the voice, inside the file.
4. Check: what is heard inside each row against that ayah and its neighbours (the row's own ayah must match best).

Writes content/recitations/<set>/measured_timings.json; tools/prepare_recitation_set.py reads it for the set's fallback surahs.
The MP3s are untrusted downloads: run with -I; this only decodes them.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_recitation_set as prs  # noqa: E402

BASMALA = "بسم الله الرحمن الرحيم"
FOLD = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه", "ء": ""}


def letters(text: str) -> str:
    out = [FOLD.get(c, c) for c in unicodedata.normalize("NFKD", text) if unicodedata.category(c) != "Mn"]
    return re.sub(r"[^ء-ي ]", "", "".join(out))


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0


def surah_words(surah: int) -> list[tuple[int, str]]:
    """(ayah, word) in order; ayah 0 is the basmalah before ayah 1 (not in surah 9; in surah 1 it is ayah 1 itself)."""
    words = []
    for line in (prs.ROOT / "android/app/src/main/assets/content/quran-simple-clean.txt").read_text(encoding="utf-8").splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or not parts[0].isdigit() or int(parts[0]) != surah:
            continue
        ayah, text = int(parts[1]), letters(parts[2])
        if ayah == 1 and surah not in (1, 9) and text.startswith(BASMALA):
            words += [(0, w) for w in BASMALA.split()]
            text = text[len(BASMALA):]
        words += [(ayah, w) for w in text.split()]
    return words


def align(heard: list[dict], text: list[tuple[int, str]]) -> list[int | None]:
    """For each text word, the index of the heard word matched to it (monotonic), or None."""
    n, m = len(heard), len(text)
    gap = -0.4
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    move = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0], move[i][0] = 0.0, 1           # heard words before the text starts cost nothing (isti'adha)
    for j in range(1, m + 1):
        score[0][j], move[0][j] = score[0][j - 1] + gap, 2
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            s = similar(heard[i - 1]["w"], text[j - 1][1])
            best = (score[i - 1][j - 1] + (s if s >= 0.6 else -0.5), 3)
            best = max(best, (score[i - 1][j] - 0.05, 1))   # a heard word with no partner (a repeat, a garble)
            best = max(best, (score[i][j - 1] + gap, 2))    # a text word not heard
            score[i][j], move[i][j] = best
    matched: list[int | None] = [None] * m
    i, j = n, m
    while i > 0 and j > 0:
        if move[i][j] == 3:
            if similar(heard[i - 1]["w"], text[j - 1][1]) >= 0.6:
                matched[j - 1] = i - 1
            i, j = i - 1, j - 1
        elif move[i][j] == 1:
            i -= 1
        else:
            j -= 1
    return matched


def pauses(audio, rate: int) -> list[tuple[int, int]]:
    import numpy as np
    hop = rate // 100
    frames = len(audio) // hop
    rms = np.sqrt(np.mean(audio[: frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    floor, speech = max(np.percentile(db, 5), -75.0), np.percentile(db, 60)
    quiet = db < floor + 0.3 * (speech - floor)
    out, i = [], 0
    while i < frames:
        if quiet[i]:
            j = i
            while j < frames and quiet[j]:
                j += 1
            if j - i >= 8:
                out.append((i * 10, j * 10))
            i = j
        else:
            i += 1
    return out


def decode(path: Path, from_ms: int | None = None, to_ms: int | None = None):
    """16 kHz mono float samples of the file, or of [from_ms, to_ms) only (streamed from that point, for long surahs)."""
    import miniaudio
    import numpy as np
    if from_ms is None:
        decoded = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=16000)
        return np.asarray(decoded.samples, dtype=np.float32)
    want = (to_ms - from_ms) * 16
    chunks, have = [], 0
    for block in miniaudio.stream_file(str(path), output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=16000,
                                       frames_to_read=16000, seek_frame=from_ms * 16):
        chunks.append(np.asarray(block, dtype=np.float32))
        have += len(chunks[-1])
        if have >= want:
            break
    return np.concatenate(chunks)[:want]


def time_surah(model, path: Path, surah: int, counts: list[int], window: tuple[int, int, int, int] | None = None) -> dict:
    """Times the whole surah, or with `window` = (first ayah, last ayah, from ms, to ms) only those ayat inside that stretch."""
    import numpy as np
    first_ayah, last_ayah, base = (window[0], window[1], window[2]) if window else (1, counts[surah - 1], 0)
    audio = decode(path, window[2], window[3]) if window else decode(path)
    duration_ms = round(len(audio) / 16)
    # Long files are recognized in pieces of about 10 minutes, each cut in the middle of a pause, so memory stays small and no word is split.
    quiet_all = pauses(audio, 16000)
    cuts, at = [0], 0
    while len(audio) / 16 - at > 900_000:
        target = at + 600_000
        pause = min((p for p in quiet_all if at + 300_000 < (p[0] + p[1]) / 2 < at + 900_000), key=lambda p: abs((p[0] + p[1]) / 2 - target), default=None)
        at = round((pause[0] + pause[1]) / 2) if pause else target
        cuts.append(at)
    cuts.append(round(len(audio) / 16))
    heard = []
    for begin, end in zip(cuts, cuts[1:]):
        segments, _ = model.transcribe(audio[begin * 16:end * 16], language="ar", beam_size=5, word_timestamps=True, vad_filter=False,
                                       condition_on_previous_text=False)
        for seg in segments:
            for w in seg.words or []:
                text = letters(w.word).strip()
                if text:
                    heard += [{"w": part, "s": begin + round(w.start * 1000), "e": begin + round(w.end * 1000)} for part in text.split()]
    text = [(a, w) for a, w in surah_words(surah) if first_ayah <= a <= last_ayah]
    matched = align(heard, text)
    quiet = pauses(audio, 16000)
    rows, notes = [], []
    for ayah in range(first_ayah, last_ayah + 1):
        idx = [k for k, (a, _) in enumerate(text) if a == ayah]
        first = next((matched[k] for k in idx[:3] if matched[k] is not None), None)
        if first is None:
            notes.append(f"{surah}:{ayah}: none of its first three words was heard; started after the previous ayah's last matched word")
            prev = [matched[k] for k, (a, _) in enumerate(text) if a == ayah - 1 and matched[k] is not None]
            start = heard[prev[-1]]["e"] if prev else (rows[-1]["startMs"] + 1000 if rows else 0)
        else:
            start = heard[first]["s"]
        before = [p for p in quiet if p[1] <= start + 200 and start - p[1] <= 1500]
        snapped = max(before[-1][1] - 70, 0) if before else start
        rows.append({"surah": surah, "ayah": ayah, "startMs": snapped, "heardStartMs": start,
                     "snappedToPause": bool(before), "matchedWords": sum(1 for k in idx if matched[k] is not None), "words": len(idx)})
    for a, b in zip(rows, rows[1:]):
        if b["startMs"] <= a["startMs"]:
            notes.append(f"{surah}:{b['ayah']} did not start after {surah}:{a['ayah']}")
        a["endMs"] = b["startMs"]
    voice = [p for p in quiet if p[0] > rows[-1]["startMs"]]
    voice_end = voice[-1][0] if voice and voice[-1][1] >= duration_ms - 20 else duration_ms
    rows[-1]["endMs"] = min(voice_end + 300, duration_ms)
    for r in rows:    # a window's times are within it: back to the file's time
        for key in ("startMs", "endMs", "heardStartMs"):
            r[key] += base
    for h in heard:
        h["s"] += base
        h["e"] += base
    # The check: what is heard inside each row, against that ayah and its neighbours.
    own = {a: " ".join(w for x, w in surah_words(surah) if x == a) for a in range(0, counts[surah - 1] + 1)}
    checks = []
    for r in rows:
        inside = " ".join(h["w"] for h in heard if r["startMs"] <= h["s"] < r["endMs"])
        scores = {a: round(similar(inside, own[a]), 3) for a in (r["ayah"] - 1, r["ayah"], r["ayah"] + 1) if a in own and a > 0}
        checks.append({"ayah": f"{surah}:{r['ayah']}", "heard": inside, "scores": scores,
                       "ownBest": max(scores, key=scores.get) == r["ayah"]})
    return {"surah": surah, "window": {"ayat": [first_ayah, last_ayah], "fromMs": window[2], "toMs": window[3]} if window else None,
            "durationMs": duration_ms, "heardWords": len(heard), "textWords": len(text),
            "matchedTextWords": sum(1 for x in matched if x is not None), "rows": rows, "notes": notes, "check": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", required=True, choices=sorted(prs.SETS))
    parser.add_argument("--surahs", default="", help="whole surahs, e.g. 60,63")
    parser.add_argument("--window", action="append", default=[],
                        help="only some ayat of a surah, inside a stretch of its file: SURAH:FIRST-LAST@FROMMS-TOMS (repeatable)")
    parser.add_argument("--audio", type=Path, required=True, help="folder with the set's NNN.mp3 files")
    parser.add_argument("--model", default="small")
    parser.add_argument("--out", default="measured_timings.json", help="file name in content/recitations/<set>/ (a second model's pass goes beside the first)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=6, num_workers=1)
    counts = prs.ayah_counts(prs.SETS[args.set])
    jobs = [(int(x), None) for x in args.surahs.split(",") if x]
    for w in args.window:
        head, span = w.split("@")
        surah, ayat = head.split(":")
        jobs.append((int(surah), (*map(int, ayat.split("-")), *map(int, span.split("-")))))
    results = []
    for surah, window in jobs:
        path = args.audio / f"{surah:03}.mp3"
        result = time_surah(model, path, surah, counts, window)
        result["file"] = {"name": path.name, "size": path.stat().st_size, "sha256": prs._sha256(path)}
        results.append(result)
        bad = [c["ayah"] for c in result["check"] if not c["ownBest"]]
        print(f"surah {surah}: {result['matchedTextWords']}/{result['textWords']} text words heard, "
              f"rows {len(result['rows'])}, own ayah best in {len(result['check']) - len(bad)}/{len(result['check'])} {bad}, notes {result['notes']}")
    out = prs.OUTPUT / args.set / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    # Adds to what was measured before: a surah or window measured again replaces its earlier result.
    if out.is_file():
        done = {(r["surah"], json.dumps(r.get("window"))) for r in results}
        results = [r for r in json.loads(out.read_text(encoding="utf-8"))["surahs"]
                   if (r["surah"], json.dumps(r.get("window"))) not in done] + results
        results.sort(key=lambda r: (r["surah"], (r.get("window") or {}).get("fromMs", 0)))
    doc = {"set": args.set, "madeAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "method": "tools/time_surahs_by_speech.py: faster-whisper " + args.model + " int8 with word times, aligned to the ayah text "
                     "(letters only), starts moved to the end of the pause before them less 70 ms; ends at the next start. Not heard by anyone yet.",
           "rows": [{k: r[k] for k in ("surah", "ayah", "startMs", "endMs")} for res in results for r in res["rows"]],
           "surahs": results}
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(prs.ROOT)}")


if __name__ == "__main__":
    main()
