import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
from importlib.resources import as_file, files
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import cron_descriptor
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from cron_descriptor import FormatError

import mkvpriority.main
from mkvpriority import __version__

from .config import get_config_dir
from .logger import configure_logging
from .types import resolve_language

entrypoint_logger = logging.getLogger('entrypoint')
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

type ItemPayload = tuple[str, str, str | None]
type ProcessingQueue = asyncio.Queue[ItemPayload]

_DEPRECATION_WARNINGS: list[str] = []


def get_env_var(name: str, legacy_name: str | None = None) -> str | None:
    if (value := os.getenv(name)) is not None:
        return value
    if legacy_name and (legacy_value := os.getenv(legacy_name)) is not None:
        _DEPRECATION_WARNINGS.append(f"'{legacy_name}' is deprecated; use '{name}' instead")
        return legacy_value
    return None


CONFIG_DIR = get_config_dir()
MKVPRIORITY_ARGUMENTS = ['-c', str(CONFIG_DIR / 'config.toml')] + shlex.split(
    get_env_var('MKVPRIORITY_ARGUMENTS', 'MKVPRIORITY_ARGS') or ''
)

LOG_MAX_BYTES = get_env_var('MKVPRIORITY_LOG_MAX_BYTES', 'LOG_MAX_BYTES')
LOG_MAX_FILES = get_env_var('MKVPRIORITY_LOG_MAX_FILES', 'LOG_MAX_FILES')

WEBHOOK_HOST = get_env_var('MKVPRIORITY_WEBHOOK_HOST', 'WEBHOOK_HOST') or '127.0.0.1'
WEBHOOK_PORT_STR = get_env_var('MKVPRIORITY_WEBHOOK_PORT', 'WEBHOOK_PORT')
WEBHOOK_PORT = int(WEBHOOK_PORT_STR) if WEBHOOK_PORT_STR else None

CRON_TIMEZONE = get_env_var('MKVPRIORITY_CRON_TIMEZONE') or os.getenv('TZ') or 'UTC'
CRON_SCHEDULE = get_env_var('MKVPRIORITY_CRON_SCHEDULE', 'CRON_SCHEDULE')
CRON_TARGET_PATHS = shlex.split(
    get_env_var('MKVPRIORITY_CRON_TARGET_PATHS', 'CRON_TARGET_PATHS') or ''
)

CRON_MACROS = {
    '@yearly': '0 0 1 1 *',
    '@annually': '0 0 1 1 *',
    '@monthly': '0 0 1 * *',
    '@weekly': '0 0 * * 0',
    '@daily': '0 0 * * *',
    '@midnight': '0 0 * * *',
    '@hourly': '0 * * * *',
}

PROCESSING_QUEUE_KEY: web.AppKey[ProcessingQueue] = web.AppKey('processing_queue')


async def process_item(file_path: str, item_tags: str, orig_lang: str | None) -> None:
    if item_tags:
        file_path += f'::{re.split(r"[,;|]", item_tags)[0]}'
    try:
        argv = [*MKVPRIORITY_ARGUMENTS, file_path]
        await asyncio.to_thread(mkvpriority.main.main, argv, orig_lang)
    except Exception:
        entrypoint_logger.exception(f"error occurred while processing '{file_path}'")


async def queue_worker(queue: ProcessingQueue) -> None:
    while True:
        file_path, item_tags, item_id = await queue.get()
        try:
            await process_item(file_path, item_tags, item_id)
        finally:
            queue.task_done()


async def process_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            return web.json_response({'error': 'expected JSON object'}, status=400)
    except (json.JSONDecodeError, web.HTTPBadRequest):
        return web.json_response({'error': 'invalid JSON payload'}, status=400)

    file_path = payload.get('file_path')
    if not file_path:
        return web.json_response({'error': "missing 'file_path'"}, status=400)

    item_tags = payload.get('item_tags', '')
    if orig_lang := payload.get('orig_lang'):
        orig_lang = resolve_language(orig_lang, target_format='alpha_3')

    queue: ProcessingQueue = request.app[PROCESSING_QUEUE_KEY]
    await queue.put((file_path, item_tags, orig_lang))
    return web.json_response({'message': f"received '{file_path}'"})


async def create_runner(host: str, port: int) -> web.AppRunner:
    app = web.Application()
    queue: ProcessingQueue = asyncio.Queue()
    app[PROCESSING_QUEUE_KEY] = queue
    app.router.add_post('/process', process_handler)

    async def on_startup(app: web.Application) -> None:
        app['worker'] = asyncio.create_task(queue_worker(queue))

    async def on_cleanup(app: web.Application) -> None:
        app['worker'].cancel()
        try:
            await app['worker']
        except asyncio.CancelledError:
            pass

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


async def create_scheduler(cron_expr: str, timezone: ZoneInfo | None = None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)
    trigger = CronTrigger.from_crontab(cron_expr, timezone)
    cron_argv = MKVPRIORITY_ARGUMENTS + CRON_TARGET_PATHS

    async def run_cron_job() -> None:
        await asyncio.to_thread(mkvpriority.main.main, cron_argv)

    scheduler.add_job(run_cron_job, trigger)
    scheduler.start()
    return scheduler


async def run_daemon() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def handle_signal(sig_name: str) -> None:
        entrypoint_logger.info(f'received {sig_name} signal')
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, handle_signal, signum.name)
        except NotImplementedError:
            signal.signal(
                signum,
                lambda value, _: loop.call_soon_threadsafe(
                    handle_signal, signal.Signals(value).name
                ),
            )

    runner: web.AppRunner | None = None
    scheduler: AsyncIOScheduler | None = None
    try:
        if WEBHOOK_HOST and WEBHOOK_PORT:
            runner = await create_runner(WEBHOOK_HOST, WEBHOOK_PORT)
            entrypoint_logger.info(f'webhook listener started on {WEBHOOK_HOST}:{WEBHOOK_PORT}')

        if cron_expr := CRON_SCHEDULE:
            if cron_expr.startswith('@'):
                try:
                    cron_expr = CRON_MACROS[cron_expr]
                except KeyError as e:
                    e.add_note(f"unsupported cron macro '{cron_expr}'")
                    raise

            try:
                timezone = ZoneInfo(CRON_TIMEZONE)
            except ZoneInfoNotFoundError as e:
                e.add_note(f"invalid cron timezone '{CRON_TIMEZONE}'")
                raise

            try:
                expr_desc = cron_descriptor.get_description(cron_expr)
                expr_desc = expr_desc[0].lower() + expr_desc[1:]
                scheduler = await create_scheduler(cron_expr, timezone)
            except (FormatError, ValueError) as e:
                e.add_note(f"unsupported cron expression '{cron_expr}'")
                raise

            entrypoint_logger.info(f'scheduled task to run {expr_desc} ({CRON_TIMEZONE})')

        await stop_event.wait()

    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        if runner:
            await runner.cleanup()


def copy_resource(resource_name: str, dst_path: Path) -> None:
    if dst_path.is_file():
        return

    resource = files('mkvpriority').joinpath(resource_name)
    if resource.is_file():
        with as_file(resource) as src_path:
            shutil.copy2(src_path, dst_path)
        return

    fallback_path = Path(__file__).resolve().parents[2] / resource_name
    shutil.copy2(fallback_path, dst_path)


def main() -> None:
    if not WEBHOOK_PORT and not CRON_SCHEDULE:
        entrypoint_logger.warning(
            'neither webhook listener nor cron scheduler is configured '
            '(set MKVPRIORITY_WEBHOOK_PORT or MKVPRIORITY_CRON_SCHEDULE)'
        )
        return

    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config_path = CONFIG_DIR / 'config.toml'
        copy_resource('config.toml', config_path)

        extensions_dir = CONFIG_DIR / 'extensions'
        extensions_dir.mkdir(parents=True, exist_ok=True)
        init_path = extensions_dir / '__init__.py'
        init_path.touch(exist_ok=True)

        if sys.platform != 'win32':
            script_path = CONFIG_DIR / 'mkvpriority.sh'
            copy_resource('mkvpriority.sh', script_path)
            script_path.chmod(0o755)

        database_path = CONFIG_DIR / 'archive.db'
        database_path.touch(exist_ok=True)
    except PermissionError:
        if os.getenv('MKVPRIORITY_CONFIG_DIR') == '/config':
            entrypoint_logger.warning(f'recreate {CONFIG_DIR} with correct PUID/PGID ownership')
        else:
            entrypoint_logger.warning(f'permission denied writing to {CONFIG_DIR}')
        raise

    log_path = CONFIG_DIR / 'mkvpriority.log'
    max_bytes = 5242880 if LOG_MAX_BYTES is None else int(LOG_MAX_BYTES)
    max_files = 3 if LOG_MAX_FILES is None else int(LOG_MAX_FILES)
    configure_logging(log_path, max_bytes, max_files)

    entrypoint_logger.setLevel(logging.INFO)
    entrypoint_logger.info(f'MKVPriority {__version__}')
    for warning in _DEPRECATION_WARNINGS:
        entrypoint_logger.warning(warning)

    if config_path.is_file():
        mkvpriority.main.main(MKVPRIORITY_ARGUMENTS)

    asyncio.run(run_daemon())


if __name__ == '__main__':
    if WEBHOOK_PORT or CRON_SCHEDULE:
        main()
    else:
        mkvpriority.main.main()
