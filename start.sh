#!/usr/bin/env bash
# One command to run the app: ./start.sh   then log in as admin / admin123
# Uses Docker when it is running (four containers), otherwise uv (one process). Then open http://127.0.0.1:8000.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Set LLM_API_KEY in it to ask new questions."
  echo "Without a key: the demo chats, replay, saved reports and the comparison page all work."
fi

# Local default login; later runs find the user and skip this.
create_user() { "$@" python -m investigator add-user admin --password admin123 --demo-chats || true; }

if docker info >/dev/null 2>&1; then
  echo "Starting with Docker: ui, api, agent and db containers."
  docker compose up --build -d
  create_user docker compose exec -T api
  echo
  echo "Open http://127.0.0.1:8000 and log in as admin / admin123. Logs: docker compose logs -f   Stop: docker compose down"
else
  command -v uv >/dev/null || { echo "Install Docker (and start it) or uv: https://docs.astral.sh/uv/" >&2; exit 1; }
  echo "Docker is not running; starting with uv in this terminal (Ctrl+C stops it)."
  uv sync --frozen --quiet
  create_user uv run
  echo
  echo "Log in as admin / admin123 at the address below."
  exec uv run --env-file .env python -m investigator serve
fi
