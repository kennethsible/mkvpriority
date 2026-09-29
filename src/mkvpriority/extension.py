import importlib
import inspect
import logging
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
    ) -> None: ...


def load_extension(module_name: str) -> Extension | None:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        try:
            module = importlib.import_module(f'mkvpriority.extensions.{module_name}')
        except ImportError:
            mkvpriority_logger.error(f"could not locate extension '{module_name}'")
            return None

    for class_name, member in inspect.getmembers(module, inspect.isclass):
        if (
            issubclass(member, Extension)
            and member is not Extension
            and member.__module__ == module.__name__
        ):
            try:
                return member()
            except Exception:
                mkvpriority_logger.exception(f"could not instantiate extension '{class_name}'")
                return None

    mkvpriority_logger.error(f"no valid extension subclass found in '{module_name}'")
    return None
