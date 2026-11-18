#!/usr/bin/env python3
"""Fetch the files a speech measurement needs, on a GitHub Actions machine (decision 258: the owner's rule that recitation audio
goes from its source to GitHub and never through a PC).

Run by the audio repository's "Import recitation sets" workflow in `measure` mode (import/tools/ there holds a copy of this
file). Reads the environment: SET (a set id of prepare_recitation_set.SETS), WINDOWS (space-separated windows as
tools/time_surahs_by_speech.py takes them, SURAH:FIRST-LAST@FROMMS-TOMS) and AUDIO (the folder to fill). Each surah a window
names is downloaded unmodified from the set's server and checked against the set's committed audio_manifest.json (size and
sha256); the first difference stops the run. Standard library only; the files are untrusted downloads and are never executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_recitation_set as prs  # noqa: E402


def main() -> None:
    set_id = os.environ["SET"]
    windows = os.environ.get("WINDOWS", "").split()
    audio = Path(os.environ["AUDIO"])
    if set_id not in prs.SETS or not windows:
        sys.exit(f"unknown set {set_id!r} or no windows")
    manifest = json.loads((prs.OUTPUT / set_id / "audio_manifest.json").read_text(encoding="utf-8"))
    tracks = {t["surah"]: t for t in manifest["tracks"]}
    surahs = sorted({int(w.split(":", 1)[0]) for w in windows})
    audio.mkdir(parents=True, exist_ok=True)
    for surah in surahs:
        track = tracks[surah]
        url = f"{prs.SETS[set_id]['server']}{surah:03}.mp3"
        target = audio / f"{surah:03}.mp3"
        request = urllib.request.Request(url, headers={"User-Agent": prs.USER_AGENT})
        with urllib.request.urlopen(request, timeout=300) as response, target.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        data = target.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != track["size"] or digest != track["sha256"]:
            sys.exit(f"{url}: {len(data)} bytes, sha256 {digest}; the manifest has {track['size']} bytes, {track['sha256']}")
        print(f"{target.name}: {len(data)} bytes, sha256 as in the manifest")


if __name__ == "__main__":
    main()
