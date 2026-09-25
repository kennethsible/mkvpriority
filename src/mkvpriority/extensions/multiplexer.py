from __future__ import annotations

import dataclasses
import itertools
import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from mkvpriority import Config, Extension, Track
from mkvpriority.main import mkvmerge_logger


@dataclass
class Parameters:
    multiplex_container: bool = False
    remove_original_container: bool = True
    remove_external_subtitles: bool = False
    mkvmerge_arguments: list[str] = dataclasses.field(default_factory=list)
    strip_unscored_tracks: bool = False
    strip_audio_profile: str | None = None
    strip_subtitle_profile: str | None = None
    order_tracks_by_score: bool = False
    order_audio_profile: str | None = None
    order_subtitle_profile: str | None = None

    @classmethod
    def from_dict(cls, section: dict[str, Any]) -> Parameters:
        valid_parameters = {field.name for field in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in section.items() if k in valid_parameters})


class Multiplexer(Extension):
    def __init__(self) -> None:
        super().__init__('multiplexer')
        self.parameters: dict[str, Parameters] = {}

    def process_file(
        self,
        file_path: Path,
        video_tracks: list[Track],
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        config: Config,
        dry_run: bool = False,
    ) -> None:
        if config.toml_path in self.parameters:
            parameters = self.parameters[config.toml_path]
        else:
            with open(config.toml_path, 'rb') as f:
                toml_file = tomllib.load(f)
            multiplexer_section = toml_file.get('multiplexer', {})
            parameters = Parameters.from_dict(multiplexer_section)
            self.parameters[config.toml_path] = parameters

        if parameters.multiplex_container:
            self.multiplex_file(
                file_path, video_tracks, audio_tracks, subtitle_tracks, parameters, dry_run
            )

    @staticmethod
    def partition_tracks(
        tracks: list[Track],
        strip_tracks: bool = False,
        strip_profile: str | None = None,
        order_tracks: bool = False,
        order_profile: str | None = None,
    ) -> tuple[list[Track], list[str]]:
        container_tracks = list(tracks)
        stripped_indices: list[str] = []

        if strip_tracks and strip_profile:
            container_tracks = [
                track for track in container_tracks if track.scores.get(strip_profile, 0) > 0
            ]
            stripped_indices = [
                str(track.index) for track in container_tracks if not track.is_external
            ]

        if order_tracks and order_profile:
            container_tracks.sort(
                key=lambda track: track.scores.get(order_profile, 0), reverse=True
            )

        return container_tracks, stripped_indices

    def multiplex_file(
        self,
        file_path: Path,
        video_tracks: list[Track],
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        parameters: Parameters,
        dry_run: bool = False,
    ) -> None:
        internal_audio_tracks = [track for track in audio_tracks if not track.is_external]
        internal_subtitle_tracks = [track for track in subtitle_tracks if not track.is_external]

        audio_tracks, stripped_audio_indices = self.partition_tracks(
            audio_tracks,
            parameters.strip_unscored_tracks,
            parameters.strip_audio_profile,
            parameters.order_tracks_by_score,
            parameters.order_audio_profile,
        )
        subtitle_tracks, stripped_subtitles_indices = self.partition_tracks(
            subtitle_tracks,
            parameters.strip_unscored_tracks,
            parameters.strip_subtitle_profile,
            parameters.order_tracks_by_score,
            parameters.order_subtitle_profile,
        )
        external_tracks = [track for track in subtitle_tracks if track.is_external]
        remaining_tracks = [*video_tracks, *audio_tracks, *subtitle_tracks]

        stripped_audio_indices = (
            stripped_audio_indices
            if len(stripped_audio_indices) != len(internal_audio_tracks)
            else []
        )
        stripped_subtitles_indices = (
            stripped_subtitles_indices
            if len(stripped_subtitles_indices) != len(internal_subtitle_tracks)
            else []
        )

        source_mapping: dict[Path, int] = {file_path.resolve(): 0}
        for external_track in external_tracks:
            if external_track.file_path is None:
                continue
            resolved_path = external_track.file_path.resolve()
            if resolved_path not in source_mapping:
                source_mapping[resolved_path] = len(source_mapping)

        track_order: list[str] = []
        for track in remaining_tracks:
            if track.is_external and track.file_path:
                source_id = source_mapping[track.file_path.resolve()]
                track_order.append(f'{source_id}:0')
            else:
                track_order.append(f'0:{track.index}')

        if order_tracks := parameters.order_tracks_by_score:
            internal_indices = [
                int(spec.split(':')[1]) for spec in track_order if spec.startswith('0:')
            ]
            order_tracks = any(
                idx_a > idx_b for idx_a, idx_b in itertools.pairwise(internal_indices)
            ) or bool(external_tracks)

        temp_output_path = file_path.with_name(f'{file_path.stem}_temp.mkv')
        arguments = ['-o', str(temp_output_path)]

        if stripped_audio_indices:
            arguments.extend(['--audio-tracks', ','.join(stripped_audio_indices)])
        if stripped_subtitles_indices:
            arguments.extend(['--subtitle-tracks', ','.join(stripped_subtitles_indices)])
        if order_tracks or external_tracks:
            arguments.extend(['--track-order', ','.join(track_order)])
        if parameters.mkvmerge_arguments:
            arguments.extend(parameters.mkvmerge_arguments)
        arguments.append(str(file_path))

        for subtitle_track in external_tracks:
            track_lang = subtitle_track.normalized_language
            if track_lang != 'und':
                arguments.extend(['--language', f'0:{track_lang}'])
            if subtitle_track.name:
                arguments.extend(['--track-name', f'0:{subtitle_track.name}'])
            arguments.extend(['--default-track-flag', f'0:{int(subtitle_track.default)}'])
            arguments.extend(['--forced-display-flag', f'0:{int(subtitle_track.forced)}'])
            if subtitle_track.codec == 'S_TEXT/UTF8':
                arguments.extend(['--sub-charset', '0:UTF-8'])
            arguments.append(str(subtitle_track.file_path))

        log_prefix = '[DRY RUN] ' if dry_run else ''
        self.extension_logger.info(log_prefix + ' '.join(arguments))
        if not dry_run:
            try:
                self.multiplex_tracks(arguments)
                if not parameters.remove_original_container:
                    backup_path = file_path.with_name(f'{file_path.stem}.orig.mkv')
                    file_path.replace(backup_path)
                temp_output_path.replace(file_path)
            except subprocess.CalledProcessError as e:
                mkvmerge_logger.error((e.stderr or e.stdout or str(e)).strip())
                temp_output_path.unlink(missing_ok=True)
                raise

        if parameters.remove_external_subtitles:
            for track in external_tracks:
                if track.file_path is None:
                    continue
                self.extension_logger.info(log_prefix + f"removing subtitles '{track.file_path}'")
                if not dry_run:
                    track.file_path.unlink(missing_ok=True)

    def multiplex_tracks(self, arguments: list[str]) -> None:
        with NamedTemporaryFile('w+', encoding='utf-8', suffix='.json', delete=False) as temp_file:
            json.dump(arguments, temp_file)
            temp_file_path = Path(temp_file.name)

        try:
            result = subprocess.run(
                ['mkvmerge', f'@{temp_file_path}'],
                capture_output=True,
                encoding='utf-8',
                check=True,
                text=True,
            )
            mkvmerge_logger.debug(result.stdout.strip())
        finally:
            temp_file_path.unlink(missing_ok=True)
