import tomllib
from pathlib import Path
from typing import Any

import pysubs2
from pysubs2.exceptions import Pysubs2Error

from mkvpriority import Config, Extension, Track

SUBTITLE_EXTENSIONS = {'ASS': 'ass', 'SSA': 'ssa', 'UTF8': 'srt', 'WEBVTT': 'vtt'}


class SubtitleConverter(Extension):
    def __init__(self) -> None:
        super().__init__('subtitle_converter')
        self.parameters: dict[str, Any] = {}

    def process_file(
        self,
        file_path: Path,
        video_tracks: list[Track],
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        config: Config,
        dry_run: bool = False,
    ) -> None:
        if not subtitle_tracks:
            return

        if config.toml_path in self.parameters:
            params = self.parameters[config.toml_path]
        else:
            with open(config.toml_path, 'rb') as f:
                toml_file = tomllib.load(f)
            params = {
                'convert': toml_file.get('convert_external_subtitles', False),
                'remove_source': toml_file.get('convert_remove_source', False),
                'target_format': toml_file.get('convert_target_format', 'srt').lower(),
            }
            self.parameters[config.toml_path] = params

        if params['convert']:
            subtitle_track = max(subtitle_tracks, key=lambda track: track.score)
            source_path = self.build_subtitle_path(file_path, subtitle_track)
            if source_path and source_path.is_file():
                subtitle_ext = source_path.suffix.lstrip('.').lower()
                target_format = params['target_format']
                if subtitle_ext != target_format:
                    target_path = source_path.with_suffix(f'.{target_format}')
                    if not target_path.is_file():
                        self.convert_subtitles(source_path, target_path)
                        if params['remove_source'] and target_path.is_file():
                            self.extension_logger.info(f"removing subtitles '{source_path.name}'")
                            source_path.unlink(missing_ok=True)

    def build_subtitle_path(self, file_path: Path, subtitle_track: Track) -> Path | None:
        if not subtitle_track.codec.startswith('S_TEXT/'):
            return None
        codec_name = subtitle_track.codec.split('/')[-1]
        if codec_name not in SUBTITLE_EXTENSIONS:
            return None
        subtitle_ext = SUBTITLE_EXTENSIONS[codec_name]
        subtitle_suffix = f'.{subtitle_track.language}'
        if subtitle_track.default:
            subtitle_suffix += '.default'
        if subtitle_track.forced:
            subtitle_suffix += '.forced'
        return Path(file_path).with_suffix(f'{subtitle_suffix}.{subtitle_ext}')

    def convert_subtitles(self, source_path: Path, target_path: Path) -> None:
        self.extension_logger.info(f"converting extracted subtitles to '{target_path.name}'")

        # result = subprocess.run(
        #     ['ffmpeg', '-y', '-i', str(source_path), str(target_path)],
        #     capture_output=True,
        #     encoding='utf-8',
        #     check=True,
        #     text=True,
        # )
        # self.extension_logger.debug(result.stdout.strip())

        try:
            subs = pysubs2.load(str(source_path), encoding='utf-8')
            subs.save(str(target_path), encoding='utf-8')
        except (OSError, UnicodeError, Pysubs2Error) as e:
            self.extension_logger.error(str(e))
