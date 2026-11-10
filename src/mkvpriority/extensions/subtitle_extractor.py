from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from ..config import Config
from ..database import Database
from ..extension import Extension
from ..types import Track

SUBTITLE_EXTENSIONS = {'ASS': 'ass', 'SSA': 'ssa', 'UTF8': 'srt'}

mkvextract_logger = logging.getLogger('mkvextract')


@dataclass
class Parameters:
    extract_embedded_subtitles: bool = False

    @classmethod
    def from_dict(cls, section: dict[str, Any]) -> Parameters:
        valid_parameters = {field.name for field in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in section.items() if k in valid_parameters})


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
        database: Database | None = None,
        *,
        simulate: bool = False,
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

        if not parameters.extract_embedded_subtitles:
            return

        target_tracks = [
            track
            for track in subtitle_tracks
            if not track.is_external and (track.default or track.forced)
        ]

        subtitle_paths: list[tuple[Track, Path]] = []
        for track in target_tracks:
            subtitle_path = self.build_subtitle_path(file_path, track)
            if subtitle_path and not subtitle_path.is_file():
                subtitle_paths.append((track, subtitle_path))

        if subtitle_paths:
            self.extract_subtitles(file_path, subtitle_paths)

    def build_subtitle_path(self, file_path: Path, track: Track) -> Path | None:
        subtitle_format = track.codec.split('/')[-1]
        if not (extension := SUBTITLE_EXTENSIONS.get(subtitle_format)):
            return None
        if not track.default and not track.forced:
            return None

        suffix = f'.{track.language}'
        if track.default:
            suffix += '.default'
        if track.forced:
            suffix += '.forced'
        return Path(file_path).with_suffix(f'{suffix}.{extension}')

    def extract_subtitles(self, file_path: Path, subtitle_paths: list[tuple[Track, Path]]) -> None:
        path_args: list[str] = []
        for track, subtitle_path in subtitle_paths:
            subtitle_suffix = subtitle_path.name[len(file_path.stem) :]
            path_args.append(f'{track.index}:{subtitle_path}')
            track_str = f"{track.index} '{track.name}'" if track.name else str(track.index)
            self.extension_logger.info(f"extracting track {track_str} to '{subtitle_suffix}'")

        arguments = ['tracks', str(file_path), *path_args]
        mkvextract_logger.debug(' '.join(arguments))
        with NamedTemporaryFile('w+', encoding='utf-8', suffix='.json', delete=False) as temp_file:
            json.dump(arguments, temp_file)
            temp_file_path = Path(temp_file.name)

        try:
            result = subprocess.run(
                ['mkvextract', f'@{temp_file_path}'],
                capture_output=True,
                encoding='utf-8',
                check=True,
                text=True,
            )
            mkvextract_logger.debug(result.stdout.strip())
        except subprocess.CalledProcessError as e:
            mkvextract_logger.error((e.stderr or e.stdout or str(e)).strip())
        finally:
            temp_file_path.unlink(missing_ok=True)
