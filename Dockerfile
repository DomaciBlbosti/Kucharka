# Obraz nese jen toolchain (Python + Node + git). Vlastní kód se za běhu
# naklonuje z Gitu do volume /app a aktualizuje přes `git pull` (self-update).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV REPO_URL=https://github.com/DomaciBlbosti/Kucharka.git \
    REPO_BRANCH=main \
    REPO_DIR=/app \
    PIP_BREAK_SYSTEM_PACKAGES=1

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
