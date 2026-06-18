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

# Zapeč Python závislosti do image, aby uvicorn nikdy nechyběl – ani po
# znovuvytvoření kontejneru, kdy se vrstva resetuje a /app volume zůstane.
# Runtime `pip install` v entrypointu pak jen dotahuje případné změny.
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV REPO_URL=https://github.com/DomaciBlbosti/Kucharka.git \
    REPO_BRANCH=main \
    REPO_DIR=/app \
    PIP_BREAK_SYSTEM_PACKAGES=1

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
