#!/usr/bin/env bash
# Prostředí kucharka-web: Node.js + závislosti frontendu.
# Spusť uvnitř distra kucharka-web z kořene repa:  bash deploy/wsl/web/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
FRONTEND="$REPO_ROOT/frontend"

if ! command -v node >/dev/null 2>&1; then
  echo "==> Instaluji Node.js 20…"
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
fi
echo "==> Node $(node -v), npm $(npm -v)"

echo "==> Instaluji balíčky frontendu…"
cd "$FRONTEND"
npm install --no-audit --no-fund

echo ""
echo "Hotovo. Spuštění frontendu:  bash deploy/wsl/web/run.sh"
