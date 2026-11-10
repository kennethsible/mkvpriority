import logging
import sys
from logging.handlers import RotatingFileHandler

mkvpriority_logger = logging.getLogger('mkvpriority')
mkvpropedit_logger = logging.getLogger('mkvpropedit')
mkvextract_logger = logging.getLogger('mkvextract')
mkvmerge_logger = logging.getLogger('mkvmerge')

VERBOSE = 15
logging.addLevelName(VERBOSE, 'VERBOSE')


class PrefixFilter(logging.Filter):
    def __init__(self, *, simulate: bool = False, dry_run: bool = False):
        super().__init__()
        self.simulate = simulate
        self.dry_run = dry_run

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name in ('mkvpriority.mkvpropedit', 'mkvpriority.mkvmerge'):
            use_prefix = self.simulate
        else:
            use_prefix = self.dry_run
        record.prefix = '[DRY RUN] ' if use_prefix else ''
        return True


class StreamFilter(logging.Filter):
    def __init__(self, stream_level: int = logging.INFO):
        super().__init__()
        self.stream_level = stream_level

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == 'mkvpriority':
            return record.levelno >= self.stream_level
        return True


prefix_filter = PrefixFilter()
stream_filter = StreamFilter()


def configure_logging(
    log_path_str: str | None = None, max_bytes: int = 0, max_files: int = 1
) -> None:
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        return

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.addFilter(prefix_filter)
    stream_handler.addFilter(stream_filter)

    handlers: list[logging.Handler] = [stream_handler]
    if log_path_str:
        file_handler = RotatingFileHandler(log_path_str, maxBytes=max_bytes, backupCount=max_files)
        file_handler.setLevel(logging.DEBUG)
        file_handler.addFilter(prefix_filter)
        handlers.append(file_handler)

    logging.basicConfig(
        format='[%(asctime)s %(levelname)s] [%(name)s] %(prefix)s%(message)s', handlers=handlers
    )


def set_log_levels(
    main_level: int = logging.INFO,
    tool_level: int = logging.INFO,
    *,
    simulate: bool = False,
    dry_run: bool = False,
) -> None:
    prefix_filter.dry_run = dry_run
    prefix_filter.simulate = simulate
    stream_filter.stream_level = main_level

    mkvpriority_logger.setLevel(logging.DEBUG)
    for logger in (mkvpropedit_logger, mkvextract_logger, mkvmerge_logger):
        logger.setLevel(tool_level)
