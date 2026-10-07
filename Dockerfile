# One image for the api, agent and db containers (compose.yaml). The sales database is not in it: db mounts it read-only.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["python", "-m", "investigator", "serve", "--host", "0.0.0.0"]  # compose sets each container's command
