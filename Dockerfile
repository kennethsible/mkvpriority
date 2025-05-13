FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    mkvtoolnix \
    mkvtoolnix-gui \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY main.py .
COPY config.toml .

CMD ["python", "-u", "main.py"]
