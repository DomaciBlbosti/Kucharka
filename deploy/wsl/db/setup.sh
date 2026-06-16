#!/usr/bin/env bash
# Prostředí kucharka-db: instalace a inicializace MariaDB.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"

DB_NAME="${DB_NAME:-kucharka}"
DB_USER="${DB_USER:-kucharka}"
DB_PASS="${DB_PASS:-kucharka}"

echo "==> Instaluji MariaDB…"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mariadb-server mariadb-client

echo "==> Nastavuji bind-address na 0.0.0.0…"
CONF="/etc/mysql/mariadb.conf.d/50-server.cnf"
[ -f "$CONF" ] && sudo sed -i 's/^\s*bind-address\s*=.*/bind-address = 0.0.0.0/' "$CONF"

ensure_mariadb

echo "==> Vytvářím databázi a uživatele '$DB_USER'…"
sudo mariadb <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_czech_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
CREATE USER IF NOT EXISTS '$DB_USER'@'%'         IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'%';
FLUSH PRIVILEGES;
SQL

echo ""
echo "Hotovo. DB '$DB_NAME', uživatel '$DB_USER'. Start příště: bash deploy/wsl/db/run.sh"
