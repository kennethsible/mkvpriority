import importlib
import inspect
import logging
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from .config import Config
from .database import Database
from .types import Track

mkvpriority_logger = logging.getLogger('mkvpriority')


class Extension(ABC):
    def __init__(self, extension_name: str | None = None):
        name = extension_name or self.__class__.__name__
        self.extension_logger = logging.getLogger(name)

    @abstractmethod
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
        raise NotImplementedError


def setup_extension_paths() -> None:
    ext_dirs: list[Path] = []
    if env_dir := os.environ.get('MKVPRIORITY_EXT_DIR'):
        ext_dirs.append(Path(env_dir))
    ext_dirs.append(Path.home() / '.config' / 'mkvpriority' / 'extensions')
    ext_dirs.append(Path.cwd())
    for ext_dir in ext_dirs:
        if ext_dir.is_dir() and str(ext_dir) not in sys.path:
            sys.path.insert(0, str(ext_dir))


def load_extension(module_name: str) -> Extension | None:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        try:
            module = importlib.import_module(f'mkvpriority.extensions.{module_name}')
        except ImportError:
            mkvpriority_logger.error(f"could not locate extension '{module_name}'")
            return None

    for name, obj in inspect.getmembers(module, inspect.isclass):
        if (
            issubclass(obj, Extension)
            and obj is not Extension
            and obj.__module__ == module.__name__
        ):
            if not callable(obj):
                mkvpriority_logger.error(f"'{name}' in '{module_name}' is not callable")
                return None
            try:
                return obj()
            except TypeError:
                mkvpriority_logger.error(f"could not instantiate '{name}'")
                return None

    mkvpriority_logger.error(f"no valid extension subclass found in '{module_name}'")
    return None
