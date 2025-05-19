import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys

import aiohttp
from aiohttp import web

import main as mkvpriority

__version__ = 'v1.0.7'

logger = logging.getLogger('entrypoint')
processing_queue: asyncio.Queue[tuple[str, str, int]] = asyncio.Queue()

SONARR_URL = os.getenv('SONARR_URL')
RADARR_URL = os.getenv('RADARR_URL')
SONARR_API_KEY = os.getenv('SONARR_API_KEY')
RADARR_API_KEY = os.getenv('RADARR_API_KEY')
MKVPRIORITY_ARGS = os.getenv('MKVPRIORITY_ARGS', '')


async def rescan_folder(arr_name: str, media_id: int):
    match arr_name:
        case 'Sonarr':
            if SONARR_URL is None or SONARR_API_KEY is None:
                logger.error('SONARR_URL/SONARR_API_KEY is missing; skipping rescan')
                return
            api_url, api_key = SONARR_URL + '/api/v3/command', SONARR_API_KEY
        case 'Radarr':
            if RADARR_URL is None or RADARR_API_KEY is None:
                logger.error('RADARR_URL/RADARR_API_KEY is missing; skipping rescan')
                return
            api_url, api_key = RADARR_URL + '/api/v3/command', RADARR_API_KEY
        case _:
            raise ValueError(f'arr_name must be either Sonarr or Radarr, not {arr_name}')

    headers = {'X-Api-Key': api_key}
    async with aiohttp.ClientSession() as session:
        payload = {
            'name': 'RescanSeries' if arr_name == 'Sonarr' else 'RescanMovie',
            'seriesId' if arr_name == 'Sonarr' else 'movieId': media_id,
        }
        async with session.post(api_url, json=payload, headers=headers) as response:
            if response.status != 201:
                logger.error(f'{payload['name']} failed; status {response.status}')


async def queue_worker():
    while True:
        file_path, arr_name, media_id = await processing_queue.get()
        try:
            argv = [*MKVPRIORITY_ARGS.split(), file_path]
            await asyncio.get_event_loop().run_in_executor(None, lambda: mkvpriority.main(argv))
            try:
                await rescan_folder(arr_name, media_id)
            except aiohttp.ClientConnectorError:
                logger.error(f'{arr_name} API is unreachable; skipping rescan')
        except subprocess.CalledProcessError:
            logger.error(f'error occurred; skipping file: \'{file_path}\'')
        finally:
            processing_queue.task_done()


async def preprocess_handler(request: web.Request) -> web.Response:
    args = await request.json()
    arr_name = args.get('arr_name')
    file_path = args.get('file_path')
    media_id = int(args.get('media_id'))

    await processing_queue.put((file_path, arr_name, media_id))
    return web.json_response({'message': f'\'{file_path}\' recieved from {arr_name}'})


async def init_api(host: str, port: int):
    app = web.Application()
    app.router.add_post('/preprocess', preprocess_handler)

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

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: stop_event.set())
    loop.add_signal_handler(signal.SIGINT, lambda: stop_event.set())

    await stop_event.wait()
    await runner.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    logger.info(f'MKVPriority {__version__}')
    logger.info(f'running on http://{args.host}:{args.port}')

    asyncio.run(init_api(args.host, args.port))


if __name__ == '__main__':
    logging.basicConfig(
        format='[%(asctime)s %(levelname)s] [%(name)s] %(message)s',
        level=logging.INFO,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

    if SONARR_URL or RADARR_URL:
        main()
    else:
        mkvpriority.main()
