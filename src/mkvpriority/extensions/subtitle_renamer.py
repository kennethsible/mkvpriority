import tomllib
from pathlib import Path
from typing import Any

from mkvpriority import Config, Extension, Track
from mkvpriority.main import resolve_language


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
                'rename': subtitle_global.get('rename_external_subtitles', False),
                'language_format': subtitle_global.get('rename_language_format'),
            }
            self.parameters[config.toml_path] = attributes

        if attributes['rename']:
            self.rename_subtitles(file_path, subtitle_tracks, attributes['language_format'])

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

            file_suffix = track.file_path.suffix.lower()
            new_file_name = '.'.join(segments) + file_suffix
            new_file_path = file_path.parent / new_file_name
            if new_file_path != track.file_path:
                if new_file_path.exists():
                    continue

                self.extension_logger.info(
                    f"renaming '{track.file_path.name}' -> '{new_file_name}'"
                )
                track.file_path.rename(new_file_path)
                track.file_path = new_file_path
