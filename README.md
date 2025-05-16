# MKVPriority - MKV Track Prioritizer

**MKVPriority** assigns configurable priority scores to audio and subtitle tracks, similar to custom formats in Radarr/Sonarr. MKV flags, such as default and forced, are automatically set for the highest-priority tracks (e.g., 5.1 surround and ASS subtitles), while lower-priority tracks (e.g., stereo audio and PGS subtitles) are deprioritized.

> [!IMPORTANT]
> MKVPriority modifies track flags in place using `mkvpropedit` (**no remuxing**), allowing media players to automatically select the best audio and subtitle tracks according to your preferences.

## Features

- Assigns **configurable priority scores** to audio and subtitle tracks (similar to **custom formats** in Radarr/Sonarr)
- Automatically sets **default/forced flags** for the highest priority tracks (e.g., Japanese audio and ASS subtitles)
- Deprioritizes **unwanted audio and subtitle tracks** (e.g., English dubs, commentary tracks, signs/songs)
- Periodically scans your media library and processes new MKV files (using a cron job with **archive mode**)

## CLI Usage

[`mkvtoolnix`](https://mkvtoolnix.download/) must be installed on your system for `mkvpropedit` (unless you are using the Docker image).

```text
usage: main.py [-h] [-c FILE_PATH] [-a FILE_PATH] [-n] [-q] [-v] [-r] [-s] [input_dirs ...]

positional arguments:
  input_dirs

options:
  -c FILE_PATH, --config FILE_PATH
  -a FILE_PATH, --archive FILE_PATH
  -n, --dry-run         leave tracks unchanged
  -q, --quiet           suppress standard output
  -v, --verbose         print detailed information
  -r, --reorder         reorder tracks by score
  -s, --strip           remove unwanted tracks
```

> [!WARNING]
> `--reorder` and `--strip` are optional features that use `mkvmerge`, which, unlike `mkvpropedit`, outputs a remux because reordering and/or stripping tracks requires changing the container format.

## Docker Image

A Docker image is provided to simplify the installation process and enable quick deployment.

```bash
docker run --rm -v /path/to/media:/media ghcr.io/kennethsible/mkvpriority /media
```

> [!NOTE]
> This will use the default `config.toml` inside the image. See below for using a custom config.

### Use a Custom Config

You can specify your own preferences by creating a custom TOML config that defines track filters by name and assigns scores by property. To override the default config, use a bind mount:

```bash
docker run --rm \
  -v /path/to/media:/media \
  -v /path/to/config/config.toml:/app/config.toml \
  ghcr.io/kennethsible/mkvpriority /media
```

> [!NOTE]
> Media directories can be included in `config.toml` or specified as command-line arguments.

### Use an Archive Database

You can periodically process your media library using a cron job and an archive database. To keep track of processed files, create an `archive.db` file and use a bind mount:

```bash
docker run --rm \
  -v /path/to/media:/media \
  -v /path/to/database/archive.db:/app/archive.db \
  ghcr.io/kennethsible/mkvpriority /media
```

## Configuration (`config.toml`)

A single TOML file controls all behavior by assigning priority scores to track properties, such as languages and codecs, and by defining custom filters for track names, such as signs and songs.

### Example (Audio Codecs)

```toml
[audio_codecs]
A_DTSHD_MA = 10 # DTS-HD Master Audio
A_TRUEHD = 9    # Dolby TrueHD
A_FLAC = 8      # Free Lossless Audio Codec
A_DTS = 7       # DTS
A_OPUS = 6      # Opus
A_EAC3 = 5      # Dolby Digital Plus
A_AC3 = 4       # Dolby Digital (AC-3)
A_AAC = 3       # Advanced Audio Coding
"A_MPEG/L3" = 2 # MP3 (MPEG Layer III)
```

## Limitations

MKVPriority avoids remuxing by using `mkvpropedit`, but this still affects [hardlinks](https://trash-guides.info/File-and-Folder-Structure/Hardlinks-and-Instant-Moves/) since the metadata is modified.

## Acknowledgments

I've been using the excellent script from [TheCaptain989/radarr-striptracks](https://github.com/TheCaptain989/radarr-striptracks) to remove unwanted audio and subtitle tracks. However, I've always wanted a solution that automatically sets my preferred tracks as default/forced and doesn't require remuxing. After searching GitHub, I found an unmaintained project ([Andy2244/subby](https://github.com/Andy2244/subby)) that I decided to revive and package into a Docker image.
