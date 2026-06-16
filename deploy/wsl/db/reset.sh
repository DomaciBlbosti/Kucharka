#!/usr/bin/env bash
# POZOR: smaže DB data i config. Pro distra, kde byl dřív MySQL a MariaDB nejede.
set -euo pipefail
echo "==> Zastavuji DB procesy…"
sudo service mariadb stop 2>/dev/null || true
sudo pkill -9 mariadbd 2>/dev/null || true
sudo pkill -9 mysqld 2>/dev/null || true
sleep 2

echo "==> Odstraňuji MySQL/MariaDB balíčky a VŠECHNY zbytky (data i /etc/mysql)…"
sudo DEBIAN_FRONTEND=noninteractive apt-get purge -y 'mariadb-*' 'mysql-server*' 'mysql-client*' mysql-common 2>/dev/null || true
sudo rm -rf /var/lib/mysql /var/lib/mysql-8.0 /etc/mysql /run/mysqld
sudo apt-get autoremove -y -qq || true

echo "==> Čistá instalace MariaDB…"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y mariadb-server mariadb-client

echo ""
echo "Hotovo (datadir vytvořil instalátor). Teď spusť:  bash deploy/wsl/db/setup.sh"
