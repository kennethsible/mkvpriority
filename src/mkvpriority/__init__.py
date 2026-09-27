import importlib.metadata
import tomllib
from pathlib import Path

from .config import Config
from .database import Database
from .extension import Extension
from .mkvtoolnix import extract_tracks, identify_tracks, modify_tracks
from .processor import process_file, process_tracks, restore_file, restore_tracks, score_tracks
from .types import (
    AudioProfile,
    AudioProfileGroup,
    Profile,
    ProfileGroup,
    SubtitleProfile,
    SubtitleProfileGroup,
    Track,
)

try:
    __version__ = importlib.metadata.version('mkvpriority')
except importlib.metadata.PackageNotFoundError:
    __version__ = '(Unknown Version)'
    try:
        with Path('/app/pyproject.toml').open('rb') as f:
            __version__ = tomllib.load(f)['project']['version']
    except (FileNotFoundError, KeyError):
        pass


__all__ = [
    'AudioProfile',
    'AudioProfileGroup',
    'Config',
    'Database',
    'Extension',
    'Profile',
    'ProfileGroup',
    'SubtitleProfile',
    'SubtitleProfileGroup',
    'Track',
    'extract_tracks',
    'identify_tracks',
    'modify_tracks',
    'process_file',
    'process_tracks',
    'restore_file',
    'restore_tracks',
    'score_tracks',
]
