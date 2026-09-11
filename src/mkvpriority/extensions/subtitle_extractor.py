import json
import subprocess
import tomllib
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from mkvpriority import Config, Extension, Track

SUBTITLE_EXTENSIONS = {'ASS': 'ass', 'SSA': 'ssa', 'UTF8': 'srt', 'WEBVTT': 'vtt'}


class SubtitleExtractor(Extension):
    def __init__(self) -> None:
        super().__init__('subtitle_extractor')
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
            attributes = {'extract': subtitle_global.get('extract_embedded_subtitles', False)}
            self.parameters[config.toml_path] = attributes

        if attributes['extract']:
            target_tracks = [track for track in subtitle_tracks if track.default or track.forced]
            subtitle_paths: list[tuple[int, Path]] = []
            for track in target_tracks:
                subtitle_path = self.build_subtitle_path(file_path, track)
                if subtitle_path and not subtitle_path.is_file():
                    subtitle_paths.append((track.index, subtitle_path))
            if subtitle_paths:
                self.extract_subtitles(file_path, subtitle_paths)

    def build_subtitle_path(self, file_path: Path, subtitle_track: Track) -> Path | None:
        subtitle_format = subtitle_track.codec.split('/')[-1]
        if not (subtitle_ext := SUBTITLE_EXTENSIONS.get(subtitle_format)):
            return None
        if not subtitle_track.default and not subtitle_track.forced:
            return None
        subtitle_suffix = f'.{subtitle_track.language}'
        if subtitle_track.default:
            subtitle_suffix += '.default'
        if subtitle_track.forced:
            subtitle_suffix += '.forced'
        return Path(file_path).with_suffix(f'{subtitle_suffix}.{subtitle_ext}')

    def extract_subtitles(self, file_path: Path, subtitle_paths: list[tuple[int, Path]]) -> None:
        for _, subtitle_path in subtitle_paths:
            self.extension_logger.info(f"extracting embedded subtitles to '{subtitle_path}'")
        arguments = [f'{index}:{subtitle_path}' for index, subtitle_path in subtitle_paths]
        with NamedTemporaryFile('w+', encoding='utf-8', suffix='.json', delete=False) as temp_file:
            json.dump(['tracks', str(file_path), *arguments], temp_file)
            temp_file_path = Path(temp_file.name)

        try:
            result = subprocess.run(
                ['mkvextract', f'@{temp_file_path}'],
                capture_output=True,
                encoding='utf-8',
                check=True,
                text=True,
            )
            self.extension_logger.debug(result.stdout.strip())
        finally:
            temp_file_path.unlink(missing_ok=True)
