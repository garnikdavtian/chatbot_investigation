# Optional: the same app without installing Python or uv. The main path is `uv run` (README).
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
# 0.0.0.0 inside the container only: publish it with -p 127.0.0.1:8000:8000
CMD ["python", "-m", "investigator", "serve", "--host", "0.0.0.0"]
