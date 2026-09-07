FROM ghcr.io/astral-sh/uv:python3.14-alpine AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --group docker --all-extras

FROM python:3.14-alpine
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache mkvtoolnix ffmpeg

COPY --from=builder /app/.venv /app/.venv
COPY --chmod=755 <<-"EOF" /app/.venv/bin/mkvpriority
#!/app/.venv/bin/python
import sys
from mkvpriority.main import main
if __name__ == '__main__':
    sys.exit(main())
EOF

COPY config.toml mkvpriority.sh pyproject.toml ./
COPY src ./src

ENV MKVPRIORITY_EXT_DIR="/config/extensions"
ENV PYTHONPATH="/app/src"
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["python", "-m", "mkvpriority.entrypoint"]
