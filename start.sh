#!/usr/bin/env bash
# One command to run the app: ./start.sh [login name]   (default name: reviewer)
# Uses Docker when it is running (four containers), otherwise uv (one process). Then open http://127.0.0.1:8000.
set -euo pipefail
cd "$(dirname "$0")"
name="${1:-reviewer}"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Set LLM_API_KEY in it to ask new questions."
  echo "Without a key: the demo chats, replay, saved reports and the comparison page all work."
fi

# The first run asks for a password; later runs find the user and skip this.
create_user() { "$@" python -m investigator add-user "$name" --demo-chats || true; }

if docker info >/dev/null 2>&1; then
  echo "Starting with Docker: ui, api, agent and db containers."
  docker compose up --build -d
  create_user docker compose exec api
  echo
  echo "Open http://127.0.0.1:8000 and log in as '$name'. Logs: docker compose logs -f   Stop: docker compose down"
else
  command -v uv >/dev/null || { echo "Install Docker (and start it) or uv: https://docs.astral.sh/uv/" >&2; exit 1; }
  echo "Docker is not running; starting with uv in this terminal (Ctrl+C stops it)."
  uv sync --frozen --quiet
  create_user uv run
  echo
  echo "Log in as '$name' at the address below."
  exec uv run --env-file .env python -m investigator serve
fi
