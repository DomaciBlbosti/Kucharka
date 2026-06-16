#!/usr/bin/env bash
# Spustí Vite dev server frontendu (hot reload).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
FRONTEND="$REPO_ROOT/frontend"

# Adresa API instance. S mirrored networking funguje localhost napříč distry.
export VITE_API_TARGET="${VITE_API_TARGET:-http://localhost:8000}"

cd "$FRONTEND"
echo "==> Frontend na http://localhost:5173  (API proxy → $VITE_API_TARGET)"
exec npm run dev
