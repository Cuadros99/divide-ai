#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

source .venv/bin/activate

PORT="${PORT:-8000}"
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "SEU_IP_LOCAL")

echo "Rodando em http://localhost:$PORT (e http://$LOCAL_IP:$PORT na rede local, ex: celular)"

exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload
