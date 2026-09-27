from __future__ import annotations

import glob
import json
import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

from .config import Config
from .subtitles import parse_external_subtitles
from .types import Track

if TYPE_CHECKING:
    from .database import Database


mkvpriority_logger = logging.getLogger('mkvpriority')
mkvpropedit_logger = logging.getLogger('mkvpropedit')
mkvextract_logger = logging.getLogger('mkvextract')
mkvmerge_logger = logging.getLogger('mkvmerge')


class MissingCommandError(Exception):
    pass


def verify_mkvtoolnix_install() -> None:
    for command in ('mkvpropedit', 'mkvextract', 'mkvmerge'):
        if shutil.which(command) is None:
            raise MissingCommandError(f"'{command}' not found in PATH")


def normalize_segment_uid(segment_uid: str | None) -> str:
    if not segment_uid:
        return ''
    segment_uid = segment_uid.strip().lower().replace('-', '')
    return segment_uid.removeprefix('0x')


def ensure_segment_uid(file_path: Path, dry_run: bool = False) -> str:
    segment_uid = uuid.uuid4().hex
    mkvpriority_logger.info(f"generating segment_uid for '{file_path}'")
    arguments = [str(file_path), '--edit', 'info', '--set', f'segment-uid={segment_uid}']
    if not dry_run:
        try:
            modify_tracks(arguments)
        except subprocess.CalledProcessError as e:
            mkvpropedit_logger.error((e.stderr or e.stdout or str(e)).strip())
    return segment_uid


def identify_tracks(file_path: Path) -> Any:
    with NamedTemporaryFile('w+', encoding='utf-8', suffix='.json', delete=False) as temp_file:
        json.dump(['--identification-format', 'json', '--identify', str(file_path)], temp_file)
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
        return json.loads(result.stdout)
    finally:
        temp_file_path.unlink(missing_ok=True)


def modify_tracks(arguments: list[str]) -> None:
    with NamedTemporaryFile('w+', encoding='utf-8', suffix='.json', delete=False) as temp_file:
        json.dump(arguments, temp_file)
        temp_file_path = Path(temp_file.name)

    try:
        result = subprocess.run(
            ['mkvpropedit', f'@{temp_file_path}'],
            capture_output=True,
            check=True,
            text=True,
        )
        mkvpropedit_logger.debug(result.stdout.strip())
    finally:
        temp_file_path.unlink(missing_ok=True)


def extract_tracks(
    file_path: Path, config: Config | None = None, database: Database | None = None
) -> tuple[str | None, list[Track], list[Track], list[Track]]:
    try:
        track_data = identify_tracks(file_path)
    except subprocess.CalledProcessError as e:
        try:
            track_data = json.loads(e.stdout)
        except json.JSONDecodeError:
            mkvmerge_logger.error((e.stderr or e.stdout or str(e)).strip())
        else:
            for warning in track_data.get('warnings', []):
                mkvmerge_logger.warning(warning)
            for error in track_data.get('errors', []):
                mkvmerge_logger.error(error)
        return None, [], [], []
    else:
        for warning in track_data.get('warnings', []):
            mkvmerge_logger.warning(warning)

    container_metadata = track_data.get('container', {})
    container_properties = container_metadata.get('properties', {})
    if segment_uid := container_properties.get('segment_uid'):
        segment_uid = normalize_segment_uid(segment_uid)

    video_tracks: list[Track] = []
    audio_tracks: list[Track] = []
    subtitle_tracks: list[Track] = []

    for metadata in track_data.get('tracks', []):
        properties = metadata.get('properties', {})

        track = Track(
            index=metadata.get('id'),
            category=metadata.get('type'),
            name=properties.get('track_name', ''),
            language=properties.get('language', 'und'),
            codec=properties.get('codec_id', ''),
            channels=properties.get('audio_channels', 0),
            default=properties.get('default_track', False),
            forced=properties.get('forced_track', False),
            enabled=properties.get('enabled_track', False),
            uid=properties.get('uid'),
        )
        if track.uid is None:
            continue

        if database is not None:
            database.restore(segment_uid, track)

        match track.category:
            case 'video':
                video_tracks.append(track)
            case 'audio':
                audio_tracks.append(track)
            case 'subtitles':
                subtitle_tracks.append(track)

    if config and config.subtitle_group.process_external_subtitles:
        parent_dir = file_path.parent
        if parent_dir.is_dir():
            file_stem = file_path.stem
            virtual_id = -1
            for sidecar_path in sorted(parent_dir.glob(f'{glob.escape(file_stem)}*')):
                if not sidecar_path.is_file():
                    continue
                if subtitle_track := parse_external_subtitles(sidecar_path, file_stem, virtual_id):
                    subtitle_tracks.append(subtitle_track)
                    virtual_id -= 1

    return segment_uid, video_tracks, audio_tracks, subtitle_tracks
