FROM python:3.13-slim AS builder

WORKDIR /app

COPY pyproject.toml .
RUN pip install poetry>=2.0 && \
    poetry config virtualenvs.in-project true && \
    poetry install --no-root --no-interaction && \
    poetry cache clear PyPI --all --no-interaction

FROM python:3.13-slim AS runtime

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder ${VIRTUAL_ENV} ${VIRTUAL_ENV}
COPY mkvpriority/ .
COPY config.toml .

ENTRYPOINT ["python", "entrypoint.py"]
