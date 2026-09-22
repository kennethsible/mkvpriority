import tomllib
from pathlib import Path
from typing import Any

from mkvpriority import Config, Extension, Track


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
            attributes = {'rename': subtitle_global.get('rename_external_subtitles', False)}
            self.parameters[config.toml_path] = attributes

        if attributes['rename']:
            for track in subtitle_tracks:
                if not track.is_external or track.file_path is None or not track.file_path.exists():
                    continue

                is_default = '.default.' in track.file_path.name
                is_forced = '.forced.' in track.file_path.name
                if track.default == is_default and track.forced == is_forced:
                    continue

                segments = [file_path.stem]
                if track.language and track.language != 'und':
                    segments.append(track.language)
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
