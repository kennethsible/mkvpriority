# MKVPriority - MKV Track Prioritizer

A fast, configuration-driven tool designed to prioritize audio and subtitle tracks in MKV files. It was originally created to prioritize Japanese audio tracks with ASS subtitles for watching anime.

> [!IMPORTANT]
> MKVPriority **modifies track flags in-place** using `mkvpropedit` (no remuxing, non-destructive), allowing media players to automatically select the best audio and subtitle tracks according to your preferences.

## Features

- Assigns configurable priority scores to audio and subtitle tracks (similar to custom formats in Radarr/Sonarr)
- Automatically sets default/forced flags for the highest priority tracks (e.g., Japanese audio and ASS subtitles)
- Deprioritizes unwanted audio and subtitle tracks (e.g., English dubs, commentary tracks, signs/songs)

## CLI Usage

[`mkvtoolnix`](https://mkvtoolnix.download/) must be installed and available in `PATH` for `mkvpropedit` and `mkvmerge` (optional).

```bash
usage: main.py [-h] [--config FILE_PATH] [--dry-run] [--reorder] [--strip] [input_dirs ...]

positional arguments:
  input_dirs

options:
  --config FILE_PATH
  --dry-run           leaves tracks unchanged
  --reorder           reorders tracks by score
  --strip             strips unwanted tracks
````

> [!WARNING]
> `--reorder` and `--strip` are optional arguments that use `mkvmerge` to reorder and strip tracks, respectively. `mkvmerge`, in contrast to `mkvpropedit`, outputs a remux instead of modifying in-place since reordering and/or stripping tracks requires changing the container format.

## Docker Image

A Docker image is provided for quick deployment and to simplify the installation process.

```bash
docker run --rm -v /path/to/media:/media ghcr.io/kennethsible/mkvpriority /media
```

> [!NOTE]
> This will use the default `config.toml` inside the Docker image.

### Use a Custom Config

To override the default config, use a bind mount:

```bash
docker run --rm \
  -v /path/to/media:/media \
  -v /path/to/custom/config.toml:/app/config.toml \
  ghcr.io/kennethsible/mkvpriority /media
```

> [!NOTE]
> `/media` can be included in `config.toml` or passed as an argument.

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

## Acknowledgments

This Python script was originally adapted from a Ruby script available on GitHub ([Andy2244/subby](https://github.com/Andy2244/subby)), updated for my specific use cases, and packaged into a convenient Docker image.
