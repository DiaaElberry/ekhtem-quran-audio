"""Prepare a further recitation set: its audio (MP3Quran, or the exact URLs QUD timed) and its ayah timings.

Standard library only. Nothing here is used by the apps at runtime; tools/build_recitation_sets.py bundles the output
(see docs/FUTURE_CONTENT_SOURCES.md, sections C.5 and C.6). A set's timings come from QUD v4 for the URLs its catalog lists, or
from MP3Quran's own ayat_timing as a draft (`"qud": None`), which tools/check_recitation_audio.py then checks against the audio.

    python -I tools/prepare_recitation_set.py --download          # fetch audio, QUD assets, MP3Quran API data
    python -I tools/prepare_recitation_set.py                     # build content/recitations/<id>/ from staged files
    python -I tools/prepare_recitation_set.py --check             # rebuild in memory, compare with the committed files

Staged files (untrusted downloads; this script only reads them, never executes them):
    <staging>/<set id>/NNN.mp3                     the 114 MP3s, unmodified
    <staging>/qud-v4.0.0/                          QUD release assets (catalog.json, manifest.json, <reciter>.zip)
    <staging>/mp3quran-api/                        reciters?language=ar and ayat_timing?surah=N&read=M responses
    <staging>/fetch-log/<name>.json                HTTP status, headers and date of every download

The build is deterministic: every number written comes from a staged file or a recorded fetch log.
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import json
import re
import statistics
import sys
import threading
import time
import unicodedata
import urllib.request
import zipfile
from array import array
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STAGING = Path(r"C:\Users\aldia\Mobile APPs\Ekhtem-AlQuran-Releases\content-staging")
OUTPUT = ROOT / "content" / "recitations"
USER_AGENT = "Mozilla/5.0 (compatible; content-preparation)"

QUD_VERSION = "v4.0.0"
QUD_RELEASE = f"https://github.com/QUD-Technologies/quranic-universal-audio/releases/download/{QUD_VERSION}/"
MP3QURAN_API = "https://www.mp3quran.net/api/v3/"
QUL_API = "https://qul.tarteel.ai/api/v1/audio/"
RECITERS_URL = "https://mp3quran.net/api/v3/reciters?language=ar"
# MP3Quran's timing releases (its v4 API, https://docs-staging.mp3quran.net/): per recitation, ayah and word timings made for
# exact files, each surah naming the sha256 and size of the MP3 it was aligned to. Recitation ids are v4 ids, not moshaf ids.
TIMING_RELEASE = "2026.10.8"
TIMING_RELEASE_BASE = f"https://cdn.mp3quran.net/timings/{TIMING_RELEASE}/"
TIMING_RELEASE_API = f"https://api-staging.mp3quran.net/v4/timing-releases/{TIMING_RELEASE}"
RELEASE_BASE = "https://github.com/DiaaElberry/ekhtem-quran-audio/releases/download/"
# The Cloudflare copy of every release (docs/STREAMING_PLAN.md): a manifest's `sources` try it first, then the release itself.
# A new set must also be copied there (tools/audio_mirror.py, the audio repo's mirror-to-r2 workflow) before it ships.
MIRROR_BASE = "https://audio.ekhtemalquran.stream/"
RIWAYA_NAMES = {"hafs": "Hafs an Asim", "warsh": "Warsh an Nafi"}
TARTEEL_TERMS = ("Tarteel's CDN publishes no licence for these files, and QUL lists none (\"We don't have copyright information "
                 "for this resource\"). Audio rights: to be accepted by the owner; see FUTURE_CONTENT_SOURCES.md C.6.")
QURANICAUDIO_TERMS = ("quranicaudio.com's About page: \"Mp3s on this site may be downloaded and used for personal use free of charge\" "
                      "and \"you may not use these files for commercial purposes\". No other licence. Audio rights: to be accepted by "
                      "the owner; see FUTURE_CONTENT_SOURCES.md C.6.")

SETS = {
    "afasy-hafs-v1": {
        "moshaf": 123, "reciterId": 123, "server": "https://cdn.mp3quran.net/audio/mishary-alafasy/r1/",
        "qud": "mishary_rashid_al_afasy_mp3quran", "timingFile": "verse_timestamps_afasy.json",
        "reciterAr": "مِشَارِيّ رَاشِد الْعَفَاسِيّ", "reciterEn": "Mishary Rashid Alafasy",
    },
    "minshawi-murattal-hafs-v1": {
        "moshaf": 112, "reciterId": 112, "server": "https://cdn.mp3quran.net/audio/muhammad-minshawi/r1/",
        "qud": "mohammed_siddiq_al_minshawi_mp3quran", "timingFile": "verse_timestamps_minshawi_murattal.json",
        "reciterAr": "مُحَمَّد صِدِّيق الْمِنْشَاوِيّ", "reciterEn": "Mohamed Siddiq Al-Minshawi",
    },
    "abdulbasit-murattal-hafs-v1": {
        "moshaf": 53, "reciterId": 51, "server": "https://audio-cdn.tarteel.ai/quran/surah/abdulBasit/murattal/mp3/",
        "qud": "abdulbasit_abdulsamad_tarteel", "timingFile": "verse_timestamps_abdulbasit_murattal.json",
        "reciterAr": "عَبْد الْبَاسِط عَبْد الصَّمَد (مُرَتَّل)", "reciterEn": "Abdul Basit Abdul Samad (Murattal)",
        "host": "Tarteel", "terms": TARTEEL_TERMS,
    },
    "abdulbasit-mujawwad-hafs-v1": {
        "moshaf": 51, "reciterId": 51, "server": "https://audio-cdn.tarteel.ai/quran/surah/abdulBasit/mujawwad/mp3/",
        "qud": "abdulbasit_abdulsamad_mujawwad_tarteel", "timingFile": "verse_timestamps_abdulbasit_mujawwad.json",
        "reciterAr": "عَبْد الْبَاسِط عَبْد الصَّمَد (مُجَوَّد)", "reciterEn": "Abdul Basit Abdul Samad (Mujawwad)", "style": "mujawwad",
        "host": "Tarteel", "terms": TARTEEL_TERMS,
    },
    "abdulbasit-warsh-v1": {
        "moshaf": 52, "reciterId": 51, "server": "https://download.quranicaudio.com/quran/abdulbaset_warsh/",
        "qud": "abdulbasit_abdulsamad_warsh_qdc", "timingFile": "verse_timestamps_abdulbasit_warsh.json",
        "reciterAr": "عَبْد الْبَاسِط عَبْد الصَّمَد", "reciterEn": "Abdul Basit Abdul Samad", "riwayah": "warsh",
        "host": "QuranicAudio", "terms": QURANICAUDIO_TERMS,
    },
    "huthaifi-hafs-v1": {
        "moshaf": 74, "reciterId": 74, "server": "https://cdn.mp3quran.net/audio/ali-hudhaifi/r1/",
        "qud": "ali_al_huthaifi_mp3quran", "timingFile": "verse_timestamps_huthaifi.json",
        "reciterAr": "عَلِيّ بْن عَبْد الرَّحْمَٰن الْحُذَيْفِيّ", "reciterEn": "Ali Al-Huthaifi",
    },
    "juhani-hafs-v1": {
        "moshaf": 62, "reciterId": 62, "server": "https://cdn.mp3quran.net/audio/abdullah-juhani/r1/",
        "qud": None, "qul": 162, "timingFile": "verse_timestamps_juhani.json",
        # Measured by cross-correlating this file with quranicaudio's 002.mp3, which the timings fit (FUTURE_CONTENT_SOURCES.md C.6).
        "shifts": [{"surah": 2, "fromAyah": 177, "ms": -9660,
                    "reason": "this copy of 002.mp3 is 9.66 s shorter from the boundary 2:176/2:177 on (2,987.4 s): the same recording "
                              "without about 4.5 s of silence and 5 s of voice that quranicaudio's copy has there"}],
        "reciterAr": "عَبْد اللَّه عَوَّاد الْجُهَنِيّ", "reciterEn": "Abdullah Awad Al-Juhani",
    },
    "hassan-saleh-hafs-v1": {
        "moshaf": 299, "reciterId": 286, "server": "https://cdn.mp3quran.net/audio/hassan-saleh/r1/",
        "qud": None, "timingFile": None, "synced": False,
        "timingRejected": "MP3Quran's ayat_timing read=299 is the only timing found, and it is not bundled: nonsense in 20 surahs whose files have one "
                          "channel inverted (23, 24, 42-47, 55-66), poor in many others. See audio_check.json and FUTURE_CONTENT_SOURCES.md C.6.",
        "reciterAr": "حَسَن صَالِح", "reciterEn": "Hassan Saleh",
    },
    # Another recording, with clean channels: the archive.org item mp3quran17 (tags: mp3quran.net), its audio frames the same as Assabile's and those tagged Islamway. The timings come
    # from a third party's ayah clips cut from these very files, located by waveform (check_recitation_audio.py --locate-ayah-clips).
    "hassan-saleh-hafs-v2": {
        "moshaf": 299, "reciterId": 286, "server": "https://archive.org/download/mp3quran17/",
        "qud": None, "ayahClips": "Hassan_Saleh_Hafs", "timingFile": "verse_timestamps_hassan_saleh.json",
        # Where the clips themselves are cut wrong, found by local speech recognition of every surah's opening: the start is the end of the pause in this
        # file's envelope, less the 70 ms by which the voice follows a start in this set (audio_check.json), and the ayah before now ends there.
        "corrections": [
            {"surah": 7, "ayah": 1, "startMs": 3990, "reason": "the clip of the basmalah also holds most of الٓمٓصٓ; the letters start after the pause that ends at 4,060 ms"},
            {"surah": 18, "ayah": 1, "startMs": 630, "reason": "this file has no basmalah, yet the clips cut a 1.3 s \"basmalah\" off the start of 18:1; the voice starts at 700 ms"},
            {"surah": 41, "ayah": 1, "startMs": 4440, "reason": "the clip of the basmalah also holds 41:1 and 41:2; حمٓ starts after the pause that ends at 4,510 ms"},
            {"surah": 41, "ayah": 2, "startMs": 7390, "reason": "see 41:1; the ayah starts after the pause that ends at 7,460 ms"},
            {"surah": 41, "ayah": 3, "startMs": 14370, "reason": "the clips of 41:1 and 41:2 are the start of 41:3; the ayah starts after the pause that ends at 14,440 ms"},
            {"surah": 49, "ayah": 1, "startMs": 4900, "reason": "the clip starts 0.5 s late, after يا; the voice resumes when the pause ends at 4,970 ms"},
        ],
        # From the owner's ear review: last ayat that ran on past the voice, and ayat that held the first reading of the next ayah. Each one's old value, new value, reason and evidence are in the file.
        "timingCorrections": "timing_corrections.json",
        "reciterAr": "حَسَن صَالِح", "reciterEn": "Hassan Saleh",
        "host": "Internet Archive, item mp3quran17 (uploader mp3qurandotnet@gmail.com)",
        "terms": "The archive.org item mp3quran17 states CC BY-ND 3.0 (licenseurl http://creativecommons.org/licenses/by-nd/3.0/). This is the uploader's statement, "
                 "not a licence from the reciter or recording producer. The files' tags name www.Islamway.com, 2009, and \"حقوق النشر محفوظة لكل مسلم\". The files stay unmodified.",
    },
    # 4.0.8 (owner, 11 Nov 2026): thirteen MP3Quran sets timed by MP3Quran's timing release (timingRelease = its v4 recitation id).
    "husary-mujawwad-hafs-v1": {
        "moshaf": 119, "reciterId": 118, "server": "https://cdn.mp3quran.net/audio/mahmoud-husary/r2/", "qud": None, "timingRelease": 114,
        "timingFile": "verse_timestamps_husary_mujawwad.json", "style": "mujawwad", "timingCorrections": "timing_corrections.json",
        "reciterAr": "مَحْمُود خَلِيل الْحُصَرِيّ (مُجَوَّد)", "reciterEn": "Mahmoud Khalil Al-Husary (Mujawwad)",
    },
    "mustafa-ismail-hafs-v1": {
        "moshaf": 125, "reciterId": 125, "server": "https://cdn.mp3quran.net/audio/mustafa-ismail/r1/", "qud": None, "timingRelease": 120,
        "timingFile": "verse_timestamps_mustafa_ismail.json",
        "reciterAr": "مُصْطَفَى إِسْمَاعِيل (مُرَتَّل)", "reciterEn": "Mustafa Ismail (Murattal)",
    },
    "banna-hafs-v1": {
        "moshaf": 121, "reciterId": 121, "server": "https://cdn.mp3quran.net/audio/mahmoud-banna/r1/", "qud": None, "timingRelease": 116,
        "timingFile": "verse_timestamps_banna.json",
        "reciterAr": "مَحْمُود عَلِيّ الْبَنَّا (مُرَتَّل)", "reciterEn": "Mahmoud Ali Al-Banna (Murattal)",
    },
    "tablawi-hafs-v1": {
        "moshaf": 106, "reciterId": 106, "server": "https://cdn.mp3quran.net/audio/muhammad-tablawi/r1/", "qud": None, "timingRelease": 101,
        "timingFile": "verse_timestamps_tablawi.json",
        "reciterAr": "مُحَمَّد مَحْمُود الطَّبْلَاوِيّ (مُرَتَّل)", "reciterEn": "Mohamed Mahmoud Al-Tablawi (Murattal)",
    },
    "ayyub-hafs-v1": {
        "moshaf": 109, "reciterId": 109, "server": "https://cdn.mp3quran.net/audio/muhammad-ayyub/r1/", "qud": None, "timingRelease": 104,
        "timingFile": "verse_timestamps_ayyub.json",
        "reciterAr": "مُحَمَّد أَيُّوب", "reciterEn": "Muhammad Ayyub",
    },
    "akhdar-hafs-v1": {
        "moshaf": 1, "reciterId": 1, "server": "https://cdn.mp3quran.net/audio/ibrahim-akhdar/r1/", "qud": None, "timingRelease": 1,
        "timingFile": "verse_timestamps_akhdar.json",
        "reciterAr": "إِبْرَاهِيم الْأَخْضَر", "reciterEn": "Ibrahim Al-Akhdar",
    },
    "sudais-hafs-v1": {
        "moshaf": 54, "reciterId": 54, "server": "https://cdn.mp3quran.net/audio/abdulrahman-sudais/r1/", "qud": None, "timingRelease": 52,
        "timingFile": "verse_timestamps_sudais.json",
        "reciterAr": "عَبْد الرَّحْمَٰن السُّدَيْس", "reciterEn": "Abdulrahman Al-Sudais",
    },
    "shuraim-hafs-v1": {
        "moshaf": 31, "reciterId": 31, "server": "https://cdn.mp3quran.net/audio/saud-shuraim/r1/", "qud": None, "timingRelease": 30,
        "timingFile": "verse_timestamps_shuraim.json",
        "reciterAr": "سُعُود الشُّرَيْم", "reciterEn": "Saud Al-Shuraim",
    },
    "muaiqly-murattal-hafs-v1": {
        "moshaf": 102, "reciterId": 102, "server": "https://cdn.mp3quran.net/audio/maher-muaiqly/r1/", "qud": None, "timingRelease": 97,
        "timingFile": "verse_timestamps_muaiqly_murattal.json",
        "reciterAr": "مَاهِر الْمُعَيْقِلِيّ (مُرَتَّل)", "reciterEn": "Maher Al-Muaiqly (Murattal)",
    },
    "muaiqly-mujawwad-hafs-v1": {
        "moshaf": 133, "reciterId": 102, "server": "https://cdn.mp3quran.net/audio/maher-muaiqly/r3/", "qud": None, "timingRelease": 125,
        "timingFile": "verse_timestamps_muaiqly_mujawwad.json", "style": "mujawwad",
        "reciterAr": "مَاهِر الْمُعَيْقِلِيّ (مُجَوَّد)", "reciterEn": "Maher Al-Muaiqly (Mujawwad)",
    },
    "ghamdi-hafs-v1": {
        "moshaf": 30, "reciterId": 30, "server": "https://cdn.mp3quran.net/audio/saad-ghamdi/r1/", "qud": None, "timingRelease": 29,
        "timingFile": "verse_timestamps_ghamdi.json",
        "reciterAr": "سَعْد الْغَامِدِيّ", "reciterEn": "Saad Al-Ghamdi",
    },
    "abbad-hafs-v1": {
        "moshaf": 81, "reciterId": 81, "server": "https://cdn.mp3quran.net/audio/fares-abbad/r1/", "qud": None, "timingRelease": 78,
        "timingFile": "verse_timestamps_abbad.json",
        "reciterAr": "فَارِس عَبَّاد", "reciterEn": "Fares Abbad",
    },
    # The three mujawwad sets (owner, 11 Nov 2026: "murattal and mujawwad"). MP3Quran's aligner rejected some surahs of two of them
    # (Mustafa Ismail's 6, Al-Tablawi's 60 and 63): measured in these files by local speech recognition (measured_timings.json,
    # tools/time_surahs_by_speech.py).
    "mustafa-ismail-mujawwad-hafs-v1": {
        "moshaf": 288, "reciterId": 125, "server": "https://cdn.mp3quran.net/audio/mustafa-ismail/r2/", "qud": None, "timingRelease": 239,
        # MP3Quran's older ayat_timing for these 6 was made for another edit of the recording (3.8 to 6.7 s off these files'
        # pauses, audio_check.json of the first GitHub prepare run), so they are measured here too.
        "releaseRejected": [10, 43, 57, 60, 63, 66], "fallback": "measured", "timingCorrections": "timing_corrections.json",
        "timingFile": "verse_timestamps_mustafa_ismail_mujawwad.json", "style": "mujawwad",
        "reciterAr": "مُصْطَفَى إِسْمَاعِيل (مُجَوَّد)", "reciterEn": "Mustafa Ismail (Mujawwad)",
    },
    "banna-mujawwad-hafs-v1": {
        "moshaf": 122, "reciterId": 121, "server": "https://cdn.mp3quran.net/audio/mahmoud-banna/r2/", "qud": None, "timingRelease": 117,
        "timingFile": "verse_timestamps_banna_mujawwad.json", "style": "mujawwad",
        "reciterAr": "مَحْمُود عَلِيّ الْبَنَّا (مُجَوَّد)", "reciterEn": "Mahmoud Ali Al-Banna (Mujawwad)",
    },
    "tablawi-mujawwad-hafs-v1": {
        "moshaf": 10912, "reciterId": 106, "server": "https://cdn.mp3quran.net/audio/muhammad-tablawi/r2/", "qud": None, "timingRelease": 301,
        "releaseRejected": [60, 63], "fallback": "measured", "timingCorrections": "timing_corrections.json",
        "timingFile": "verse_timestamps_tablawi_mujawwad.json", "style": "mujawwad",
        "reciterAr": "مُحَمَّد مَحْمُود الطَّبْلَاوِيّ (مُجَوَّد)", "reciterEn": "Mohamed Mahmoud Al-Tablawi (Mujawwad)",
    },
    "yasser-dosari-hafs-v1": {
        "moshaf": 92, "reciterId": 92, "server": "https://cdn.mp3quran.net/audio/yasser-dosari/r1/", "qud": None, "timingRelease": 89,
        "timingFile": "verse_timestamps_yasser_dosari.json",
        "reciterAr": "يَاسِر الدَّوْسَرِيّ", "reciterEn": "Yasser Al-Dosari",
    },
}


# --------------------------------------------------------------------------------------------- download

def _fetch(url: str, target: Path, log: dict, expected_size: int | None = None) -> None:
    """Download url to target via a .part file (an interrupted .part resumes with HTTP Range); log status, final URL, headers."""
    part = target.with_name(target.name + ".part")
    for attempt in range(6):
        try:
            have = part.stat().st_size if part.is_file() else 0
            headers = {"User-Agent": USER_AGENT}
            if have:
                headers["Range"] = f"bytes={have}-"
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response:
                resumed = bool(have) and response.status == 206
                with open(part, "ab" if resumed else "wb") as out:
                    while chunk := response.read(1 << 20):
                        out.write(chunk)
                kept = {k: v for k, v in response.headers.items()
                        if k.lower() in ("content-length", "content-type", "last-modified", "etag", "date")}
                status, final = response.status, response.geturl()
            size = part.stat().st_size
            if "Content-Length" in kept and int(kept["Content-Length"]) + (have if resumed else 0) != size:
                raise OSError(f"{url}: got {size} bytes, Content-Length {kept['Content-Length']}")
            if resumed:
                kept["Content-Length"] = str(size)
                status = 200
            if expected_size is not None and size != expected_size:
                raise OSError(f"{url}: got {size} bytes, expected {expected_size}")
            part.replace(target)
            log[url] = {"status": status, "finalUrl": final, "headers": kept, "bytes": size,
                        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            return
        except OSError as error:
            if attempt == 5:
                raise
            print(f"retry {attempt + 1} {url}: {error}", file=sys.stderr, flush=True)
            time.sleep(5 * (attempt + 1))


def _write_log(staging: Path, name: str, log: dict) -> None:
    folder = staging / "fetch-log"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    old.update(log)
    path.write_text(json.dumps(dict(sorted(old.items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def download(staging: Path, set_ids: list[str]) -> None:
    """Fetch what is not staged yet. Staged files are never replaced: earlier sets' provenance names their hashes."""
    api = staging / "mp3quran-api"
    qud = staging / f"qud-{QUD_VERSION}"
    api.mkdir(parents=True, exist_ok=True)
    qud.mkdir(parents=True, exist_ok=True)
    log: dict = {}
    if not (api / "reciters_ar.json").is_file():
        _fetch(RECITERS_URL, api / "reciters_ar.json", log)
    extra = ["warsh_words.json.gz"] if any(SETS[s].get("riwayah") == "warsh" for s in set_ids) else []
    for name in ["catalog.json", "manifest.json"] + extra + [SETS[s]["qud"] + ".zip" for s in set_ids if SETS[s].get("qud")]:
        if not (qud / name).is_file():
            _fetch(QUD_RELEASE + name, qud / name, log)
    _write_log(staging, "catalogues", log)
    if any(SETS[s].get("timingRelease") for s in set_ids):
        log = {}
        folder = staging / "mp3quran-timings" / TIMING_RELEASE
        folder.mkdir(parents=True, exist_ok=True)
        wanted = [(TIMING_RELEASE_API, folder / "release.json"), (TIMING_RELEASE_BASE + "manifest.json", folder / "manifest.json")]
        wanted += [(f"{TIMING_RELEASE_BASE}recitations/{SETS[s]['timingRelease']}.json", folder / f"{SETS[s]['timingRelease']}.json")
                   for s in set_ids if SETS[s].get("timingRelease")]
        for url, target in wanted:
            if not target.is_file():
                _fetch(url, target, log)
        _write_log(staging, f"mp3quran-timings-{TIMING_RELEASE}", log)
    for set_id in set_ids:
        spec = SETS[set_id]
        log = {}
        folder = api / f"ayat_timing-read{spec['moshaf']}"
        folder.mkdir(exist_ok=True)
        for surah in range(1, 115):
            if not (folder / f"{surah:03}.json").is_file():
                _fetch(f"{MP3QURAN_API}ayat_timing?surah={surah}&read={spec['moshaf']}", folder / f"{surah:03}.json", log)
        _write_log(staging, f"ayat_timing-read{spec['moshaf']}", log)
        if spec.get("qul"):
            log = {}
            folder = staging / "qul-api" / f"surah_segments-{spec['qul']}"
            folder.mkdir(parents=True, exist_ok=True)
            for surah in range(1, 115):
                if not (folder / f"{surah:03}.json").is_file():
                    _fetch(f"{QUL_API}surah_segments/{spec['qul']}?surah={surah}&per_page=500", folder / f"{surah:03}.json", log)
            _write_log(staging, f"qul-surah_segments-{spec['qul']}", log)
        audio = staging / set_id
        audio.mkdir(exist_ok=True)
        lock = threading.Lock()

        def one(surah: int) -> None:
            target = audio / f"{surah:03}.mp3"
            url = spec["server"] + target.name
            if target.is_file():
                # Already staged: record the server's current headers without fetching the body again.
                request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=60) as response:
                    length = int(response.headers["Content-Length"])
                if length == target.stat().st_size:
                    return
            one_log: dict = {}
            _fetch(url, target, one_log)
            with lock:
                _write_log(staging, set_id, one_log)  # after every file, so an interrupted run keeps its log
            print(f"{set_id} {target.name} {target.stat().st_size}", flush=True)

        with ThreadPoolExecutor(3) as pool:
            list(pool.map(one, range(1, 115)))
        print(f"{set_id} DONE", flush=True)


# ------------------------------------------------------------------------------------------ MP3 frames

MP3_BITRATES = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
                2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
MP3_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
CHANNEL_MODES = ["stereo", "joint stereo", "dual channel", "mono"]


def _frame(data: bytes, i: int, end: int):
    """(version, bitrate kbps, sample rate, frame length, channel mode) of an MPEG Layer III header at i, or None."""
    if i + 4 > end or data[i] != 0xFF or data[i + 1] & 0xE0 != 0xE0:
        return None
    b1, b2 = data[i + 1], data[i + 2]
    version, layer, bitrate_index, rate_index = (b1 >> 3) & 3, (b1 >> 1) & 3, b2 >> 4, (b2 >> 2) & 3
    if version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    bitrate = MP3_BITRATES[1 if version == 3 else 2][bitrate_index]
    rate = MP3_RATES[version][rate_index]
    length = (144 if version == 3 else 72) * bitrate * 1000 // rate + ((b2 >> 1) & 1)
    return version, bitrate, rate, length, data[i + 3] >> 6


def parse_mp3(path: Path, activity: array | None = None) -> dict:
    """Walk every MPEG frame of an MP3 (read-only). Duration = audio frames x samples per frame / sample rate.

    If `activity` is given, it receives one value per granule (576 samples, MPEG-1 Layer III): the summed
    global_gain of its channels (the quantizer gain in the side information). It drops in pauses, so it is a
    coarse loudness envelope that needs no decoder.
    """
    data = path.read_bytes()
    size = len(data)
    pos = id3v2 = 0
    while data[pos:pos + 3] == b"ID3":
        tag = 10 + ((data[pos + 6] << 21) | (data[pos + 7] << 14) | (data[pos + 8] << 7) | data[pos + 9])
        tag += 10 if data[pos + 5] & 0x10 else 0
        pos += tag
        id3v2 += tag
    end = size
    id3v1 = size >= 128 and data[size - 128:size - 125] == b"TAG"
    if id3v1:
        end -= 128
    ape = 0
    if data[end - 32:end - 24] == b"APETAGEX":
        ape = int.from_bytes(data[end - 20:end - 16], "little")
        ape += 32 if int.from_bytes(data[end - 12:end - 8], "little") & 0x80000000 else 0
        end -= ape
    frames = junk = 0
    bitrates: dict[int, int] = {}
    rates: set[int] = set()
    modes: set[str] = set()
    versions: set[int] = set()
    xing = None
    truncated = 0
    i = pos
    first = True
    synced = False
    while i < end:
        header = _frame(data, i, end)
        if header is not None and not synced and i + header[3] + 4 <= end:
            # Out of sync (start of audio, or after non-frame bytes): the next frame must confirm the header.
            following = _frame(data, i + header[3], end)
            if following is None or following[2] != header[2]:
                header = None
        if header is None:
            i += 1
            junk += 1
            synced = False
            continue
        synced = True
        version, bitrate, rate, length, mode = header
        if i + length > end:
            truncated = end - i
            junk += end - i
            break
        if first:
            first = False
            side = (32 if mode != 3 else 17) if version == 3 else (17 if mode != 3 else 9)
            tag = data[i + 4 + side:i + 8 + side]
            if tag in (b"Xing", b"Info") or data[i + 36:i + 40] == b"VBRI":
                xing = {"tag": tag.decode() if tag in (b"Xing", b"Info") else "VBRI"}
                if tag in (b"Xing", b"Info"):
                    at = i + 4 + side
                    flags = int.from_bytes(data[at + 4:at + 8], "big")
                    field = at + 8
                    if flags & 1:
                        xing["frames"] = int.from_bytes(data[field:field + 4], "big")
                        field += 4
                    if flags & 2:
                        xing["bytes"] = int.from_bytes(data[field:field + 4], "big")
                        field += 4
                    field += 100 if flags & 4 else 0
                    field += 4 if flags & 8 else 0
                    encoder = data[field:field + 9]
                    if encoder[:4] in (b"LAME", b"Lavc", b"Lavf"):
                        xing["encoder"] = "".join(chr(c) if 32 <= c < 127 else "?" for c in encoder).strip()
                        d = data[field + 21:field + 24]
                        xing["encoderDelay"] = (d[0] << 4) | (d[1] >> 4)
                        xing["encoderPadding"] = ((d[1] & 15) << 8) | d[2]
                i += length
                continue  # the tag frame carries no audio
        frames += 1
        if activity is not None and version == 3:
            start = i + 4 + (0 if data[i + 1] & 1 else 2)
            bits = int.from_bytes(data[start:start + 32], "big")
            # (granule, channel) blocks of 59 bits; global_gain is 8 bits at +21 (after part2_3_length, big_values).
            if mode == 3:  # mono: 9 + 5 + 4 bits before granule 0
                activity.append(bits >> (256 - 18 - 29) & 0xFF)
                activity.append(bits >> (256 - 18 - 59 - 29) & 0xFF)
            else:  # two channels: 9 + 3 + 8 bits before granule 0
                for granule in (0, 1):
                    at = 20 + granule * 118
                    activity.append((bits >> (256 - at - 29) & 0xFF) + (bits >> (256 - at - 59 - 29) & 0xFF))
        elif activity is not None:  # MPEG-2 (one granule per frame): 8 + 1 or 2 bits, then 63 bits per channel
            start = i + 4 + (0 if data[i + 1] & 1 else 2)
            bits = int.from_bytes(data[start:start + 16], "big")
            if mode == 3:
                activity.append(bits >> (128 - 9 - 29) & 0xFF)
            else:
                activity.append((bits >> (128 - 10 - 29) & 0xFF) + (bits >> (128 - 10 - 63 - 29) & 0xFF))
        bitrates[bitrate] = bitrates.get(bitrate, 0) + 1
        rates.add(rate)
        modes.add(CHANNEL_MODES[mode])
        versions.add(version)
        i += length
    assert len(rates) == 1 and len(versions) == 1, (path, rates, versions)
    rate = rates.pop()
    samples = 1152 if versions.pop() == 3 else 576
    info = {
        "sizeBytes": size, "sha256": hashlib.sha256(data).hexdigest(),
        "durationSeconds": round(frames * samples / rate, 3), "audioFrames": frames,
        "bitrateMode": "CBR" if len(bitrates) == 1 else "VBR",
        "bitrateKbps": next(iter(bitrates)) if len(bitrates) == 1 else round(
            sum(b * n for b, n in bitrates.items()) / frames, 1),
        "sampleRateHz": rate, "channelMode": "/".join(sorted(modes)),
        "id3v2Bytes": id3v2, "id3v1": id3v1, "apeBytes": ape, "nonFrameBytes": junk,
    }
    if truncated:
        info["truncatedLastFrameBytes"] = truncated
    if xing:
        info["infoTag"] = xing
    if len(bitrates) > 1:
        info["bitrateHistogram"] = {str(k): v for k, v in sorted(bitrates.items())}
    return info


# ------------------------------------------------------------------------------------------ timings

def hafs_ayah_counts() -> list[int]:
    meta = json.loads((ROOT / "android/app/src/main/assets/content/quran_meta.json").read_text(encoding="utf-8"))
    counts = [s["ayahs"] for s in sorted(meta["surahs"], key=lambda s: s["n"])]
    assert len(counts) == 114 and sum(counts) == 6236
    return counts


def warsh_ayah_counts() -> list[int]:
    meta = json.loads((ROOT / "android/app/src/main/assets/warsh/quran_meta.json").read_text(encoding="utf-8"))
    counts = [s["ayahs"] for s in sorted(meta["surahs"], key=lambda s: s["n"])]
    assert len(counts) == 114 and sum(counts) == 6214
    return counts


def ayah_counts(spec: dict) -> list[int]:
    return warsh_ayah_counts() if spec.get("riwayah") == "warsh" else hafs_ayah_counts()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile (deterministic, no interpolation)."""
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, -(-len(ordered) * p // 100) - 1))]


def clip_rows(set_id: str, counts: list[int]) -> tuple[list[dict], list[dict], list[str]]:
    """Rows measured in the set's own files from ayah clips (tools/check_recitation_audio.py --locate-ayah-clips writes clip_check.json)."""
    rows = json.loads((OUTPUT / set_id / "clip_check.json").read_text(encoding="utf-8"))["rows"]
    problems = [f"clip_check.json surah {s}: {sum(1 for r in rows if r['surah'] == s)} ayat, the mushaf has {counts[s - 1]}"
                for s in range(1, 115) if sum(1 for r in rows if r["surah"] == s) != counts[s - 1]]
    return rows, [], problems


def load_qud(staging: Path, spec: dict) -> dict:
    folder = staging / f"qud-{QUD_VERSION}"
    archive = folder / f"{spec['qud']}.zip"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["recitations"][spec["qud"]]
    digest = _sha256(archive)
    assert entry["sha256"] == digest and entry["bytes"] == archive.stat().st_size, "QUD zip does not match manifest.json"
    with zipfile.ZipFile(archive) as zf:
        catalog = json.loads(zf.read("catalog.json"))
        specials = json.loads(zf.read("specials.json"))
        verse = json.loads(gzip.decompress(zf.read("verse_timestamps.json.gz")))
        members = {i.filename: i.file_size for i in zf.infolist()}
    urls = {int(k): v for k, v in catalog["audio"]["chapter_urls"].items()}
    assert urls == {s: f"{spec['server']}{s:03}.mp3" for s in range(1, 115)}, "QUD catalog URLs differ from the moshaf"
    release_catalog = json.loads((folder / "catalog.json").read_text(encoding="utf-8"))
    listed = [r for r in release_catalog["recitations"] if r.get("slug") == spec["qud"]]
    return {"archive": archive, "sha256": digest, "entry": entry, "catalog": catalog, "specials": specials,
            "verse": verse, "members": members, "manifest": manifest, "listedInReleaseCatalog": len(listed),
            "manifestSha256": _sha256(folder / "manifest.json"), "releaseCatalogSha256": _sha256(folder / "catalog.json")}


def mp3quran_rows(staging: Path, spec: dict, counts: list[int]) -> tuple[list[dict], list[dict], list[str]]:
    """MP3Quran's own ayat_timing as this set's timings: start and end as published, its ayah 0 (isti'adha, basmalah) dropped."""
    folder = staging / "mp3quran-api" / f"ayat_timing-read{spec['moshaf']}"
    rows, problems = [], []
    for surah in range(1, 115):
        entries = json.loads((folder / f"{surah:03}.json").read_text(encoding="utf-8"))
        real = [e for e in entries if e["ayah"]]
        if [e["ayah"] for e in real] != list(range(1, counts[surah - 1] + 1)):
            problems.append(f"MP3Quran read={spec['moshaf']} surah {surah}: {len(real)} ayat, the mushaf has {counts[surah - 1]}")
        rows += [{"surah": surah, "ayah": e["ayah"], "startMs": e["start_time"], "endMs": e["end_time"]} for e in real]
    return rows, [], problems


def load_timing_release(staging: Path, spec: dict) -> dict:
    """The set's file of MP3Quran's timing release, with the release record and the release manifest's status for its surahs."""
    folder = staging / "mp3quran-timings" / TIMING_RELEASE
    path = folder / f"{spec['timingRelease']}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["release"] == TIMING_RELEASE and doc["recitation_id"] == spec["timingRelease"], (doc["release"], doc["recitation_id"])
    status = [f for f in json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["files"] if f["recitation_id"] == spec["timingRelease"]]
    return {"path": path, "doc": doc, "sha256": _sha256(path), "bytes": path.stat().st_size,
            "release": json.loads((folder / "release.json").read_text(encoding="utf-8"))["data"],
            "manifestStatus": {"accepted": sum(1 for f in status if f["status"] == "accepted"), "files": len(status),
                               "notAccepted": [f"{f['surah']}: {f['status']} {f['gate_reason']}" for f in status if f["status"] != "accepted"]},
            "manifestSha256": _sha256(folder / "manifest.json")}


def release_rows(timing: dict, counts: list[int], skip: frozenset[int] = frozenset(), patches: dict | None = None,
                 measured: list[dict] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """One row per ayah from the release's ayah_timings ([ayah, start, end], ayah 0 = the isti'adha or basmalah, dropped).

    A reciter who reads an ayah again gives it a second segment. The same ayah again: its range runs from the first start to the last
    end, as QUD's repeats are merged. Back to an earlier ayah (he re-reads ayat before going on): the ayah lit before the repeat keeps
    its highlight through it, as accepted for Hassan Saleh's 55:45 (FUTURE_CONTENT_SOURCES.md C.6), so no range runs backwards."""
    rows, repeats, problems = [], [], []
    for surah in range(1, 115):
        if surah in skip:    # rejected by MP3Quran's aligner: timed from the set's fallback (fallback_rows)
            if str(surah) in timing["doc"]["surahs"]:
                problems.append(f"timing release surah {surah}: listed as rejected, but the release times it")
            continue
        timed = timing["doc"]["surahs"][str(surah)]["ayah_timings"]
        segments = sorted((a for a in timed["ayahs"] if a[0]), key=lambda a: (a[1], a[2]))
        for lo, hi in (patches or {}).get(surah, []):
            # The measured rows of ayat lo..hi replace every release segment of those ayat, and any segment lying inside them.
            rows_in = [r for r in measured if r["surah"] == surah and lo <= r["ayah"] <= hi]
            if [r["ayah"] for r in rows_in] != list(range(lo, hi + 1)):
                problems.append(f"patch {surah}:{lo}-{hi}: measured_timings.json has {[r['ayah'] for r in rows_in]}")
                continue
            begin, finish = rows_in[0]["startMs"], rows_in[-1]["endMs"]
            segments = [a for a in segments if not (lo <= a[0] <= hi or begin <= a[1] < finish)]
            segments = sorted(segments + [[r["ayah"], r["startMs"], r["endMs"]] for r in rows_in], key=lambda a: (a[1], a[2]))
        if not timed["complete"] and not (patches or {}).get(surah):
            problems.append(f"timing release surah {surah}: not complete")
        by_ayah: dict[int, dict] = {}
        last = None
        for ayah, start, end in segments:
            if ayah not in by_ayah:
                if ayah != (last["ayah"] + 1 if last else 1):
                    problems.append(f"timing release {surah}:{ayah} starts after {surah}:{last['ayah'] if last else 0}, out of order")
                last = by_ayah[ayah] = {"surah": surah, "ayah": ayah, "startMs": start, "endMs": end}
                continue
            kind = "the same ayah again" if ayah == last["ayah"] else "back to an earlier ayah"
            repeats.append({"ayah": f"{surah}:{ayah}", "kind": kind, "segmentMs": [start, end], "litAyah": f"{surah}:{last['ayah']}",
                            "litAyahEndBeforeMs": last["endMs"]})
            last["endMs"] = max(last["endMs"], end)
        if sorted(by_ayah) != list(range(1, counts[surah - 1] + 1)):
            problems.append(f"timing release surah {surah}: {len(by_ayah)} ayat, the mushaf has {counts[surah - 1]}")
        rows += [by_ayah[a] for a in sorted(by_ayah)]
    index = {(r["surah"], r["ayah"]): r for r in rows}
    for repeat in repeats:
        surah, ayah = map(int, repeat["litAyah"].split(":"))
        lit, after = index[surah, ayah], index.get((surah, ayah + 1))
        repeat.update(mergedStartMs=lit["startMs"], mergedEndMs=lit["endMs"], nextAyahStartMs=after["startMs"] if after else None,
                      overlapsNeighbour=bool(after and lit["endMs"] > after["startMs"]))
    return rows, repeats, problems


FALLBACK_NOTES = {
    "mp3quran": lambda spec: (f"MP3Quran's older ayat_timing for the same moshaf ({MP3QURAN_API}ayat_timing?surah=N&read={spec['moshaf']}), start and end "
                              "as published, its ayah 0 dropped; a draft checked against the decoded audio like the rest (audio_check.json)"),
    "measured": lambda spec: ("measured in these files: local speech recognition (faster-whisper, nothing leaves the PC) with word times, matched "
                              "to the ayah text, each start moved to the end of the pause before it (measured_timings.json, "
                              "tools/time_surahs_by_speech.py); not heard by anyone yet"),
}


def measured_patches(set_id: str) -> tuple[dict[int, list[list[int]]], list[dict]]:
    """The windows of the set's measured_timings.json (ayat measured inside the release's surahs): {surah: [[lo, hi], ...]}, and all its rows."""
    path = OUTPUT / set_id / "measured_timings.json"
    if not path.is_file():
        return {}, []
    doc = json.loads(path.read_text(encoding="utf-8"))
    patches: dict[int, list[list[int]]] = {}
    for entry in doc["surahs"]:
        if entry.get("window"):
            patches.setdefault(entry["surah"], []).append(entry["window"]["ayat"])
    return patches, doc["rows"]


def fallback_rows(staging: Path, set_id: str, spec: dict, counts: list[int], surahs: frozenset[int]) -> tuple[list[dict], list[str]]:
    """Rows for the surahs MP3Quran's timing release rejected: its older ayat_timing, or rows measured in these files."""
    problems = []
    if spec["fallback"] == "mp3quran":
        folder = staging / "mp3quran-api" / f"ayat_timing-read{spec['moshaf']}"
        rows = []
        for surah in sorted(surahs):
            entries = [e for e in json.loads((folder / f"{surah:03}.json").read_text(encoding="utf-8")) if e["ayah"]]
            rows += [{"surah": surah, "ayah": e["ayah"], "startMs": e["start_time"], "endMs": e["end_time"]} for e in entries]
    else:
        measured = json.loads((OUTPUT / set_id / "measured_timings.json").read_text(encoding="utf-8"))["rows"]
        rows = [dict(surah=r["surah"], ayah=r["ayah"], startMs=r["startMs"], endMs=r["endMs"]) for r in measured if r["surah"] in surahs]
    for surah in sorted(surahs):
        ayat = [r["ayah"] for r in rows if r["surah"] == surah]
        if ayat != list(range(1, counts[surah - 1] + 1)):
            problems.append(f"fallback surah {surah}: ayat {ayat[:5]}... ({len(ayat)}), the mushaf has {counts[surah - 1]}")
    return rows, problems


def region_area(path: str) -> float:
    """The area of one ayah region (its M/L/Z sub-paths) in the reader's 345 x 550 space."""
    total = 0.0
    for sub in re.findall(r"M[^MZ]*Z?", path):
        numbers = list(map(float, re.findall(r"-?\d+(?:\.\d+)?", sub)))
        points = list(zip(numbers[::2], numbers[1::2]))
        total += abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))) / 2
    return total


def warsh_page_turns(rows: list[dict], qud: dict) -> tuple[list[dict], list[dict]]:
    """When the voice reaches the next page inside an ayah printed on two pages, for page following: an ESTIMATE, as for
    Al-Husary's Warsh set (tools/warsh_timing_review.py). The ayah's time is shared by its area on each page, then the turn moves
    to the nearest word that starts after a pause of at least 120 ms (QUD's word timings), or else the nearest word."""
    warsh = ROOT / "content" / "warsh"
    spans = json.loads((warsh / "ayah_page_spans.json").read_text(encoding="utf-8"))
    index = {(r["surah"], r["ayah"]): r for r in rows}
    words: dict[tuple[int, int], list[list[int]]] = {}
    with zipfile.ZipFile(qud["archive"]) as zf:
        for row in json.loads(gzip.decompress(zf.read("word_timestamps.json.gz")))["rows"]:
            surah, ayah = map(int, row[0].split(":"))
            words.setdefault((surah, ayah), []).extend(row[5])
    turns, detail = [], []
    for label, pages in sorted(spans.items(), key=lambda kv: tuple(map(int, kv[0].split(":")))):
        surah, ayah = map(int, label.split(":"))
        areas = [next(region_area(r["p"]) for r in json.loads((warsh / "quran-page-coordinates" / f"{page:03d}.json").read_text(encoding="utf-8"))
                      if (r["s"], r["a"]) == (surah, ayah)) for page in pages]
        row = index[surah, ayah]
        estimate = row["startMs"] + round((row["endMs"] - row["startMs"]) * areas[0] / sum(areas))
        ws = [w for w in words[surah, ayah] if row["startMs"] < w[1] < row["endMs"]]
        after_pause = [b[1] for a, b in zip(ws, ws[1:]) if b[1] - a[2] >= 120]
        at = min(after_pause or [w[1] for w in ws], key=lambda t: abs(t - estimate))
        turns.append({"surah": surah, "ayah": ayah, "page": pages[1], "atMs": at})
        detail.append({"ayah": label, "pages": pages, "atMs": at, "byAreaMs": estimate, "shareOnFirstPage": round(areas[0] / sum(areas), 3),
                       "afterAPause": bool(after_pause)})
    return turns, detail


def warsh_numbering_check(staging: Path) -> dict:
    """Proves QUD's Warsh keys are the app's Warsh ayat: the same keys, and per ayah QUD's words against the app's text, letters only."""
    words: dict[str, list[str]] = {}
    for word in json.loads(gzip.decompress((staging / f"qud-{QUD_VERSION}" / "warsh_words.json.gz").read_bytes())):
        words.setdefault(word["ref"].rsplit(":", 1)[0], []).append(word["text"])
    app = {}
    for line in (ROOT / "android/app/src/main/assets/warsh/quran-warsh-plain.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            s, a, text = line.split("|", 2)
            app[f"{s}:{a}"] = text
    fold = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ࡰ": "ا", "ࡴ": "ا", "ࡲ": "ا", "ى": "ي", "ے": "ي", "ؤ": "و", "ئ": "ي", "ء": "", "ة": "ه", "ۥ": "", "ۦ": "",
            "ں": "ن", "ڢ": "ف", "ڧ": "ق", "ٯ": "ق"}

    def letters(text: str) -> str:
        return "".join(fold.get(c, c) for c in unicodedata.normalize("NFKD", text) if unicodedata.category(c).startswith("L"))

    def ratio(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()

    ratios, better_next = [], []
    for key, text in app.items():
        ours = letters("".join(words.get(key, [])))
        own = ratio(ours, letters(text))
        ratios.append(own)
        s, a = map(int, key.split(":"))
        following = app.get(f"{s}:{a + 1}")
        if following and ratio(ours, letters(following)) > own:
            better_next.append(key)
    return {"qudKeys": len(words), "appKeys": len(app), "sameKeys": set(words) == set(app),
            "lowestTextMatch": round(min(ratios), 3), "ayatMatchingTheNextAyahBetter": better_next,
            "wordCountDiffers": [{"ayah": k, "qud": len(words[k]), "app": len(app[k].split())} for k in app if k in words and len(words[k]) != len(app[k].split())],
            "method": "QUD warsh_words.json.gz against quran-warsh-plain.txt, letters only, difflib ratio per ayah"}


def qul_rows(staging: Path, spec: dict, counts: list[int]) -> tuple[list[dict], list[dict], list[str]]:
    """QUL's surah_segments, ayah boundaries only (time_from, time_to)."""
    folder = staging / "qul-api" / f"surah_segments-{spec['qul']}"
    rows, problems = [], []
    for surah in range(1, 115):
        doc = json.loads((folder / f"{surah:03}.json").read_text(encoding="utf-8"))
        segments = doc["segments"]
        ayat = [int(k.split(":")[1]) for k in segments]
        if ayat != list(range(1, counts[surah - 1] + 1)):
            problems.append(f"QUL {spec['qul']} surah {surah}: {len(ayat)} ayat, the mushaf has {counts[surah - 1]}")
        rows += [{"surah": surah, "ayah": a, "startMs": v["time_from"], "endMs": v["time_to"]} for a, v in zip(ayat, segments.values())]
    return rows, [], problems


def qul_against_mp3quran(staging: Path, spec: dict, rows: list[dict]) -> dict:
    index = {}
    for surah in range(1, 115):
        for e in json.loads((staging / "mp3quran-api" / f"ayat_timing-read{spec['moshaf']}" / f"{surah:03}.json").read_text(encoding="utf-8")):
            index[surah, e["ayah"]] = (e["start_time"], e["end_time"])
    same = [r for r in rows if index.get((r["surah"], r["ayah"])) == (r["startMs"], r["endMs"])]
    other = [r for r in rows if index.get((r["surah"], r["ayah"])) != (r["startMs"], r["endMs"])]
    return {"identicalAyat": len(same), "differentAyat": len(other),
            "different": [f"{other[0]['surah']}:{other[0]['ayah']}", f"{other[-1]['surah']}:{other[-1]['ayah']}"] if other else [],
            "surahsWithDifferences": sorted({r["surah"] for r in other})}


def merge_rows(qud: dict, counts: list[int]) -> tuple[list[dict], list[dict], list[str]]:
    """One range per ayah: first segment start to last segment end (QUD repeats are merged)."""
    problems: list[str] = []
    segments: dict[tuple[int, int], list[list]] = {}
    previous: dict[int, list] = {}
    for ref, start, end, canonical, silence in qud["verse"]["rows"]:
        surah, ayah = map(int, ref.split(":"))
        if surah in previous and start < previous[surah][1]:
            problems.append(f"QUD row {ref} starts at {start} before the previous row ends ({previous[surah]})")
        previous[surah] = [ref, end]
        segments.setdefault((surah, ayah), []).append([start, end, canonical, silence])
    expected = {(s, a) for s in range(1, 115) for a in range(1, counts[s - 1] + 1)}
    missing, extra = sorted(expected - set(segments)), sorted(set(segments) - expected)
    if missing or extra:
        problems.append(f"QUD ayat missing {missing[:10]} extra {extra[:10]}")
    rows = [{"surah": s, "ayah": a, "startMs": min(x[0] for x in segments[s, a]), "endMs": max(x[1] for x in segments[s, a])}
            for s, a in sorted(expected & set(segments))]
    canonical_count = {}
    for key, segs in segments.items():
        canonical_count[key] = sum(1 for x in segs if x[2])
    bad = sorted(k for k, n in canonical_count.items() if n != 1)
    if bad:
        problems.append(f"ayat without exactly one canonical QUD occurrence: {bad}")
    index = {(r["surah"], r["ayah"]): r for r in rows}
    repeats = []
    for (surah, ayah), segs in sorted(segments.items()):
        if len(segs) < 2:
            continue
        before, after = index.get((surah, ayah - 1)), index.get((surah, ayah + 1))
        merged = index[surah, ayah]
        repeats.append({
            "ayah": f"{surah}:{ayah}",
            "segments": [{"startMs": x[0], "endMs": x[1], "canonical": x[2]} for x in segs],
            "order": "repeat first, then canonical" if not segs[0][2] else "canonical first, then repeat",
            "mergedStartMs": merged["startMs"], "mergedEndMs": merged["endMs"],
            "previousAyahEndMs": before["endMs"] if before else None,
            "nextAyahStartMs": after["startMs"] if after else None,
            "overlapsNeighbour": bool((before and merged["startMs"] < before["endMs"])
                                      or (after and merged["endMs"] > after["startMs"])),
        })
    return rows, repeats, problems


def validate(rows: list[dict], durations: dict[int, float], qud: dict | None, total: int = 6236) -> tuple[dict, list[str]]:
    hard: list[str] = []
    by_surah: dict[int, list[dict]] = {}
    for row in rows:
        by_surah.setdefault(row["surah"], []).append(row)
        if not row["startMs"] < row["endMs"]:
            hard.append(f"{row['surah']}:{row['ayah']} startMs {row['startMs']} >= endMs {row['endMs']}")
    keys = [(r["surah"], r["ayah"]) for r in rows]
    if len(keys) != total or len(set(keys)) != total:
        hard.append(f"{len(keys)} rows, {len(set(keys))} distinct ayat (need {total} once each)")
    overlaps, beyond, margins, trailing = [], [], [], []
    for surah, items in sorted(by_surah.items()):
        for a, b in zip(items, items[1:]):
            if b["startMs"] < a["endMs"] or b["ayah"] != a["ayah"] + 1:
                overlaps.append(f"{surah}:{a['ayah']}->{b['ayah']} ({a['endMs']} > {b['startMs']})")
        last = items[-1]["endMs"]
        duration_ms = round(durations[surah] * 1000)
        margins.append((duration_ms - last, surah))
        trailing.append({"surah": surah, "lastEndMs": last, "durationMs": duration_ms, "afterLastAyahMs": duration_ms - last})
        if last > duration_ms + 50:
            beyond.append(f"surah {surah}: last ayah ends {last} ms, file lasts {duration_ms} ms")
    hard += [f"overlap or gap in numbering: {x}" for x in overlaps] + beyond
    lengths = [(r["endMs"] - r["startMs"], f"{r['surah']}:{r['ayah']}") for r in rows]
    short = sorted([{"ayah": k, "lengthMs": v} for v, k in lengths if v < 300], key=lambda x: x["lengthMs"])
    long = sorted([{"ayah": k, "lengthMs": v} for v, k in lengths if v > 300000], key=lambda x: -x["lengthMs"])
    specials = qud["specials"]["chapters"] if qud else {}
    opening = []
    for surah, items in sorted(by_surah.items()):
        chapter = specials.get(str(surah), {})
        record = {"surah": surah, "ayah1StartMs": items[0]["startMs"], "ayah1EndMs": items[0]["endMs"]}
        for name in ("istiadha", "basmala"):
            if name in chapter:
                record[name] = [chapter[name]["start_ms"], chapter[name]["end_ms"]]
                record[name + "SameVoice"] = chapter[name].get("same_voice")
        opening.append(record)
    no_basmala = [o["surah"] for o in opening if "basmala" not in o]
    with_istiadha = [o["surah"] for o in opening if "istiadha" in o]
    basmala_inside_ayah1 = [o["surah"] for o in opening if "basmala" in o and o["surah"] != 1
                            and o["basmala"][1] > o["ayah1StartMs"]]
    fatiha = opening[0]
    gaps = sorted(((b["startMs"] - a["endMs"], f"{s}:{a['ayah']}-{b['ayah']}")
                   for s, items in by_surah.items() for a, b in zip(items, items[1:])), reverse=True)
    report = {
        "ayatTimed": len(set(keys)), "rows": len(rows), "everyAyahOnce": len(keys) == len(set(keys)) == total,
        "monotonicPerSurahNoOverlap": not overlaps, "startBeforeEnd": all(r["startMs"] < r["endMs"] for r in rows),
        "lastEndWithinDurationPlus50ms": not beyond,
        "smallestMarginAfterLastAyahMs": {"ms": min(margins)[0], "surah": min(margins)[1]},
        "largestAudioAfterLastAyahMs": {"ms": max(margins)[0], "surah": max(margins)[1]},
        "surahsWhoseLastEndEqualsFileDurationWithin1ms": sum(1 for m, _ in margins if abs(m) <= 1),
        "ayatShorterThan300ms": short, "ayatLongerThan5min": long,
        "shortestAyah": {"ayah": min(lengths)[1], "lengthMs": min(lengths)[0]},
        "longestAyah": {"ayah": max(lengths)[1], "lengthMs": max(lengths)[0]},
        "largestGapsBetweenAyat": [{"between": k, "ms": v} for v, k in gaps[:5]],
        "untimedGapsOver2s": [{"between": k, "ms": v} for v, k in gaps if v > 2000],
        "openings": {
            "fatiha": {"ayah1": [fatiha["ayah1StartMs"], fatiha["ayah1EndMs"]], "istiadha": fatiha.get("istiadha"),
                       "basmala": fatiha.get("basmala"),
                       "ayah1IsTheBasmala": fatiha.get("basmala") == [fatiha["ayah1StartMs"], fatiha["ayah1EndMs"]]},
            "tawbah": next(o for o in opening if o["surah"] == 9),
            "surahsWithoutBasmalaSegment": no_basmala,
            "surahsWithIstiadhaSegment": with_istiadha,
            "surahsWhereBasmalaRunsIntoAyah1": basmala_inside_ayah1,
            "ayah1StartsAfterBasmalaEverywhere": not basmala_inside_ayah1,
            "perSurah": opening,
        },
        "perSurahEnd": trailing,
    }
    return report, hard


def cross_check(rows: list[dict], staging: Path, spec: dict, counts: list[int]) -> dict:
    folder = staging / "mp3quran-api" / f"ayat_timing-read{spec['moshaf']}"
    index = {(r["surah"], r["ayah"]): r for r in rows}
    per_surah, all_start, all_abs, big = [], [], [], []
    numbering = []
    for surah in range(1, 115):
        entries = json.loads((folder / f"{surah:03}.json").read_text(encoding="utf-8"))
        ayat = [e["ayah"] for e in entries]
        if sorted(ayat) != list(range(1, counts[surah - 1] + 1)):
            numbering.append({"surah": surah, "mp3quranAyat": len(ayat), "hafsAyat": counts[surah - 1],
                              "extra": sorted(set(ayat) - set(range(1, counts[surah - 1] + 1))),
                              "missing": sorted(set(range(1, counts[surah - 1] + 1)) - set(ayat))})
        starts, ends = [], []
        for e in entries:
            row = index.get((surah, e["ayah"]))
            if row is None:
                continue
            d_start, d_end = e["start_time"] - row["startMs"], e["end_time"] - row["endMs"]
            starts.append((abs(d_start), d_start, e["ayah"]))
            ends.append(abs(d_end))
            all_start.append(d_start)
            all_abs.append(abs(d_start))
            if abs(d_start) > 2000:
                big.append({"ayah": f"{surah}:{e['ayah']}", "mp3quranStartMs": e["start_time"], "appStartMs": row["startMs"],
                            "deltaMs": d_start})
        if not starts:
            continue
        worst = max(starts)
        per_surah.append({"surah": surah, "ayat": len(starts),
                          "medianStartDeltaMs": statistics.median(d for _, d, _ in starts),
                          "medianAbsStartDeltaMs": statistics.median(a for a, _, _ in starts),
                          "maxAbsStartDeltaMs": worst[0], "maxAt": f"{surah}:{worst[2]}",
                          "medianAbsEndDeltaMs": statistics.median(ends)})
    if not all_start:
        return {"source": f"{MP3QURAN_API}ayat_timing?surah=N&read={spec['moshaf']} (N = 1..114)", "ayatCompared": 0,
                "note": "MP3Quran publishes no timing for this moshaf (empty response for every surah).",
                "medianStartDeltaMs": None, "absStartDeltaMsPercentiles": {"p99": None}, "ayatOver2s": []}
    return {
        "source": f"{MP3QURAN_API}ayat_timing?surah=N&read={spec['moshaf']} (N = 1..114)",
        "compared": "MP3Quran start_time/end_time minus this set's startMs/endMs (after merging repeats)",
        "ayatCompared": len(all_start),
        "medianStartDeltaMs": statistics.median(all_start),
        "absStartDeltaMsPercentiles": {"p50": _percentile(all_abs, 50), "p90": _percentile(all_abs, 90),
                                       "p99": _percentile(all_abs, 99), "max": max(all_abs)},
        "ayatOver2s": big,
        "surahsWithMaxOver2s": [p["surah"] for p in per_surah if p["maxAbsStartDeltaMs"] > 2000],
        "numberingDifferences": numbering,
        "perSurah": per_surah,
    }


CLAMP_LIMIT_MS = 1000


def apply_timing_corrections(path: Path, by_key: dict[tuple[int, int], dict]) -> list[dict]:
    """Applies a set's corrections file (recorded as in content/warsh/timing_corrections.json). Each names the value it replaces, so a change underneath stops the build."""
    done = []
    for fix in json.loads(path.read_text(encoding="utf-8"))["corrections"]:
        surah, ayah = map(int, fix["ayah"].split(":"))
        row = by_key[surah, ayah]
        field = {"start": "startMs", "end": "endMs"}[fix["field"]]
        assert row[field] == fix["old"], (fix["ayah"], fix["field"], row[field], fix["old"])
        row[field] = fix["new"]
        done.append({"ayah": fix["ayah"], "field": fix["field"], "old": fix["old"], "new": fix["new"], "kind": fix["kind"]})
    return done


def clamp_to_duration(rows: list[dict], durations: dict[int, float]) -> list[dict]:
    """A last ayah may not end after its file: end it at the file's length (only if it overshoots by <= 1 s)."""
    adjustments = []
    last = {}
    for row in rows:
        last[row["surah"]] = row
    for surah, row in sorted(last.items()):
        duration_ms = round(durations[surah] * 1000)
        if duration_ms < row["endMs"] <= duration_ms + CLAMP_LIMIT_MS:
            adjustments.append({"ayah": f"{surah}:{row['ayah']}", "field": "endMs", "qudValue": row["endMs"],
                                "newValue": duration_ms, "reason": "QUD's end is after the last audio frame of the file"})
            row["endMs"] = duration_ms
    return adjustments


def pause_alignment(activity: array, rate: int, qud_starts: list[int], mp3quran_starts: list[int]) -> dict:
    """Where, relative to the timed ayah starts, the recitation resumes after the pause before each ayah.

    For shifts of -1.5..+1.5 s, the envelope around start+shift (+-2 granules) minus its +-1 s surroundings is averaged
    over the ayat of the surah. That profile has a dip (the pause); the onset is where it climbs back to half the dip's
    depth after the deepest point. Timings made for this file put the onset at about the same shift in every surah;
    a timing made for another edit or encoding of the file does not.
    """
    granule = 576 / rate * 1000
    prefix = [0]
    for value in activity:
        prefix.append(prefix[-1] + value)
    n = len(activity)

    def mean(lo: int, hi: int) -> float:
        lo, hi = max(0, lo), min(n, hi)
        return (prefix[hi] - prefix[lo]) / (hi - lo) if hi > lo else 0.0

    reach = round(1500 / granule)

    def onset(starts: list[int]):
        if not starts:
            return None
        profile = []
        for shift in range(-reach, reach + 1):
            total = 0.0
            for start in starts:
                g = round(start / granule) + shift
                total += mean(g - 2, g + 2) - mean(g - 77, g + 77)
            profile.append(total / len(starts))
        k = min(range(len(profile)), key=profile.__getitem__)
        half = profile[k] / 2
        for j in range(k + 1, len(profile)):
            if profile[j] >= half:
                x = j - 1 + (half - profile[j - 1]) / (profile[j] - profile[j - 1]) if profile[j] != profile[j - 1] else j
                return round((x - reach) * granule)
        return None

    return {"ayatUsed": len(qud_starts), "qudOnsetMs": onset(qud_starts), "mp3quranOnsetMs": onset(mp3quran_starts)}


def summarize_pauses(pauses: list[dict]) -> dict:
    usable = [p for p in pauses if p["ayatUsed"] >= 5 and p["qudOnsetMs"] is not None]
    summary_ = {
        "method": "Envelope = summed MPEG side-information global_gain per granule (no decoding). Per surah, the profile "
                  "of that envelope around the ayah starts (ayah >= 2) shows the pause before each ayah; the 'onset' is "
                  "where the profile climbs back to half the pause depth, in ms relative to the timed start. "
                  "Resolution about one granule (13 ms). Surahs with fewer than 5 starts are listed but not judged.",
        "surahsJudged": len(usable),
    }
    for key, label in (("qudOnsetMs", "qud"), ("mp3quranOnsetMs", "mp3quran")):
        values = [p[key] for p in usable if p[key] is not None]
        if not values:
            summary_[f"{label}MedianOnsetMs"] = None
            summary_[f"{label}OnsetRangeMs"] = None
            summary_[f"{label}SurahsOffMedianBy150msOrMore"] = []
            continue
        median = statistics.median(values)
        summary_[f"{label}MedianOnsetMs"] = median
        summary_[f"{label}OnsetRangeMs"] = [min(values), max(values)]
        summary_[f"{label}SurahsOffMedianBy150msOrMore"] = [
            {"surah": p["surah"], "onsetMs": p[key]} for p in usable if p[key] is None or abs(p[key] - median) >= 150]
    summary_["perSurah"] = pauses
    return summary_


# ------------------------------------------------------------------------------------------ build

def _log(staging: Path, name: str) -> dict:
    path = staging / "fetch-log" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def build(staging: Path, set_id: str, counts: list[int]) -> tuple[dict[str, str], dict, list[str]]:
    spec = SETS[set_id]
    riwayah = spec.get("riwayah", "hafs")
    style = spec.get("style", "murattal")
    on_mp3quran = spec["server"].startswith("https://cdn.mp3quran.net/")
    audio_dir = staging / set_id
    names = sorted(p.name for p in audio_dir.iterdir())
    expected = [f"{s:03}.mp3" for s in range(1, 115)]
    assert names == expected, f"{audio_dir}: expected exactly 001.mp3..114.mp3, found {len(names)} entries"
    audio_log, catalogue_log = _log(staging, set_id), _log(staging, "catalogues")
    timing_log = _log(staging, f"ayat_timing-read{spec['moshaf']}")
    timing_dir = staging / "mp3quran-api" / f"ayat_timing-read{spec['moshaf']}"
    qud = load_qud(staging, spec) if spec.get("qud") else None
    release = load_timing_release(staging, spec) if spec.get("timingRelease") else None
    if qud:
        rows, repeats, problems = merge_rows(qud, counts)
    elif release:
        rejected = frozenset(spec.get("releaseRejected", []))
        patches, measured = measured_patches(set_id)
        rows, repeats, problems = release_rows(release, counts, rejected, patches, measured)
        if rejected:
            extra, extra_problems = fallback_rows(staging, set_id, spec, counts, rejected)
            rows = sorted(rows + extra, key=lambda r: (r["surah"], r["ayah"]))
            problems += extra_problems
    elif spec.get("qul"):
        rows, repeats, problems = qul_rows(staging, spec, counts)
    elif spec.get("ayahClips"):
        rows, repeats, problems = clip_rows(set_id, counts)
    else:
        rows, repeats, problems = mp3quran_rows(staging, spec, counts)
    tracks, pauses = [], []
    for surah in range(1, 115):
        activity = array("H")
        info = parse_mp3(audio_dir / f"{surah:03}.mp3", activity)
        # MP3Quran's rows were made for another recording of this reciter, so they say nothing about these files.
        mp3quran = [] if spec.get("ayahClips") else json.loads((timing_dir / f"{surah:03}.json").read_text(encoding="utf-8"))
        pauses.append({"surah": surah, **pause_alignment(
            activity, info["sampleRateHz"], [r["startMs"] for r in rows if r["surah"] == surah and r["ayah"] >= 2],
            [e["start_time"] for e in mp3quran if e["ayah"] >= 2])})
        url = f"{spec['server']}{surah:03}.mp3"
        fetched = audio_log.get(url, {})
        headers = fetched.get("headers", {})
        assert fetched.get("bytes") in (None, info["sizeBytes"]), f"{url}: staged size differs from the fetch log"
        tracks.append({"surahNumber": surah, "fileName": f"{surah:03}.mp3", "sourceUrl": url,
                       "lastModified": headers.get("Last-Modified"), "etag": headers.get("ETag"),
                       "fetchedAt": fetched.get("fetchedAt"), **info})
    durations = {t["surahNumber"]: t["durationSeconds"] for t in tracks}
    audio_identity = None
    if release:
        # The release names the bytes each surah was aligned to: these files must be exactly those.
        mismatched = []
        for t in tracks:
            if t["surahNumber"] in spec.get("releaseRejected", []):
                continue
            aligned = release["doc"]["surahs"][str(t["surahNumber"])]["word_timings"]["audio"]
            if (aligned["sha256"], aligned["bytes"]) != (t["sha256"], t["sizeBytes"]):
                mismatched.append(t["surahNumber"])
        if mismatched:
            problems.append(f"timing release: files {mismatched} are not the bytes it was aligned to")
        rejected_listed = sorted(int(x.split(":")[0]) for x in release["manifestStatus"]["notAccepted"])
        if rejected_listed != sorted(spec.get("releaseRejected", [])):
            problems.append(f"timing release manifest: {release['manifestStatus']}, expected rejected {spec.get('releaseRejected', [])}")
        audio_identity = {"filesMatchingAlignedSha256AndSize": 114 - len(spec.get("releaseRejected", [])) - len(mismatched),
                          "mismatched": mismatched, "notTimedByTheRelease": spec.get("releaseRejected", [])}
    shifted = []
    for shift in spec.get("shifts", []):
        moved = [r for r in rows if r["surah"] == shift["surah"] and r["ayah"] >= shift["fromAyah"]]
        for r in moved:  # the seam is at the first moved ayah's start, so that start stays
            r["startMs"] += shift["ms"] if r["ayah"] > shift["fromAyah"] else 0
            r["endMs"] += shift["ms"]
        shifted.append({"surah": shift["surah"], "ayat": f"{moved[0]['ayah']}-{moved[-1]['ayah']}", "shiftMs": shift["ms"], "reason": shift["reason"]})
    # Positions measured in these bytes by tools/check_recitation_audio.py --locate-clips (each clip located at r >= 0.9).
    measured = OUTPUT / set_id / "clip_check.json"
    clip_doc = json.loads(measured.read_text(encoding="utf-8")) if measured.is_file() else {}
    measured_rows = clip_doc.get("apply", [])
    by_key = {(r["surah"], r["ayah"]): r for r in rows}
    for m in measured_rows:
        before = by_key.get((m["surah"], m["ayah"] - 1))    # the envelope's 10 ms steps must not overlap a row left as it was
        by_key[m["surah"], m["ayah"]].update(startMs=max(m["startMs"], before["endMs"]) if before else m["startMs"], endMs=m["endMs"])
    for surah in sorted({m["surah"] for m in measured_rows}):
        ayat = [m["ayah"] for m in measured_rows if m["surah"] == surah]
        shifted.append({"surah": surah, "ayat": f"{ayat[0]}-{ayat[-1]}", "shiftMs": None,
                        "reason": "start and end measured by locating each ayah's clip in this file (clip_check.json)"})
    for fix in spec.get("corrections", []):
        row, before = by_key[fix["surah"], fix["ayah"]], by_key.get((fix["surah"], fix["ayah"] - 1))
        shifted.append({"surah": fix["surah"], "ayat": str(fix["ayah"]), "shiftMs": fix["startMs"] - row["startMs"], "reason": fix["reason"]})
        row["startMs"] = fix["startMs"]
        if before:
            before["endMs"] = fix["startMs"]
    adjustments = clamp_to_duration(rows, durations)
    corrected = apply_timing_corrections(OUTPUT / set_id / spec["timingCorrections"], by_key) if spec.get("timingCorrections") else []
    report, hard = validate(rows, durations, qud, sum(counts))
    hard = problems + hard
    for repeat in repeats:
        if repeat["overlapsNeighbour"]:
            hard.append(f"merged repeat {repeat['ayah']} overlaps a neighbouring ayah")
    if spec.get("ayahClips"):
        check = {"source": f"{MP3QURAN_API}ayat_timing?surah=N&read={spec['moshaf']} (N = 1..114)", "ayatCompared": 0,
                 "note": "Not compared: MP3Quran's read=299 is for another recording of this reciter.",
                 "medianStartDeltaMs": None, "absStartDeltaMsPercentiles": {"p99": None}, "ayatOver2s": []}
    else:
        check = cross_check(rows, staging, spec, counts)
    for repeat in repeats:
        surah, ayah = map(int, repeat["ayah"].split(":"))
        entry = next((e for e in json.loads((timing_dir / f"{surah:03}.json").read_text(encoding="utf-8"))
                      if e["ayah"] == ayah), None)
        repeat["mp3quranRangeMs"] = [entry["start_time"], entry["end_time"]] if entry else None

    reciters = json.loads((staging / "mp3quran-api" / "reciters_ar.json").read_text(encoding="utf-8"))
    reciter = next(r for r in reciters["reciters"] if any(m["id"] == spec["moshaf"] for m in r["moshaf"]))
    moshaf = next(m for m in reciter["moshaf"] if m["id"] == spec["moshaf"])
    assert (moshaf["server"] == spec["server"] or not on_mp3quran) and moshaf["surah_total"] == 114, moshaf
    assert sorted(map(int, moshaf["surah_list"].split(","))) == list(range(1, 115))

    host = "MP3Quran" if on_mp3quran else spec.get("host", "")
    riwaya_name = {"hafs": "Hafs", "warsh": "Warsh"}[riwayah]
    if qud:
        timing_source = f"QUD Technologies quranic-universal-audio {QUD_VERSION}; {qud['catalog']['name_en']} {host} {riwaya_name}"
        if style != "murattal":
            timing_source += f" {style}"
    elif release:
        timing_source = f"MP3Quran timing release {TIMING_RELEASE}, recitation {spec['timingRelease']}; {spec['reciterEn']} {host} {riwaya_name}"
        if spec.get("releaseRejected"):
            timing_source += (f"; surahs {', '.join(map(str, spec['releaseRejected']))} from "
                              + (f"MP3Quran ayat_timing read={spec['moshaf']}" if spec["fallback"] == "mp3quran" else "local speech recognition in these files"))
    elif spec.get("qul"):
        timing_source = f"QUL surah_segments {spec['qul']} (MP3Quran ayat_timing read={spec['moshaf']} where it is complete); {spec['reciterEn']} {riwaya_name}"
    elif spec.get("ayahClips"):
        timing_source = f"Measured in these files from the ayah clips of archive.org item {spec['ayahClips']}; {spec['reciterEn']} {riwaya_name}"
    else:
        timing_source = f"MP3Quran ayat_timing read={spec['moshaf']}; {spec['reciterEn']} {riwaya_name}"
    timing = {"source": timing_source, "sourceOffsetMs": 0, "units": "ms", "rows": rows}
    turns_detail = None
    if riwayah == "warsh":
        timing["pageTurns"], turns_detail = warsh_page_turns(rows, qud)
    manifest = {"schemaVersion": 2, "id": set_id, "reciterAr": spec["reciterAr"], "reciterEn": spec["reciterEn"],
                "riwaya": RIWAYA_NAMES[riwayah], "style": style, "baseUrl": f"{RELEASE_BASE}{set_id}/",
                "sources": [f"{MIRROR_BASE}{set_id}/", f"{RELEASE_BASE}{set_id}/"], "timingOffsetMs": 0,
                "tracks": [{"surah": t["surahNumber"], "file": t["fileName"], "size": t["sizeBytes"], "sha256": t["sha256"],
                            "durationMs": round(t["durationSeconds"] * 1000)}
                           for t in tracks]}
    modes = sorted({t["bitrateMode"] for t in tracks})
    fetch_dates = sorted(t["fetchedAt"] for t in tracks if t["fetchedAt"])
    last_modified = sorted({t["lastModified"] for t in tracks if t["lastModified"]})
    reciters_fetch = catalogue_log.get(RECITERS_URL, {})
    if on_mp3quran:
        source = f"mp3quran.net (moshaf {spec['moshaf']}, {moshaf['name']}), {spec['server']}NNN.mp3, downloaded unmodified"
        terms = ("mp3quran.net lets any visitor or developer copy and use its material (privacy page, Copyrights "
                 "section). This is MP3Quran's statement, not a licence from the reciter or recording producer.")
    elif spec.get("ayahClips"):
        source = f"{spec['host']}, {spec['server']}NNN.mp3, downloaded unmodified"
        terms = spec["terms"]
    else:
        source = f"{spec['host']}, {spec['server']}NNN.mp3 (the exact URLs QUD's catalog lists), downloaded unmodified"
        terms = spec["terms"]
    if qud:
        timing_note = (f"QUD's catalog lists exactly these URLs, so its timings are used as published. No independent "
                       "offset measurement was possible: QUD publishes no audio checksums for the files it aligned.")
        timings_block = {
            "provider": "QUD Technologies, quranic-universal-audio", "licence": qud["manifest"]["license"],
            "release": QUD_VERSION, "releaseCreatedAt": qud["manifest"]["created_at"],
            "asset": QUD_RELEASE + qud["archive"].name, "assetSha256": qud["sha256"], "assetBytes": qud["archive"].stat().st_size,
            "manifestUrl": QUD_RELEASE + "manifest.json", "manifestSha256": qud["manifestSha256"],
            "manifestEntry": {k: qud["entry"][k] for k in ("sha256", "bytes", "ts_version", "content_hash",
                                                           "coverage_ayahs", "riwayah", "change_kind")},
            "releaseCatalogUrl": QUD_RELEASE + "catalog.json", "releaseCatalogSha256": qud["releaseCatalogSha256"],
            "releaseCatalogEntriesForSlug": qud["listedInReleaseCatalog"],
            "zipMembers": qud["members"],
            "catalog": {k: qud["catalog"][k] for k in ("slug", "name_en", "name_ar", "riwayah", "style", "channel",
                                                       "audio_category", "coverage")},
            "catalogAudio": {k: v for k, v in qud["catalog"]["audio"].items() if k != "chapter_urls"},
            "chapterUrlsMatchMoshaf": True,
            "verseMeta": qud["verse"]["_meta"],
            "conversion": "verse_timestamps.json.gz rows [ref, start, end, canonical, silence_after] -> one row per ayah: "
                          "startMs = first occurrence start, endMs = last occurrence end. Values otherwise unchanged.",
        }
    elif release:
        timing_note = (f"MP3Quran's timing release {TIMING_RELEASE} names the sha256 and size of the MP3 each surah was aligned to, and every "
                       "file here is exactly those bytes, so its timings are used as published. audio_check.json has the check against the "
                       "decoded audio.")
        meta = release["release"]
        timings_block = {
            "provider": "MP3Quran timing release (v4 API)", "release": TIMING_RELEASE, "releaseCreatedAt": meta.get("created_at"),
            "releaseRecord": TIMING_RELEASE_API, "textSource": meta.get("text_source"), "granularities": meta.get("granularities"),
            "recitationId": spec["timingRelease"], "url": f"{TIMING_RELEASE_BASE}recitations/{spec['timingRelease']}.json",
            "sha256": release["sha256"], "bytes": release["bytes"],
            "manifestUrl": TIMING_RELEASE_BASE + "manifest.json", "manifestSha256": release["manifestSha256"],
            "manifestStatus": release["manifestStatus"], "audioIdentity": audio_identity,
            **({"fallbackSurahs": {"surahs": spec["releaseRejected"], "source": FALLBACK_NOTES[spec["fallback"]](spec)}} if spec.get("releaseRejected") else {}),
            **({"patchedAyat": {"ayat": {str(k): v for k, v in measured_patches(set_id)[0].items()}, "source": FALLBACK_NOTES["measured"](spec),
                                "why": "where MP3Quran's aligner timed under 70 % of an ayah's words, or the release is plainly wrong (an ayah missing, "
                                       "mislabelled or swapped), the ayah and its neighbours were measured and replace the release's rows"}}
               if measured_patches(set_id)[0] else {}),
            "licence": "mp3quran.net's general statement (FUTURE_CONTENT_SOURCES.md C.4); no separate licence for the timing",
            "conversion": "ayah_timings [ayah, start, end] unchanged; ayah 0 (isti'adha, basmalah) dropped; a repeated ayah merged "
                          "(first start to last end); a return to an earlier ayah kept lit on the ayah before it (see repeats). "
                          "Word, letter and phoneme timings are not used.",
        }
    else:
        timing_note = ("MP3Quran's ayat_timing for this moshaf, as published. It is a draft: see audio_check.json for the "
                       "check against the decoded audio.")
        timings_block = {
            "provider": "MP3Quran ayat_timing", "url": check["source"],
            "licence": "mp3quran.net's general statement (FUTURE_CONTENT_SOURCES.md C.4); no separate licence for the timing",
            "conversion": "Each surah's rows, start_time and end_time unchanged; the row numbered ayah 0 (isti'adha, basmalah) dropped.",
        }
        if spec.get("qul"):
            timing_note = ("QUL's ayah boundaries. QUL lists quranicaudio.com's copy for this recitation, but the numbers are MP3Quran's "
                           "(identical but for the end of surah 4) and fit MP3Quran's file: QUL's 4:176 ends inside this set's 004.mp3 and after "
                           "quranicaudio's. clip_check.json locates everyayah's clips of this recording in these bytes; surah 2 from 2:177 and surah 4 "
                           "from 4:87 hold those measured positions. audio_check.json has the check against the decoded audio.")
            timings_block = {
                "provider": "QUL (Tarteel), Quranic Universal Library", "recitationId": spec["qul"],
                "url": f"{QUL_API}surah_segments/{spec['qul']}?surah=N&per_page=500 (N = 1..114)",
                "licence": "QUL's page for this recitation: \"We don't have copyright information for this resource\"",
                "againstMp3quran": qul_against_mp3quran(staging, spec, rows),
                "conversion": "segments[ref].time_from/time_to unchanged; the word timings (empty in the API) are not used.",
            }
        if spec.get("ayahClips"):
            timing_note = ("Measured in these bytes. The ayah clips of archive.org item " + spec["ayahClips"] + " were cut from these very files (waveform correlation near 1 at one "
                           "place each): clip_check.json has every clip's place. An ayah starts where its clip lies and ends where the next one starts. audio_check.json has the check "
                           "against the decoded audio, speech_check.json the independent check by local speech recognition.")
            timings_block = {
                "provider": "Measured from the ayah clips of archive.org item " + spec["ayahClips"] + " (uploader aymankotb1973@yahoo.com, 16 Sep 2026)",
                "url": clip_doc.get("source"),
                "licence": "The numbers are this project's measurement; the clips are not redistributed. The item says it is for archiving and non-commercial education and research "
                           "(\"مثل تطوير تطبيقات القرآن الكريم\"), and that the audio's rights stay with the reciter and producers.",
                "conversion": "startMs = where the ayah's clip lies in the file; endMs = the next ayah's startMs, the last ayah's the end of its clip, at most the file's end; "
                              "then the corrections in SETS and in timing_corrections.json.",
                "located": {k: clip_doc.get(k) for k in ("clips", "lockedAtR0.8", "lockedRPercentiles", "joinMsPercentiles", "ayahStartsNotLocked")},
            }
    provenance = {
        "schemaVersion": 3,
        "id": set_id,
        "reciter": spec["reciterEn"], "reciterAr": spec["reciterAr"],
        "riwaya": RIWAYA_NAMES[riwayah], "style": style.capitalize(),
        "status": "Prepared, not published. The release tag below is planned; nothing has been uploaded.",
        **({"timingRejected": spec["timingRejected"]} if spec.get("timingRejected") else {}),
        "source": source,
        "sourceTerms": terms,
        "releaseTag": set_id, "baseUrlPlanned": f"{RELEASE_BASE}{set_id}/",
        "timingOffsetMs": 0,
        "timingOffsetNote": timing_note,
        "mp3quranApi": {
            "url": RECITERS_URL, "finalUrl": reciters_fetch.get("finalUrl"), "fetchedAt": reciters_fetch.get("fetchedAt"),
            "sha256": _sha256(staging / "mp3quran-api" / "reciters_ar.json"),
            "reciter": {"id": reciter["id"], "name": reciter["name"], "date": reciter.get("date")},
            "moshaf": {k: moshaf[k] for k in ("id", "name", "rewaya_id", "server", "surah_total", "moshaf_type")},
            "ayatTiming": {"url": check["source"], "fetchedAt": sorted(v["fetchedAt"] for v in timing_log.values())[:1]
                           + sorted(v["fetchedAt"] for v in timing_log.values())[-1:]},
        },
        "timings": timings_block,
        "totals": {
            "tracks": len(tracks), "bytes": sum(t["sizeBytes"] for t in tracks),
            "durationSeconds": round(sum(t["durationSeconds"] for t in tracks), 3),
            "bitrateModes": modes,
            "bitratesKbps": sorted({t["bitrateKbps"] for t in tracks}),
            "sampleRatesHz": sorted({t["sampleRateHz"] for t in tracks}),
            "channelModes": sorted({t["channelMode"] for t in tracks}),
            "filesWithInfoTag": [t["surahNumber"] for t in tracks if "infoTag" in t],
            "filesWithId3v2": sum(1 for t in tracks if t["id3v2Bytes"]),
            "filesWithNonFrameBytes": [t["surahNumber"] for t in tracks if t["nonFrameBytes"]],
            "lastModifiedRange": [last_modified[0], last_modified[-1]] if last_modified else None,
            "lastModifiedDistinct": len(last_modified),
            "fetchedRange": [fetch_dates[0], fetch_dates[-1]] if fetch_dates else None,
        },
        "durationMethod": "Every MPEG Layer III frame counted (ID3v2/ID3v1/APE tags and any Xing/Info tag frame excluded); "
                          "duration = audio frames x samples per frame / sample rate.",
        "repeats": repeats,
        "repeatMapping": "Each repeated ayah keeps one range from its first occurrence's start to its last occurrence's end, "
                         "so the highlight stays on the ayah while it is repeated. No merged range overlaps a neighbour.",
        "timingAdjustments": adjustments,
        **({"timingShifts": shifted} if shifted else {}),
        **({"timingCorrections": {"file": f"content/recitations/{set_id}/{spec['timingCorrections']}", "values": corrected}} if corrected else {}),
        **({"pageTurns": turns_detail, "warshNumberingCheck": warsh_numbering_check(staging)} if turns_detail else {}),
        "pauseAlignmentCheck": summarize_pauses(pauses),
        "validation": {**report, "hardFailures": hard},
        "mp3quranCrossCheck": check,
        "trackCount": len(tracks), "trackOrder": "Surah number order, 1 through 114",
        "tracks": tracks,
    }
    outputs = {
        "audio_manifest.json": json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        **({spec["timingFile"]: json.dumps(timing, ensure_ascii=False, separators=(",", ":"))} if spec["timingFile"] else {}),
        "provenance.json": json.dumps(provenance, ensure_ascii=False, indent=4) + "\n",
    }
    return outputs, provenance, hard


def summary(set_id: str, provenance: dict) -> str:
    t, v, c = provenance["totals"], provenance["validation"], provenance["mp3quranCrossCheck"]
    return (f"{set_id}: {t['tracks']} files, {t['bytes']:,} B ({t['bytes'] / 1e9:.3f} GB), "
            f"{t['durationSeconds'] / 3600:.2f} h, {'/'.join(t['bitrateModes'])} {t['bitratesKbps']} kbps, "
            f"{t['sampleRatesHz']} Hz; {v['ayatTimed']} ayat, repeats {len(provenance['repeats'])}, "
            f"short {len(v['ayatShorterThan300ms'])}, long {len(v['ayatLongerThan5min'])}, "
            f"hard failures {len(v['hardFailures'])}; MP3Quran median start delta {c['medianStartDeltaMs']} ms, "
            f"p99 |delta| {c['absStartDeltaMsPercentiles']['p99']} ms, {len(c['ayatOver2s'])} ayat over 2 s; "
            f"adjusted {[a['ayah'] for a in provenance['timingAdjustments']]}; onset check: QUD median "
            f"{provenance['pauseAlignmentCheck']['qudMedianOnsetMs']} ms range "
            f"{provenance['pauseAlignmentCheck']['qudOnsetRangeMs']}, off "
            f"{provenance['pauseAlignmentCheck']['qudSurahsOffMedianBy150msOrMore']}; MP3Quran median "
            f"{provenance['pauseAlignmentCheck']['mp3quranMedianOnsetMs']} ms, off "
            f"{provenance['pauseAlignmentCheck']['mp3quranSurahsOffMedianBy150msOrMore']}")


# Sets whose manifest and staged files are not under SETS / content/recitations: id -> (manifest, staged folder under --staging).
EXTRA_MANIFESTS = {"husary-warsh-v1": (ROOT / "content" / "warsh" / "audio_manifest.json", Path("warsh") / "audio")}


def manifest_location(staging: Path, set_id: str) -> tuple[Path, Path]:
    if set_id in EXTRA_MANIFESTS:
        manifest, folder = EXTRA_MANIFESTS[set_id]
        return manifest, staging / folder
    return OUTPUT / set_id / "audio_manifest.json", staging / set_id


def seek_durations(staging: Path, set_id: str, write: bool = True) -> list[str]:
    """Every track's `durationMs` in the set's committed manifest = the audio's real length (MPEG frames x samples / sample rate),
    from the staged files. The app seeks a file by it (`SeekScale`: a player's bitrate estimate is wrong when the frames don't
    average the first frame's bitrate, as in Hassan Saleh's 22.05 kHz files). Returns the tracks that changed; writes them when `write`."""
    manifest_path, folder = manifest_location(staging, set_id)
    text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(text)
    changed = []
    for track in manifest["tracks"]:
        info = parse_mp3(folder / track["file"])
        assert info["sizeBytes"] == track["size"], f"{set_id}/{track['file']}: staged file is not the manifest's ({info['sizeBytes']} bytes)"
        duration = round(info["durationSeconds"] * 1000)
        if track.get("durationMs") != duration:
            changed.append(f"{track['file']}: {track.get('durationMs')} -> {duration}")
            track["durationMs"] = duration
    if changed and write:
        out = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + ("\n" if text.endswith("\n") else "")
        manifest_path.write_text(out, encoding="utf-8", newline="\n")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", choices=sorted(SETS) + sorted(EXTRA_MANIFESTS) + ["all"], default="all")
    parser.add_argument("--seek-durations", action="store_true",
                        help="write each track's durationMs (real audio length) into the set's committed manifest from the staged files; run build_recitation_sets.py after")
    parser.add_argument("--staging", type=Path, default=DEFAULT_STAGING)
    parser.add_argument("--download", action="store_true", help="fetch audio, QUD assets and MP3Quran API data first")
    parser.add_argument("--download-only", action="store_true", help="with --download: stop after fetching")
    parser.add_argument("--check", action="store_true", help="re-validate staged files against the committed output")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    set_ids = sorted(SETS) if args.set == "all" else [args.set]
    if args.seek_durations:
        for set_id in (sorted(SETS) + sorted(EXTRA_MANIFESTS) if args.set == "all" else set_ids):
            changed = seek_durations(args.staging, set_id)
            print(f"{set_id}: {len(changed)} durationMs written" + (f" (first: {changed[0]})" if changed else ""))
        return
    if args.download:
        download(args.staging, set_ids)
    if args.download_only:
        return
    failed = False
    for set_id in set_ids:
        outputs, provenance, hard = build(args.staging, set_id, ayah_counts(SETS[set_id]))
        print(summary(set_id, provenance))
        for problem in hard:
            print(f"  FAIL {problem}")
        failed |= bool(hard)
        folder = OUTPUT / set_id
        if args.check:
            for name, text in outputs.items():
                path = folder / name
                if not path.is_file() or path.read_text(encoding="utf-8") != text:
                    print(f"  DIFFERS {path.relative_to(ROOT)}")
                    failed = True
        elif not hard:
            folder.mkdir(parents=True, exist_ok=True)
            for name, text in outputs.items():
                (folder / name).write_text(text, encoding="utf-8", newline="\n")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
