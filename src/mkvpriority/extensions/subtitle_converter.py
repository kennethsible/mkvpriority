import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

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
            attributes = self.parameters[config.toml_path]
        else:
            with open(config.toml_path, 'rb') as f:
                toml_file = tomllib.load(f)
            subtitle_section = toml_file.get('subtitle_profiles', {})
            subtitle_global = subtitle_section.get('global', {})
            attributes = {
                'convert': subtitle_global.get('convert_external_subtitles', False),
                'remove_source': subtitle_global.get('convert_remove_source', False),
                'target_format': subtitle_global.get('convert_target_format', 'srt').lower(),
            }
            self.parameters[config.toml_path] = attributes

        if attributes['convert']:
            target_tracks = [track for track in subtitle_tracks if track.default or track.forced]
            for subtitle_track in target_tracks:
                source_path = self.build_subtitle_path(file_path, subtitle_track)
                if not source_path or not source_path.is_file():
                    continue

                subtitle_ext = source_path.suffix.lstrip('.').lower()
                target_format = attributes['target_format']
                if subtitle_ext == target_format:
                    continue

                target_path = source_path.with_suffix(f'.{target_format}')
                if target_path.is_file():
                    continue

                if self.convert_subtitles(source_path, target_path) and attributes['remove_source']:
                    self.extension_logger.info(f"removing subtitles '{source_path.name}'")
                    source_path.unlink(missing_ok=True)

    def build_subtitle_path(self, file_path: Path, subtitle_track: Track) -> Path | None:
        subtitle_format = subtitle_track.codec.split('/')[-1]
        if not (subtitle_ext := SUBTITLE_EXTENSIONS.get(subtitle_format)):
            return None
        subtitle_suffix = f'.{subtitle_track.language}'
        if subtitle_track.default:
            subtitle_suffix += '.default'
        if subtitle_track.forced:
            subtitle_suffix += '.forced'
        return Path(file_path).with_suffix(f'{subtitle_suffix}.{subtitle_ext}')

    def convert_subtitles(self, source_path: Path, target_path: Path) -> bool:
        if shutil.which('ffmpeg') is None:
            self.extension_logger.warning('cannot convert subtitles (ffmpeg not in PATH)')
            return False
        self.extension_logger.info(f"converting extracted subtitles to '{target_path.name}'")
        try:
            result = subprocess.run(
                ['ffmpeg', '-nostdin', '-y', '-i', str(source_path), str(target_path)],
                capture_output=True,
                check=True,
                text=True,
            )
            if result.stderr:
                self.extension_logger.debug(result.stderr.strip())
            return True
        except subprocess.CalledProcessError as e:
            self.extension_logger.error((e.stderr or str(e)).strip())
            target_path.unlink(missing_ok=True)
        return False
