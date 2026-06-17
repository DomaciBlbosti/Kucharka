#!/usr/bin/env bash
# Supervisor smyčka: klon → (pull → build je-li potřeba) → spustí API.
# Self-update: endpoint /api/system/update ukončí uvicorn, smyčka pullne a
# restartuje. Rebuild frontendu jen když se změnil kód nebo chybí static.
set -uo pipefail

REPO_URL="${REPO_URL:-https://github.com/DomaciBlbosti/Kucharka.git}"
BRANCH="${REPO_BRANCH:-main}"
cd /app

if [ ! -e backend/app/main.py ]; then
  echo "[init] klonuji ${REPO_URL} (${BRANCH})"
  git clone --branch "${BRANCH}" "${REPO_URL}" /tmp/repo \
    && cp -a /tmp/repo/. /app/ && rm -rf /tmp/repo \
    || { echo "[init] klon selhal"; sleep 10; exit 1; }
fi
git config --global --add safe.directory /app

build() {
  echo "[build] pip install"
  pip install -q -r backend/requirements.txt || echo "[build] pip varování"
  echo "[build] frontend (npm)"
  if ( cd frontend && { npm ci --silent || npm install --silent; } && npm run build ); then
    rm -rf backend/app/static && cp -r frontend/dist backend/app/static
    echo "[build] frontend OK"
  else
    echo "[build] frontend selhal – ponechávám stávající static"
  fi
}

while true; do
  before="$(git rev-parse HEAD 2>/dev/null || echo none)"
  git pull --ff-only origin "${BRANCH}" 2>&1 || echo "[git] pull přeskočen"
  after="$(git rev-parse HEAD 2>/dev/null || echo none)"

  if [ -f .needs-build ] || [ "${before}" != "${after}" ] || [ ! -d backend/app/static ]; then
    build
    rm -f .needs-build
  fi

  echo "[run] start API na :8000 (commit ${after:0:7})"
  ( cd backend && SUPERVISED=1 REPO_DIR=/app uvicorn app.main:app --host 0.0.0.0 --port 8000 )
  echo "[run] API skončilo (kód $?) – restart za 3 s"
  sleep 3
done
