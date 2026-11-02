# Ekhtem AlQur'an — Quran Recitation Audio

This repository hosts the Quran recitation audio that the **Ekhtem AlQur'an** app downloads at runtime, so the audio does not have to be bundled inside the app.

| | |
|---|---|
| Reciter | محمود خليل الحصري — Mahmoud Khalil Al-Husary |
| Riwaya | Hafs an Asim (حفص عن عاصم), murattal |
| Coverage | All 114 surahs, one MP3 per surah |
| Source | [mp3quran.net](https://mp3quran.net) (release `husary-hafs-v2`) |

## Where the audio lives

The MP3 files are **not** stored in git. They are published as assets of GitHub Releases, named
`001.mp3` … `114.mp3` (three-digit, zero-padded surah number). A release is never changed once
published, because installed app versions download from it.

### husary-hafs-v2 (current)

[`husary-hafs-v2`](https://github.com/DiaaElberry/ekhtem-quran-audio/releases/tag/husary-hafs-v2) holds
mp3quran.net's copy of the recording, unmodified, as served at
`https://cdn.mp3quran.net/audio/mahmoud-husary/r1/NNN.mp3` on 2 November 2026: 128 kbps CBR MP3,
2,470,339,875 bytes in total.

```
https://github.com/DiaaElberry/ekhtem-quran-audio/releases/download/husary-hafs-v2/NNN.mp3
```

For example, Al-Fatiha is
`https://github.com/DiaaElberry/ekhtem-quran-audio/releases/download/husary-hafs-v2/001.mp3`.

These are the files that the ayah timings of QUD Technologies'
[Quranic Universal Audio](https://github.com/QUD-Technologies/quranic-universal-audio) (CC BY 4.0) were made for,
so the timings apply without an offset.

### husary-hafs-v1 (earlier)

[`husary-hafs-v1`](https://github.com/DiaaElberry/ekhtem-quran-audio/releases/tag/husary-hafs-v1) holds the
TvQuran.com copy of the same recording (64 kbps), with surahs 21 and 31 as `021-v2.mp3` and `031-v2.mp3`
(mp3quran.net's files re-encoded to that format). App versions up to 2.0.0 use it, so it stays published
unchanged. Its `manifest.json` predates the replacement of surahs 21 and 31 and still lists `021.mp3` and `031.mp3`;
the app's own catalog has the right names.

## manifest.json

Each release has a `manifest.json` that lists every track with its surah number, file name, size in bytes
and lowercase SHA-256 hash. Clients should build each URL as `baseUrl + file` and verify the size and hash
after download. [`manifest.json`](manifest.json) at the root of this repository is v1's (unchanged).

## Rights

- `husary-hafs-v2`: mp3quran.net allows any visitor or developer to copy and use its material (see the
  "Copyrights" section of its privacy page). The files are published unmodified and used non-commercially in a
  free app without ads.
- `husary-hafs-v1`: TvQuran.com states "All rights reserved". This is why the app moved to mp3quran.net's copy.
