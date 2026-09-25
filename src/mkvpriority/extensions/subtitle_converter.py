from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pysubs2
from pysubs2.exceptions import Pysubs2Error

from mkvpriority import Config, Database, Extension, Track

SUBTITLE_EXTENSIONS = {'ASS': 'ass', 'SSA': 'ssa', 'UTF8': 'srt'}


@dataclass
class Parameters:
    convert_external_subtitles: bool = False
    convert_target_format: str = 'srt'
    convert_remove_source: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.convert_target_format, str):
            self.convert_target_format = self.convert_target_format.lower()

    @classmethod
    def from_dict(cls, section: dict[str, Any]) -> Parameters:
        valid_parameters = {field.name for field in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in section.items() if k in valid_parameters})


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
        database: Database | None = None,
        dry_run: bool = False,
    ) -> None:
        if not subtitle_tracks:
            return

        if config.toml_path in self.parameters:
            parameters = self.parameters[config.toml_path]
        else:
            with open(config.toml_path, 'rb') as f:
                toml_file = tomllib.load(f)
            subtitle_section = toml_file.get('subtitle_profiles', {})
            subtitle_global = subtitle_section.get('global', {})
            parameters = Parameters.from_dict(subtitle_global)
            self.parameters[config.toml_path] = parameters

        if parameters.convert_external_subtitles:
            target_tracks = [track for track in subtitle_tracks if track.default or track.forced]
            for subtitle_track in target_tracks:
                source_path = self.build_subtitle_path(file_path, subtitle_track)
                if not source_path or not source_path.is_file():
                    continue

                subtitle_ext = source_path.suffix.lstrip('.').lower()
                target_format = parameters.convert_target_format
                if subtitle_ext == target_format:
                    continue

                target_path = source_path.with_suffix(f'.{target_format}')
                if target_path.is_file():
                    continue

                remove_source = parameters.convert_remove_source
                if self.convert_subtitles(file_path, source_path, target_path) and remove_source:
                    source_suffix = source_path.name[len(file_path.stem) :]
                    self.extension_logger.info(f"removing external subtitles '{source_suffix}'")
                    source_path.unlink(missing_ok=True)
                    if subtitle_track.is_external:
                        subtitle_track.file_path = target_path
                        subtitle_track.codec = f'S_TEXT/{target_format.upper()}'

    def build_subtitle_path(self, file_path: Path, subtitle_track: Track) -> Path | None:
        if subtitle_track.is_external:
            return subtitle_track.file_path

        subtitle_format = subtitle_track.codec.split('/')[-1]
        if not (subtitle_ext := SUBTITLE_EXTENSIONS.get(subtitle_format)):
            return None
        subtitle_suffix = f'.{subtitle_track.language}'
        if subtitle_track.default:
            subtitle_suffix += '.default'
        if subtitle_track.forced:
            subtitle_suffix += '.forced'
        return Path(file_path).with_suffix(f'{subtitle_suffix}.{subtitle_ext}')

    def convert_subtitles(self, file_path: Path, source_path: Path, target_path: Path) -> bool:
        subtitle_suffix = target_path.name[len(file_path.stem) :]
        self.extension_logger.info(f"converting external subtitles to '{subtitle_suffix}'")
        try:
            subtitle_file = pysubs2.load(str(source_path))
            subtitle_file.info['PlayResX'] = '1920'
            subtitle_file.info['PlayResY'] = '1080'
            subtitle_file.save(str(target_path))
            return True
        except (OSError, UnicodeError, Pysubs2Error) as e:
            self.extension_logger.error(str(e))
            target_path.unlink(missing_ok=True)
        return False
