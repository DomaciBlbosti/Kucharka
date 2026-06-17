#!/usr/bin/env bash
# Nastartuje celou Kuchařku v jednom distru: DB + API + Web.
# API běží na pozadí, Web v popředí (Ctrl+C ukončí oboje).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/../../.." && pwd)"

echo "==> DB (MariaDB :3306)"
. "$DIR/../db/_lib.sh"
ensure_mariadb

echo "==> API (uvicorn :8000)"
cd "$REPO/backend"
set -a; [ -f .env ] && . ./.env; set +a
. .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
API_PID=$!

cleanup() {
  echo; echo "==> Zastavuji…"
  pkill -P "$API_PID" 2>/dev/null || true
  kill "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> Web (Vite :5173)"
cd "$REPO/frontend"
export VITE_API_TARGET="${VITE_API_TARGET:-http://localhost:8000}"
export WEB_PORT="${WEB_PORT:-5173}"
echo ""
echo "    Otevři http://localhost:$WEB_PORT   (API /docs na :8000)"
echo ""
npm run dev
