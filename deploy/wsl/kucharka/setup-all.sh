#!/usr/bin/env bash
# Prostředí "kucharka": setup celé appky (DB + API + Web) v jednom distru.
# Spusť jednou z kořene repa:  bash deploy/wsl/kucharka/setup-all.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "############ DB ############";  bash "$DIR/../db/setup.sh"
echo "############ API ###########"; bash "$DIR/../api/setup.sh"
echo "############ WEB ###########"; bash "$DIR/../web/setup.sh"

echo ""
echo "Hotovo. Zkontroluj backend/.env (OLLAMA_URL na tvou Ollamu) a spusť:"
echo "  bash deploy/wsl/kucharka/run-all.sh"
