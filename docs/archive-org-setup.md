# archive.org (Internet Archive) backend

The simplest possible backend: public HTTP, no auth, no API key. Point the
file-provider at one or more Internet Archive item ids and it scans each
item's original-source audio files into the playlist.

## How to use it

1. Find an archive.org item id — the trailing path in the URL:

   ```
   https://archive.org/details/Hawkins_Lectures_transcoded_actual_files
                              └────────────── item id ──────────────┘
   ```

2. Add it to `.env`:

   ```env
   FILE_PROVIDER_ORDER=archive
   ARCHIVE_ORG_ITEMS=Hawkins_Lectures_transcoded_actual_files
   ```

   Multiple items? Comma-separate:

   ```env
   ARCHIVE_ORG_ITEMS=Hawkins_Lectures_transcoded_actual_files,another_item
   ```

3. Start the stack:

   ```bash
   make up
   make refresh-playlist    # force an initial scan
   ```

4. Watch the log — you should see:

   ```
   archive.org: found <N> audio files across 1 items
   ```

## What the provider does

* `GET https://archive.org/metadata/<item_id>` — enumerates every file.
* Filters to `source: original` + audio format (VBR MP3, MP3, FLAC, Ogg, WAV,
  m4a, opus, aiff) so derivatives (spectrograms, waveform PNGs, `.afpk` peak
  files) are skipped.
* Extracts duration from the `length` field (both float-seconds and
  `HH:MM:SS` are supported).
* On play, downloads the file via
  `GET https://archive.org/download/<item_id>/<url-escaped path>` to the
  shared cache and hands the local path to the bot's FFmpeg.

Files download once and then live in `./cache/`. The LRU eviction (default
10 GB, tunable via `CACHE_MAX_GB`) handles disk pressure. If the file gets
evicted and the track is requested again later, we re-download.

## Combining backends

You can also fall back across backends. The provider tries them in
`FILE_PROVIDER_ORDER` sequence:

```env
FILE_PROVIDER_ORDER=archive,telegram,local
ARCHIVE_ORG_ITEMS=Hawkins_Lectures_transcoded_actual_files
TELEGRAM_API_ID=…
TELEGRAM_API_HASH=…
TELEGRAM_CHANNEL_ID=…
LOCAL_MEDIA_PATH=/media
```

Any provider whose config is missing is skipped silently; the others still
populate the playlist.

## Rate limits & etiquette

archive.org's public download endpoints are generous, but you're using
someone else's bandwidth for a 24/7 stream. Please be nice:

* The provider caches every file after the first play — subsequent replays
  never hit archive.org.
* The prefetch thread only downloads the *next* track (one file at a time),
  not the whole playlist.
* Set `CACHE_MAX_GB` big enough to fit the whole item if you can — a 10 GB
  cache holds most reasonable audio collections in full.

If you're going to stream something huge continuously, consider mirroring
the item onto a Telegram channel and using the `telegram` backend instead.
