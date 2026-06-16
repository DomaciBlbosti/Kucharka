#!/usr/bin/env bash
# Spustí FastAPI backend s auto-reloadem (pro odlazení).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
BACKEND="$REPO_ROOT/backend"

cd "$BACKEND"
set -a
[ -f .env ] && . ./.env
set +a

. .venv/bin/activate
echo "==> API na http://0.0.0.0:8000  (Swagger: /docs)"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
