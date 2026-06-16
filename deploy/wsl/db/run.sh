#!/usr/bin/env bash
# Nastartuje MariaDB v distru (WSL po startu service nepustí samo).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
ensure_mariadb
echo "Nech toto distro/okno běžet."
