#!/usr/bin/env bash
# Prostředí kucharka-db: instalace a inicializace MariaDB.
# Spusť uvnitř WSL distra kucharka-db:  bash deploy/wsl/db/setup.sh
set -euo pipefail

DB_NAME="${DB_NAME:-kucharka}"
DB_USER="${DB_USER:-kucharka}"
DB_PASS="${DB_PASS:-kucharka}"

echo "==> Instaluji MariaDB…"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mariadb-server

echo "==> Nastavuji bind-address na 0.0.0.0 (dostupné z ostatních distr)…"
CONF="/etc/mysql/mariadb.conf.d/50-server.cnf"
if [ -f "$CONF" ]; then
  sudo sed -i 's/^\s*bind-address\s*=.*/bind-address = 0.0.0.0/' "$CONF"
fi

echo "==> Startuji MariaDB…"
sudo service mariadb start

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
echo "Hotovo. MariaDB běží, DB '$DB_NAME', uživatel '$DB_USER'."
echo "Z API instance se připojíš na  127.0.0.1:3306  (mirrored networking)."
echo "Po restartu distra spusť DB znovu:  bash deploy/wsl/db/run.sh"
