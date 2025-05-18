FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir aiohttp

WORKDIR /app

COPY main.py .
COPY config.toml .

COPY entrypoint.py .
ENTRYPOINT ["python", "-u", "entrypoint.py"]
