from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mkvpriority import Config, Database, Extension, Track
from mkvpriority.main import resolve_language


@dataclass
class Parameters:
    rename_external_subtitles: bool = False
    rename_language_format: str | None = None

    @classmethod
    def from_dict(cls, section: dict[str, Any]) -> Parameters:
        valid_parameters = {field.name for field in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in section.items() if k in valid_parameters})


class SubtitleRenamer(Extension):
    def __init__(self) -> None:
        super().__init__('subtitle_renamer')
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

        if parameters.rename_external_subtitles:
            self.rename_subtitles(file_path, subtitle_tracks, parameters.rename_language_format)

    def rename_subtitles(
        self, file_path: Path, subtitle_tracks: list[Track], language_format: str | None = None
    ) -> None:
        for track in subtitle_tracks:
            if not track.is_external or track.file_path is None or not track.file_path.exists():
                continue
            if not track.file_path.stem.startswith(file_path.stem):
                continue

            file_infix = track.file_path.stem[len(file_path.stem) :]
            tokens = [segment.lower() for segment in file_infix.split('.') if segment]
            is_default, is_forced = 'default' in tokens, 'forced' in tokens
            if track.default == is_default and track.forced == is_forced:
                continue

            segments = [file_path.stem]
            if track.language and track.language != 'und':
                normalized_language = (
                    resolve_language(track.language, language_format)
                    if language_format
                    else track.language
                )
                if normalized_language is None and language_format:
                    self.extension_logger.warning(
                        f"unrecognized language format '{language_format}'"
                    )
                segments.append(normalized_language or track.language)
            if track.default:
                segments.append('default')
            if track.forced:
                segments.append('forced')
            if track.name:
                segments.append(track.name)

            old_file_name = track.file_path.name
            file_suffix = track.file_path.suffix.lower()
            new_file_name = '.'.join(segments) + file_suffix
            new_file_path = file_path.parent / new_file_name
            if new_file_path != track.file_path:
                if new_file_path.exists():
                    continue

                old_suffix = old_file_name[len(file_path.stem) :]
                new_suffix = new_file_name[len(file_path.stem) :]
                self.extension_logger.info(
                    f"renaming external subtitles '{old_suffix}' -> '{new_suffix}'"
                )
                track.file_path.rename(new_file_path)
                track.file_path = new_file_path
