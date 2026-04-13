import asyncio
import logging
import os
import shlex
import shutil
import signal
from pathlib import Path
from typing import cast

import aiohttp
import pycountry
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from mkvpriority import __version__
from mkvpriority.main import main as main_cli
from mkvpriority.main import setup_logging

entrypoint_logger = logging.getLogger('entrypoint')
processing_queue: asyncio.Queue[tuple[str, str, str, str]] = asyncio.Queue()


MKVPRIORITY_ARGS = ['-c', '/config/config.toml'] + shlex.split(os.getenv('MKVPRIORITY_ARGS', ''))
SONARR_URL, SONARR_API_KEY = os.getenv('SONARR_URL'), os.getenv('SONARR_API_KEY')
RADARR_URL, RADARR_API_KEY = os.getenv('RADARR_URL'), os.getenv('RADARR_API_KEY')
LOG_MAX_BYTES, LOG_MAX_FILES = os.getenv('LOG_MAX_BYTES'), os.getenv('LOG_MAX_FILES')

CUSTOM_SCRIPT = os.getenv('CUSTOM_SCRIPT', 'false').lower() in ('true', '1', 't')
WEBHOOK_RECEIVER = os.getenv('WEBHOOK_RECEIVER', 'false').lower() in ('true', '1', 't')
WEBHOOK_PORT_STR = os.getenv('WEBHOOK_PORT') or ('8080' if WEBHOOK_RECEIVER else None)
WEBHOOK_PORT = int(WEBHOOK_PORT_STR) if WEBHOOK_PORT_STR else None

CRON_MACROS = {
    '@yearly': '0 0 1 1 *',
    '@annually': '0 0 1 1 *',
    '@monthly': '0 0 1 * *',
    '@weekly': '0 0 * * 0',
    '@daily': '0 0 * * *',
    '@midnight': '0 0 * * *',
    '@hourly': '0 * * * *',
}
CRON_SCHEDULE = os.getenv('CRON_SCHEDULE')
CRON_TARGET_PATHS = shlex.split(os.getenv('CRON_TARGET_PATHS', ''))


def get_alpha_3_code(lang_name: str) -> str | None:
    try:
        lang = pycountry.languages.lookup(lang_name)
        return cast(str, lang.alpha_3)  # ISO 639-3
    except LookupError:
        return None


async def get_orig_lang(item_id: str, item_type: str) -> str | None:
    match item_type:
        case 'series':
            if SONARR_URL is None:
                return None
            if SONARR_API_KEY is None:
                entrypoint_logger.warning('set SONARR_API_KEY to use SONARR_URL')
                return None
            endpoint = f'{SONARR_URL}/api/v3/series/{item_id}'
            headers = {'X-Api-Key': SONARR_API_KEY}
        case 'movie':
            if RADARR_URL is None:
                return None
            if RADARR_API_KEY is None:
                entrypoint_logger.warning('set RADARR_API_KEY to use RADARR_URL')
                return None
            endpoint = f'{RADARR_URL}/api/v3/movie/{item_id}'
            headers = {'X-Api-Key': RADARR_API_KEY}
        case _:
            return None

    async with aiohttp.ClientSession() as session:
        async with session.get(endpoint, headers=headers) as response:
            response.raise_for_status()
            data = await response.json()

    lang_info = data.get('originalLanguage', {})
    return get_alpha_3_code(lang_info.get('name', ''))


async def queue_worker() -> None:
    while True:
        file_path, item_type, item_tags, item_id = await processing_queue.get()
        if item_tags:
            file_path += f'::{item_tags.split(",")[0]}'
        try:
            argv = [*MKVPRIORITY_ARGS, file_path]
            orig_lang = await get_orig_lang(item_id, item_type)
            await asyncio.to_thread(main_cli, argv, orig_lang)
        except Exception:
            entrypoint_logger.exception(f"error occurred: '{file_path}'")
        finally:
            processing_queue.task_done()


async def process_handler(request: web.Request) -> web.Response:
    args = await request.json()
    file_path = args.get('file_path')
    item_type = args.get('item_type', '')
    item_tags = args.get('item_tags', '')
    item_id = args.get('item_id', '')
    await processing_queue.put((file_path, item_type, item_tags, item_id))
    return web.json_response({'message': f"received '{file_path}'"})


async def create_runner(host: str, port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_post('/process', process_handler)

    async def on_startup(app: web.Application) -> None:
        app['worker'] = asyncio.create_task(queue_worker())

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


async def create_scheduler(expr: str, timezone: str | None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    trigger = CronTrigger.from_crontab(expr, timezone)
    cron_argv = MKVPRIORITY_ARGS + CRON_TARGET_PATHS
    scheduler.add_job(lambda: main_cli(cron_argv), trigger)
    scheduler.start()
    return scheduler


def main() -> None:
    config_dir = Path('/config')
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / 'config.toml'
        if not config_file.is_file():
            shutil.copy2('config.toml', config_file)

        script_file = config_dir / 'mkvpriority.sh'
        if not script_file.is_file():
            shutil.copy2('mkvpriority.sh', script_file)

        database_file = config_dir / 'archive.db'
        database_file.touch(exist_ok=True)
    except PermissionError:
        entrypoint_logger.warning(f'recreate {config_dir} with correct PUID/PGID')
        raise

    max_bytes = 5242880 if LOG_MAX_BYTES is None else int(LOG_MAX_BYTES)
    max_files = 3 if LOG_MAX_FILES is None else int(LOG_MAX_FILES)
    setup_logging('/config/mkvpriority.log', max_bytes, max_files)

    entrypoint_logger.setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

    async def run_all() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def handle_signal(sig_name: str) -> None:
            entrypoint_logger.info(f'received {sig_name} signal; stopping tasks')
            stop_event.set()

        loop.add_signal_handler(signal.SIGTERM, lambda: handle_signal('SIGTERM'))
        loop.add_signal_handler(signal.SIGINT, lambda: handle_signal('SIGINT'))

        runner = None
        if WEBHOOK_PORT:
            entrypoint_logger.info(f'listening for webhooks on port {WEBHOOK_PORT}')
            runner = await create_runner('0.0.0.0', WEBHOOK_PORT)

        scheduler = None
        if expr := CRON_SCHEDULE:
            if expr.startswith('@'):
                try:
                    expr = CRON_MACROS[expr]
                except KeyError as e:
                    e.add_note(f"unsupported macro: '{expr}'")
                    raise
            timezone = os.getenv('TZ', 'UTC')
            if timezone:
                entrypoint_logger.info(f'setting time zone to {timezone}')
            entrypoint_logger.info(f"scheduling task to run at '{expr}'")
            scheduler = await create_scheduler(expr, timezone)

        await stop_event.wait()
        if runner:
            await runner.cleanup()
        if scheduler:
            scheduler.shutdown(wait=False)

    entrypoint_logger.info(f'MKVPriority {__version__}')
    asyncio.run(run_all())


if __name__ == '__main__':
    if CUSTOM_SCRIPT or WEBHOOK_RECEIVER:
        entrypoint_logger.warning(
            'CUSTOM_SCRIPT and WEBHOOK_RECEIVER are deprecated; use WEBHOOK_PORT instead'
        )
        main()
    elif WEBHOOK_PORT or CRON_SCHEDULE:
        main()
    else:
        main_cli()
