FROM python:3.11-slim

# as per https://docs.astral.sh/uv/guides/integration/docker/#installing-uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# as per https://docs.astral.sh/uv/guides/integration/docker/#installing-a-project
WORKDIR /app
COPY ./src ./src
COPY ./pyproject.toml ./pyproject.toml
COPY ./README.md ./README.md
COPY ./uv.lock ./uv.lock

RUN uv sync --group docker --locked --no-dev

# as per https://docs.astral.sh/uv/guides/integration/docker/#using-the-environment
ENV PATH="/app/.venv/bin:$PATH"