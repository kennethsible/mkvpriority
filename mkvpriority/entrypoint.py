import argparse
import asyncio
import logging
import os
import shlex
import shutil
import signal

import pycountry
import requests
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from mkvpriority.main import main as main_cli
from mkvpriority.main import setup_logging

__version__ = 'v1.2.0'

logger = logging.getLogger('entrypoint')
processing_queue: asyncio.Queue[tuple[str, str, str, str]] = asyncio.Queue()

CRON_SCHEDULE = os.getenv('CRON_SCHEDULE')
CUSTOM_SCRIPT = os.getenv('CUSTOM_SCRIPT', 'false').lower() in ('true', '1', 't')
WEBHOOK_RECEIVER = os.getenv('WEBHOOK_RECEIVER', 'false').lower() in ('true', '1', 't')
MKVPRIORITY_ARGS = ['-c', '/config/config.toml'] + shlex.split(os.getenv('MKVPRIORITY_ARGS', ''))
SONARR_URL, SONARR_API_KEY = os.getenv('SONARR_URL'), os.getenv('SONARR_API_KEY')
RADARR_URL, RADARR_API_KEY = os.getenv('RADARR_URL'), os.getenv('RADARR_API_KEY')


def get_alpha_3_code(lang_name: str) -> str | None:
    try:
        lang = pycountry.languages.lookup(lang_name)
        return lang.alpha_3  # ISO 639-3
    except LookupError:
        return None


def get_orig_lang(item_id: str, item_type: str) -> str | None:
    match item_type:
        case 'series':
            endpoint = f'{SONARR_URL}/api/v3/series/{item_id}'
            headers = {'X-Api-Key': SONARR_API_KEY}
        case 'movie':
            endpoint = f'{RADARR_URL}/api/v3/movie/{item_id}'
            headers = {'X-Api-Key': RADARR_API_KEY}
        case _:
            return None

    try:
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()
    except requests.RequestException:
        logger.exception(f"request failed: '{endpoint}'")
        raise
    lang_info = response.json().get('originalLanguage', {})
    return get_alpha_3_code(lang_info.get('name', ''))


async def queue_worker():
    while True:
        file_path, item_type, item_tags, item_id = await processing_queue.get()
        if item_tags:
            file_path += f'::{item_tags.split(",")[0]}'
        try:
            argv = [*MKVPRIORITY_ARGS, file_path]
            orig_lang = get_orig_lang(item_id, item_type)
            await asyncio.get_event_loop().run_in_executor(None, lambda: main_cli(argv, orig_lang))
        except Exception:
            logger.error(f"skipping (error occurred) '{file_path}'")
        finally:
            processing_queue.task_done()


async def process_handler(request: web.Request) -> web.Response:
    args = await request.json()
    file_path = args.get('file_path')
    item_type = args.get('item_type', '')
    item_tags = args.get('item_tags', '')
    item_id = args.get('item_id', '')
    await processing_queue.put((file_path, item_type, item_tags, item_id))
    return web.json_response({'message': f"recieved '{file_path}'"})


async def init_api(host: str, port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_post('/process', process_handler)

    async def on_startup(app: web.Application):
        app['worker'] = asyncio.create_task(queue_worker())

    async def on_cleanup(app: web.Application):
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


async def init_scheduler(timezone: str | None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    trigger = CronTrigger.from_crontab(CRON_SCHEDULE, timezone)
    scheduler.add_job(lambda: main_cli(MKVPRIORITY_ARGS), trigger)
    scheduler.start()
    return scheduler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    setup_logging()
    logger.setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

    os.makedirs('/config', exist_ok=True)
    if not os.path.exists('/config/config.toml'):
        shutil.copy2('config.toml', '/config/')
    if not os.path.exists('/config/mkvpriority.sh'):
        shutil.copy2('mkvpriority.sh', '/config/')
    open('/config/archive.db', 'a').close()

    logger.info(f'MKVPriority {__version__}')

    async def run_all():
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        loop.add_signal_handler(signal.SIGTERM, lambda: stop_event.set())
        loop.add_signal_handler(signal.SIGINT, lambda: stop_event.set())

        scheduler = None
        if CRON_SCHEDULE:
            timezone = os.getenv('TZ', 'UTC')
            if timezone:
                logger.info(f'setting timezone to {timezone}')
            logger.info(f"scheduling task at '{CRON_SCHEDULE}'")
            scheduler = await init_scheduler(timezone)

        runner = None
        if WEBHOOK_RECEIVER:
            if CRON_SCHEDULE:
                logger.warning('unset CRON_SCHEDULE to use WEBHOOK_RECEIVER')
            else:
                logger.info(f'running receiver on port {args.port}')
                runner = await init_api(args.host, args.port)

        await stop_event.wait()
        if runner:
            await runner.cleanup()
        if scheduler:
            scheduler.shutdown(wait=False)

    asyncio.run(run_all())


if __name__ == '__main__':
    if CUSTOM_SCRIPT:
        logger.warning('CUSTOM_SCRIPT is deprecated; use WEBHOOK_RECEIVER instead')
        main()
    elif WEBHOOK_RECEIVER or CRON_SCHEDULE:
        main()
    else:
        main_cli()
