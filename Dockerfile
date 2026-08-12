FROM python:3.12-slim AS base

# Unbuffered stdout: without this, log lines sit in Python's block buffer
# instead of reaching `docker logs` promptly (stdout isn't a TTY in a container).
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install dependencies first so the image layer is reused across code-only changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

VOLUME ["/data"]

CMD ["python", "-m", "petlibro_relay"]
