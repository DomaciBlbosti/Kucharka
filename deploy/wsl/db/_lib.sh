#!/usr/bin/env bash
# Sdílená logika startu MariaDB – funguje i bez systemd (typické pro WSL).

ensure_mariadb() {
  sudo install -d -o mysql -g mysql /run/mysqld 2>/dev/null || true

  # WSL: InnoDB native AIO bývá v kernelu nedostupné → vypnout, jinak mariadbd spadne
  if [ -d /etc/mysql/mariadb.conf.d ] && [ ! -f /etc/mysql/mariadb.conf.d/99-wsl.cnf ]; then
    printf '[mariadbd]\ninnodb_use_native_aio=0\n' \
      | sudo tee /etc/mysql/mariadb.conf.d/99-wsl.cnf >/dev/null
  fi
  sudo chown -R mysql:mysql /var/lib/mysql 2>/dev/null || true

  # inicializace datadiru, pokud chybí systémové tabulky
  if [ ! -d /var/lib/mysql/mysql ]; then
    echo "==> Inicializuji datadir…"
    sudo mariadb-install-db --user=mysql --datadir=/var/lib/mysql >/dev/null
  fi

  # už běží?
  if sudo mariadb -e "SELECT 1" >/dev/null 2>&1; then
    echo "MariaDB už běží."
    return 0
  fi

  echo "==> Startuji MariaDB…"
  # nejdřív zkus systemd/service, jinak spusť napřímo přes mariadbd-safe
  if ! sudo service mariadb start >/dev/null 2>&1; then
    sudo mariadbd-safe --datadir=/var/lib/mysql >/tmp/mariadb-safe.log 2>&1 &
  fi

  for _ in $(seq 1 30); do
    if sudo mariadb -e "SELECT 1" >/dev/null 2>&1; then
      echo "MariaDB běží (3306)."
      return 0
    fi
    sleep 1
  done

  echo "CHYBA: MariaDB nenaběhla. Posledních pár řádků logu:"
  sudo tail -n 25 /var/log/mysql/error.log 2>/dev/null \
    || sudo tail -n 25 /tmp/mariadb-safe.log 2>/dev/null || true
  return 1
}
