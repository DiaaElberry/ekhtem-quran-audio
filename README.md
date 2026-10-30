# Ekhtem AlQur'an — Quran Recitation Audio

This repository hosts the Quran recitation audio that the **Ekhtem AlQur'an** app downloads at runtime, so the audio does not have to be bundled inside the app.

| | |
|---|---|
| Reciter | محمود خليل الحصري — Mahmoud Khalil Al-Husary |
| Riwaya | Hafs an Asim (حفص عن عاصم) |
| Coverage | All 114 surahs, one MP3 per surah |
| Source | [TvQuran.com](https://tvquran.com) |

## Where the audio lives

The MP3 files are **not** stored in git. They are published as assets of the GitHub Release
[`husary-hafs-v1`](https://github.com/DiaaElberry/ekhtem-quran-audio/releases/tag/husary-hafs-v1),
named `001.mp3` … `114.mp3` (three-digit, zero-padded surah number).

Download URL pattern:

```
https://github.com/DiaaElberry/ekhtem-quran-audio/releases/download/husary-hafs-v1/NNN.mp3
```

For example, Al-Fatiha is
`https://github.com/DiaaElberry/ekhtem-quran-audio/releases/download/husary-hafs-v1/001.mp3`.

## manifest.json

[`manifest.json`](manifest.json) (also attached to the release) lists every track with its surah number,
file name, size in bytes, and lowercase SHA-256 hash. Clients should build each URL as
`baseUrl + file` and verify the size and hash after download.

## Rights

The recordings come from TvQuran.com. Redistribution rights should be confirmed by the repository
owner with the original source/rights holder before wider distribution.
