import itertools
import json
import subprocess
import tomllib
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from mkvpriority import Config, Extension, Track
from mkvpriority.main import mkvmerge_logger


class Multiplexer(Extension):
    def __init__(self) -> None:
        super().__init__('multiplexer')
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
        if config.toml_path in self.parameters:
            attributes = self.parameters[config.toml_path]
        else:
            with open(config.toml_path, 'rb') as f:
                toml_file = tomllib.load(f)
            attributes = toml_file.get('multiplexer', {})
            self.parameters[config.toml_path] = attributes

        strip_tracks = attributes.get('strip_tracks', False)
        reorder_tracks = attributes.get('reorder_tracks', False)
        remux_audio_profile = attributes.get('remux_audio_profile')
        remux_subtitle_profile = attributes.get('remux_subtitle_profile')
        mkvmerge_arguments = attributes.get('mkvmerge_arguments', [])

        has_track_action = any((strip_tracks, reorder_tracks))
        has_remux_profile = any((remux_audio_profile, remux_subtitle_profile))

        if (has_track_action and has_remux_profile) or mkvmerge_arguments:
            self.multiplex_file(
                file_path,
                video_tracks,
                audio_tracks,
                subtitle_tracks,
                strip_tracks,
                reorder_tracks,
                remux_audio_profile,
                remux_subtitle_profile,
                mkvmerge_arguments,
                dry_run,
            )

    @staticmethod
    def partition_tracks(
        tracks: list[Track],
        profile_name: str | None = None,
        strip_tracks: bool = False,
        reorder_tracks: bool = False,
    ) -> tuple[list[str], list[str]]:
        ordered_indices: list[str] = []
        stripped_indices: list[str] = []

        if not profile_name:
            ordered_indices.extend(f'0:{track.index}' for track in tracks)
            return ordered_indices, stripped_indices

        sorted_tracks = [track for track in tracks if track.scores.get(profile_name, 0) > 0]
        sorted_tracks.sort(key=lambda track: track.scores.get(profile_name, 0), reverse=True)
        unwanted_tracks = [track for track in tracks if track.scores.get(profile_name, 0) <= 0]

        if strip_tracks:
            ordered_indices.extend(f'0:{track.index}' for track in sorted_tracks)
            stripped_indices.extend(f'!{track.index}' for track in unwanted_tracks)
        elif reorder_tracks:
            ordered_indices.extend(f'0:{track.index}' for track in sorted_tracks + unwanted_tracks)

        return ordered_indices, stripped_indices

    def multiplex_file(
        self,
        file_path: Path,
        video_tracks: list[Track],
        audio_tracks: list[Track],
        subtitle_tracks: list[Track],
        strip_tracks: bool = False,
        reorder_tracks: bool = False,
        remux_audio_profile: str | None = None,
        remux_subtitle_profile: str | None = None,
        mkvmerge_arguments: list[str] | None = None,
        dry_run: bool = False,
    ) -> None:
        track_order: list[str] = [f'0:{track.index}' for track in video_tracks]

        audio_order, audio_strip = self.partition_tracks(
            audio_tracks, remux_audio_profile, strip_tracks, reorder_tracks
        )
        subtitle_order, subtitle_strip = self.partition_tracks(
            subtitle_tracks, remux_subtitle_profile, strip_tracks, reorder_tracks
        )

        track_order.extend(audio_order)
        track_order.extend(subtitle_order)

        requires_reorder = reorder_tracks and any(
            int(id_a.split(':')[1]) > int(id_b.split(':')[1])
            for id_a, id_b in itertools.pairwise(track_order)
        )
        if not (audio_strip or subtitle_strip or requires_reorder or mkvmerge_arguments):
            return

        temp_output_path = file_path.with_name(f'{file_path.stem}_temp.mkv')
        arguments = ['-o', str(temp_output_path)]
        if audio_strip:
            arguments.extend(['--audio-tracks', ','.join(audio_strip)])
        if subtitle_strip:
            arguments.extend(['--subtitle-tracks', ','.join(subtitle_strip)])
        if requires_reorder:
            arguments.extend(['--track-order', ','.join(track_order)])
        if mkvmerge_arguments:
            arguments.extend(mkvmerge_arguments)
        arguments.append(str(file_path))

        self.extension_logger.info(' '.join(arguments))
        if not dry_run:
            try:
                self.multiplex_tracks(arguments)
                temp_output_path.replace(file_path)
            except subprocess.CalledProcessError as e:
                mkvmerge_logger.error((e.stderr or e.stdout or str(e)).strip())
                temp_output_path.unlink(missing_ok=True)
                raise

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
