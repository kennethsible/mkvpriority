<div align="center">
  <img alt="MKVPriority Banner" src="images/mkvpriority_banner.svg" width="600">
</div>
<p align="center">
<img src="https://github.com/kennethsible/mkvpriority/actions/workflows/publish.yaml/badge.svg" alt="MKVPriority Release" />
<img src="https://github.com/kennethsible/mkvpriority/actions/workflows/pytest.yaml/badge.svg" alt="MKVPriority CI">
</p>

**MKVPriority** assigns configurable priority scores to audio and subtitle tracks, similar to custom formats in Radarr/Sonarr. MKV flags, such as default and forced, are automatically set for the highest-priority tracks (e.g., 5.1 surround and ASS subtitles), while lower-priority tracks (e.g., stereo audio and PGS subtitles) are deprioritized.

> [!IMPORTANT]
> MKVPriority modifies track flags in place using `mkvpropedit` (**no remuxing**), allowing media players to automatically select the best audio and subtitle tracks according to your preferences.

## Features

- Assigns **configurable priority scores** to audio and subtitle tracks (similar to **custom formats** in Radarr/Sonarr)
- Automatically sets **default/forced flags** for the highest priority tracks (e.g., Japanese audio and ASS subtitles)
- Deprioritizes **unwanted audio and subtitle tracks** (e.g., English dubs, commentary tracks, signs/songs)
- Periodically scans your media library using a **cron schedule** and processes new MKV files with a database
- Integrates with Radarr and Sonarr using a **custom script** to process new MKV files as they are imported
- Supports extension modules for optional, user-defined **post-processors**, allowing for edge-case handling

## Docker Image

A Docker image is provided to simplify the installation process and enable quick deployment.

```bash
docker run --rm -v /path/to/media:/media ghcr.io/kennethsible/mkvpriority /media
```

### Use a Custom Config

You can specify your own preferences by creating a custom TOML config that defines track filters by name and assigns scores by property. To override the default config, use a bind mount:

```bash
docker run --rm -u ${PUID}:${PGID} \
  -v /path/to/media:/media \
  -v /path/to/mkvpriority/config:/config \
  ghcr.io/kennethsible/mkvpriority /media \
  --config /config/custom.toml
```

> [!IMPORTANT]
> Before starting the Docker container, you should pre-create the config folder on the host. Otherwise, Docker will create the folder as the root user, causing Python to raise a `PermissionError`.

### Use an Archive Database

You can periodically process your media library using a cron job and an archive database. To keep track of processed files, create an `archive.db` file and use a bind mount:

```bash
docker run --rm -u ${PUID}:${PGID} \
  -v /path/to/media:/media \
  -v /path/to/mkvpriority/config:/config \
  ghcr.io/kennethsible/mkvpriority /media \
  --archive /config/archive.db
```

## Radarr/Sonarr Integration

You can process new MKV files as they are imported into Radarr/Sonarr by adding the custom script `mkvpriority.sh` and selecting 'On File Import' and 'On File Upgrade'. In order for Radarr/Sonarr to recognize the custom script, it must be visible inside the container.

> [!NOTE]
> To add a custom script to Radarr/Sonarr, go to Settings > Connect > Add Connection > Custom Script.

```yaml
mkvpriority:
  image: ghcr.io/kennethsible/mkvpriority
  container_name: mkvpriority
  user: ${PUID}:${PGID}
  environment:
    WEBHOOK_PORT: '8080'
    MKVPRIORITY_ARGS: >
      --archive /config/archive.db
  volumes:
    - /path/to/media:/media
    - /path/to/mkvpriority/config:/config
  restart: unless-stopped
```

> [!IMPORTANT]
> If you are not using "mkvpriority" as the name of your container, you will need to update it in the custom script.
> Also, verify that the mount point for your media directory in MKVPriority is the same as the one used by Radarr/Sonarr.

### Use Multiple Configs

MKVPriority supports multiple, tag-based configs that can be customized to match the tagging system used in Radarr/Sonarr. For example, you can create a separate config for anime by adding an `anime` tag in Radarr/Sonarr either manually or via auto-tagging. Then, append the `::anime` tag to the config path in the MKVPriority arguments.

```yaml
mkvpriority:
  image: ghcr.io/kennethsible/mkvpriority
  container_name: mkvpriority
  user: ${PUID}:${PGID}
  environment:
    WEBHOOK_PORT: '8080'
    MKVPRIORITY_ARGS: >
      --config /config/anime.toml::anime
      --archive /config/archive.db
  volumes:
    - /path/to/media:/media
    - /path/to/mkvpriority/config:/config
  restart: unless-stopped
```

> [!IMPORTANT]
> In Radarr/Sonarr, a given movie or show can have multiple tags. However, MKVPriority only uses the first tag in alphabetical order. Therefore, you may need to create new tags specifically for MKVPriority.

### Original Audio Language

MKVPriority supports using the Radarr/Sonarr API to identify the original language of a movie or series. You can assign priority scores to the language code `org` (original) by configuring API access with environment variables.

> [!NOTE]
> To generate an API key for Radarr/Sonarr, go to Settings > General > Security > API Key.

```yaml
mkvpriority:
  image: ghcr.io/kennethsible/mkvpriority
  container_name: mkvpriority
  user: ${PUID}:${PGID}
  environment:
    WEBHOOK_PORT: '8080'
    MKVPRIORITY_ARGS: >
      --archive /config/archive.db
    SONARR_URL: http://sonarr:8989
    SONARR_API_KEY: ${SONARR_API_KEY}
    RADARR_URL: http://radarr:7878
    RADARR_API_KEY: ${RADARR_API_KEY}
  volumes:
    - /path/to/media:/media
    - /path/to/mkvpriority/config:/config
  restart: unless-stopped
```

### Docker Secrets (API Keys)

MKVPriority supports Docker secrets to keep your API keys secure. To use secrets:

1. Create a text file on your host machine (one per key) containing your API key.
2. In your compose file, append `_FILE` to the environment variable for each API key.
3. Set each variable to the secret's path inside the container (e.g., `/run/secrets`).

Here is an example configuration showing how to use secrets with MKVPriority:

```yaml
mkvpriority:
  image: ghcr.io/kennethsible/mkvpriority
  container_name: mkvpriority
  user: ${PUID}:${PGID}
  environment:
    WEBHOOK_PORT: '8080'
    MKVPRIORITY_ARGS: >
      --archive /config/archive.db
    SONARR_URL: http://sonarr:8989
    SONARR_API_KEY_FILE: /run/secrets/sonarr_api_key
    RADARR_URL: http://radarr:7878
    RADARR_API_KEY_FILE: /run/secrets/radarr_api_key
  volumes:
    - /path/to/media:/media
    - /path/to/mkvpriority/config:/config
  secrets:
      - sonarr_api_key
      - radarr_api_key
  restart: unless-stopped

secrets:
  sonarr_api_key:
    file: ${SECRETS_DIR}/sonarr_api_key.txt
  radarr_api_key:
    file: ${SECRETS_DIR}/radarr_api_key.txt
```

## Cron Scheduler

You can use the built-in cron scheduler to periodically scan your media library and process MKV files. When paired with an archive database, MKVPriority will only process new files with each scan.

```yaml
mkvpriority:
  image: ghcr.io/kennethsible/mkvpriority
  container_name: mkvpriority
  user: ${PUID}:${PGID}
  environment:
    TZ: "America/New_York"
    CRON_SCHEDULE: "0 0 * * *"
    CRON_TARGET_PATHS: /media
    MKVPRIORITY_ARGS: >
      --archive /config/archive.db
  volumes:
    - /path/to/media:/media
    - /path/to/mkvpriority/config:/config
  restart: unless-stopped
```

> [!NOTE]
> MKVPriority supports [non-standard macros](https://en.wikipedia.org/wiki/Cron#Nonstandard_predefined_scheduling_definitions) for cron expressions, such as `@daily` and `@hourly`.

## CLI Usage

[`mkvtoolnix`](https://mkvtoolnix.download/) must be installed on your system for `mkvpropedit` (unless you are using the Docker image).

```text
usage: mkvpriority [-h] [-c TOML_PATH[::TAG]] [-a DB_PATH] [-i MODULE_NAME] [-v] [-x] [-q] [-p] [-n] [-r] [INPUT_PATH[::TAG] ...]

positional arguments:
  INPUT_PATH[::TAG]     files or directories

options:
  -c, --config TOML_PATH[::TAG]
  -a, --archive DB_PATH
  -i, --include MODULE_NAME
                        include extension module
  -v, --verbose         inspect track metadata
  -x, --debug           show mkvtoolnix output
  -q, --quiet           suppress logging output
  -p, --prune           prune database entries
  -n, --dry-run         simulate track changes
  -r, --restore         restore original tracks
```

### Python Package

To use MKVPriority without Docker, run the following `pip` command:

```bash
pip install 'git+ssh://git@github.com/kennethsible/mkvpriority.git'
```

## Extension Modules

MKVPriority supports user-defined extension modules for optional post-processing. This feature is designed to handle complex library edge cases, integrate your workflow with external tools, and provide an open-ended automation framework, such as automatically extracting embedded subtitles or dynamically restyling subtitle fonts.

### Example: Subtitle Extractor

You can use the `subtitle_extractor` extension to extract embedded subtitles with the highest priority score. This may result in smoother playback if your media player doesn't support certain subtitle formats. For example, if the player needs to transcode or burn in embedded subtitles, it must first demux and process the entire MKV container.

```text
Naming Format: {basename}.{language}.{default,forced}.{srt,ass}
```

> [!NOTE]
> To avoid changing internal track flags and *only* use external subtitles, use the subtitle extractor with the `--dry-run` argument since subtitle extraction still runs during a dry run, which only prevents changes to the MKV container.

### Creating Extensions

You can easily write your own post-processing scripts to handle custom logic.

1. Create a Python script (e.g., `my_extension.py`) inside any of the following:
   - current working directory (recommend for local development)
   - `~/.config/mkvpriority/extensions` (recommended for `pip`)
   - `/config/extensions` (recommended for Docker)
2. Import the `Extension` class and implement the `process_file` method:

   ```python
   class Extension(ABC):
    def __init__(self, extension_name: str | None = None):
        name = extension_name or self.__class__.__name__
        self.extension_logger = logging.getLogger(name)

    @abstractmethod
    def process_file(
        self,
        file_path: Path,
        video_tracks: list[Track],
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        config: Config,
        dry_run: bool = False,
    ) -> None:
        raise NotImplementedError
   ```

3. Use `-i/--include` with the script name (without the `.py` extension):

    ```bash
    mkvpriority -i my_extension
    ```

> [!NOTE]
> Check the `extensions` folder in the GitHub repository for example scripts. In addition to the **subtitle extractor**, there's also a **subtitle restyler** that lets you define style overrides in your config file, and there's a **multiplexer** that lets you strip tracks for languages not included in your config file (as well as reorder tracks by priority scores).

## TOML Configuration

All behavior is configured through TOML files, which assign priority scores to track properties, such as languages and codecs, and define custom filters for track names, such as "signs" and "songs."

### Example: Subtitle Codecs

```toml
[subtitle_codecs]
"S_TEXT/ASS" = 30    # Stylized (Advanced SubStationAlpha)
"S_TEXT/SSA" = 30    # Legacy Stylized (SubStationAlpha)
"S_TEXT/UTF8" = 20   # Plain Text (SubRip/SRT)
"S_TEXT/WEBVTT" = 20 # Web-Based Video Text (Used in Streaming)
"S_HDMV/PGS" = 10    # Image-Based (Used in Blu-rays)
S_VOBSUB = 10        # Legacy Image-Based (Used in DVDs)
```

## Hardlinks Limitation

MKVPriority avoids remuxing by using `mkvpropedit`, but this still affects hardlinks since the metadata is modified. To avoid breaking hardlinks, use the subtitle extractor with the `--dry-run` argument (see [Subtitle Extractor](#example-subtitle-extractor)).
