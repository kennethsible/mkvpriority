FROM python:3.13-slim AS builder

WORKDIR /app

COPY pyproject.toml poetry.lock ./
COPY mkvpriority/ ./mkvpriority/
RUN pip install poetry>=2.0 && \
    poetry config virtualenvs.in-project true && \
    poetry install --no-interaction && \
    poetry cache clear PyPI --all --no-interaction

FROM python:3.13-slim AS runtime

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder ${VIRTUAL_ENV} ${VIRTUAL_ENV}
COPY --from=builder /app/mkvpriority /app/mkvpriority
COPY config.toml mkvpriority.sh ./

ENTRYPOINT ["python", "-m", "mkvpriority.entrypoint"]
