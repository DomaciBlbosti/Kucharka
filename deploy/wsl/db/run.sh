#!/usr/bin/env bash
# Nastartuje MariaDB v distru kucharka-db (WSL po startu service nepustí samo).
set -euo pipefail
sudo service mariadb start
sudo service mariadb status || true
echo "MariaDB poslouchá na 3306. Nech toto distro běžet."
