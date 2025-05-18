import argparse
import asyncio
import logging
import os
import subprocess
import sys

import aiohttp
from aiohttp import web

import main

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler(sys.stdout))

processing_queue: asyncio.Queue[tuple[str, str, int]] = asyncio.Queue()

SONARR_URL = os.getenv('SONARR_URL')
RADARR_URL = os.getenv('RADARR_URL')
SONARR_API_KEY = os.getenv('SONARR_API_KEY')
RADARR_API_KEY = os.getenv('RADARR_API_KEY')
MKVPRIORITY_ARGS = os.getenv('MKVPRIORITY_ARGS', '')


async def trigger_rescan(arr_name: str, item_id: int):
    match arr_name:
        case 'Sonarr':
            command_name, id_name = 'RescanSeries', 'seriesId'
            if SONARR_URL is None or SONARR_API_KEY is None:
                logger.error('[mkvpriority] SONARR_URL/SONARR_API_KEY is missing; skipping rescan')
                return
            api_url, api_key = SONARR_URL + '/api/v3/command', SONARR_API_KEY
        case 'Radarr':
            command_name, id_name = 'RescanMovie', 'movieId'
            if RADARR_URL is None or RADARR_API_KEY is None:
                logger.error('[mkvpriority] RADARR_URL/RADARR_API_KEY is missing; skipping rescan')
                return
            api_url, api_key = RADARR_URL + '/api/v3/command', RADARR_API_KEY
        case _:
            raise ValueError(f'arr_name must be either Sonarr or Radarr, not {arr_name}')

    headers = {'X-Api-Key': api_key}
    async with aiohttp.ClientSession() as session:
        payload = {'name': command_name, id_name: item_id}
        async with session.post(api_url, json=payload, headers=headers) as response:
            if response.status != 201:
                logger.error(f'[mkvpriority] {command_name} failed; status {response.status}')


async def queue_worker():
    while True:
        file_path, arr_name, item_id = await processing_queue.get()
        try:
            sys.argv = ['main.py', *MKVPRIORITY_ARGS.split(), file_path]
            await asyncio.get_event_loop().run_in_executor(None, main.main)
            try:
                await trigger_rescan(arr_name, item_id)
            except aiohttp.ClientConnectorError:
                logger.error(f'[mkvpriority] {arr_name} API is unreachable; skipping rescan')
        except subprocess.CalledProcessError:
            logger.error(f'[mkvpriority] \'{file_path}\' error occurred; skipping file')
        finally:
            processing_queue.task_done()


async def preprocess_handler(request: web.Request) -> web.Response:
    args = await request.json()
    arr_name = args.get('arr_name')
    file_path = args.get('file_path')
    item_id = int(args.get('item_id'))

    await processing_queue.put((file_path, arr_name, item_id))
    return web.json_response({'message': f'\'{file_path}\' recieved from {arr_name}'})


async def init_app() -> web.Application:
    app = web.Application()
    app.router.add_post('/preprocess', preprocess_handler)

    async def on_startup(app: web.Application):
        app['worker'] = asyncio.create_task(queue_worker())

    app.on_startup.append(on_startup)
    return app


def start_api():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    web.run_app(init_app(), host=args.host, port=args.port)


if __name__ == '__main__':
    if SONARR_URL or RADARR_URL:
        start_api()
    else:
        main.main()
