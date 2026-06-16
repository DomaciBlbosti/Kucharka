#!/usr/bin/env bash
# Prostředí kucharka-api: Python venv + závislosti backendu.
# Spusť uvnitř distra kucharka-api z kořene repa:  bash deploy/wsl/api/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
BACKEND="$REPO_ROOT/backend"

echo "==> Systémové závislosti (Python, build pro lxml)…"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-venv python3-dev build-essential libxml2-dev libxslt1-dev

echo "==> Virtuální prostředí a balíčky…"
cd "$BACKEND"
python3 -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ ! -f "$BACKEND/.env" ]; then
  cp "$SCRIPT_DIR/env.example" "$BACKEND/.env"
  echo "==> Vytvořen $BACKEND/.env (uprav podle potřeby)."
fi

echo ""
echo "Hotovo. Spuštění API:  bash deploy/wsl/api/run.sh"
