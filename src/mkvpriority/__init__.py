import importlib.metadata
import tomllib
from pathlib import Path

from .main import (
    Config,
    Database,
    Track,
    extract_subtitles,
    extract_tracks,
    identify_tracks,
    modify_tracks,
    process_file,
    process_tracks,
    restore_tracks,
)

try:
    __version__ = importlib.metadata.version('mkvpriority')
except importlib.metadata.PackageNotFoundError:
    __version__ = '(Unknown Version)'
    try:
        with Path('/app/pyproject.toml').open('rb') as f:
            __version__ = tomllib.load(f)['project']['version']
    except FileNotFoundError, KeyError:
        pass


__all__ = [
    'Config',
    'Database',
    'Track',
    'extract_subtitles',
    'extract_tracks',
    'identify_tracks',
    'modify_tracks',
    'process_file',
    'process_tracks',
    'restore_tracks',
]
