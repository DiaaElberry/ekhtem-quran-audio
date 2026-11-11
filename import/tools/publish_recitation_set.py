#!/usr/bin/env python3
"""Publish a prepared recitation set as its GitHub release in DiaaElberry/ekhtem-quran-audio, straight from its source.

Runs on a GitHub Actions machine in the audio repository (import/tools/ there is a copy of this file and of
prepare_recitation_set.py, check_recitation_audio.py; the workflow is .github/workflows/import-sets.yml), so the audio
never passes through a PC:

  1. download the set's 114 files from the server in prepare_recitation_set.SETS (unmodified),
  2. check each file's size and sha256 against the set's committed audio_manifest.json (the bytes that were prepared,
     checked and timed), and stop at the first difference,
  3. create the release <set id> with the 114 files. A release that already exists is never changed: the script stops.

    python -I tools/publish_recitation_set.py --set sudais-hafs-v1 --staging <dir>          # needs gh and GH_TOKEN

Then tools/audio_mirror.py copies the release to Cloudflare R2 (mirror/files.json from `export`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_recitation_set as prs  # noqa: E402

REPO = "DiaaElberry/ekhtem-quran-audio"


def release_exists(tag: str) -> bool:
    return subprocess.run(["gh", "release", "view", tag, "--repo", REPO], capture_output=True).returncode == 0


def notes(set_id: str, spec: dict, manifest: dict) -> tuple[str, str]:
    total = sum(t["size"] for t in manifest["tracks"])
    style = manifest.get("style", "murattal")
    title = f"{spec['reciterEn']} — Hafs, {style} (114 surahs, mp3quran.net)"
    body = "\n".join([
        f"{spec['reciterAr']} — {spec['reciterEn']}",
        "",
        f"Riwayah: Hafs an Asim. Style: {style}. All 114 surahs, one MP3 per surah (`001.mp3` … `114.mp3`), {total:,} bytes.",
        f"Source: mp3quran.net (moshaf {spec['moshaf']}), `{spec['server']}NNN.mp3`, unmodified. mp3quran.net lets any visitor or "
        "developer copy and use its material (its privacy page, Copyrights); this is its statement, not a licence from the reciter "
        "or the recording's producer.",
        f"Ayah timings: MP3Quran timing release {prs.TIMING_RELEASE} (recitation {spec['timingRelease']}), made for exactly these "
        "files (each file's sha256 checked). Sizes and sha256 of every file are in the app's audio_manifest for this set.",
        "",
        "A release is never changed once published: installed app versions download from it.",
    ])
    return title, body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", required=True, choices=sorted(prs.SETS))
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="download and check, create nothing")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    set_id, spec = args.set, prs.SETS[args.set]
    manifest = json.loads((prs.OUTPUT / set_id / "audio_manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == set_id and len(manifest["tracks"]) == 114, "the committed manifest is not this set's 114 tracks"
    if release_exists(set_id):
        sys.exit(f"release {set_id} already exists: releases are never changed")
    folder = args.staging / set_id
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for track in manifest["tracks"]:
        target = folder / track["file"]
        if not (target.is_file() and target.stat().st_size == track["size"]):
            prs._fetch(spec["server"] + track["file"], target, {}, expected_size=track["size"])
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != track["sha256"]:
            sys.exit(f"{set_id}/{track['file']}: sha256 {digest} is not the prepared file's {track['sha256']}")
        files.append(str(target))
        print(f"{set_id}/{track['file']} {track['size']} ok", flush=True)
    title, body = notes(set_id, spec, manifest)
    if args.dry_run:
        print(f"dry run: {len(files)} files checked; would create {set_id}: {title}")
        return
    subprocess.run(["gh", "release", "create", set_id, "--repo", REPO, "--title", title, "--notes", body, *files], check=True)
    print(f"created release {set_id} with {len(files)} files")


if __name__ == "__main__":
    main()
