#!/usr/bin/env bash
# Run Temporal + FastAPI + Temporal worker for the Dev Fix flow.
# Usage: from voiceaiagentv5 project root: ./scripts/run_dev_fix.sh
# Press Ctrl+C to stop the API and worker (Temporal Docker is left running).

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}Dev Fix backend – starting services${NC}"

# Use project venv so FastAPI and all deps are available (pip install -r requirements.txt)
if [ -f "$ROOT/.venv/bin/activate" ]; then
  echo "Using venv: $ROOT/.venv"
  . "$ROOT/.venv/bin/activate"
elif [ -f "$ROOT/venv/bin/activate" ]; then
  echo "Using venv: $ROOT/venv"
  . "$ROOT/venv/bin/activate"
else
  echo -e "${RED}No virtualenv found. Create one and install dependencies:${NC}"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  echo "Then run this script again."
  exit 1
fi

# 1. Temporal: ensure something is listening on 7233 (Temporal CLI preferred, no Cassandra)
port_in_use() {
  if command -v nc &>/dev/null; then
    nc -z localhost 7233 2>/dev/null
  else
    false
  fi
}

TEMPORAL_PID=""
if port_in_use; then
  echo -e "${YELLOW}Temporal already running on 7233.${NC}"
elif command -v temporal &>/dev/null; then
  echo "Starting Temporal dev server (CLI; no Docker/Cassandra)..."
  temporal server start-dev --headless &
  TEMPORAL_PID=$!
  echo "Waiting for Temporal on 7233..."
  for i in $(seq 1 15); do
    sleep 2
    if port_in_use; then
      echo -e "${GREEN}Temporal is ready.${NC}"
      break
    fi
    printf "."
    [ "$i" -eq 15 ] && echo ""
  done
else
  echo -e "${YELLOW}Temporal CLI not found. Install it (recommended, no Docker/Cassandra):${NC}"
  echo "  brew install temporal"
  echo "Then run this script again."
  echo ""
  echo "Or start Temporal in another terminal and run this script again:"
  echo "  temporal server start-dev"
  exit 1
fi

if ! port_in_use; then
  echo -e "${RED}Temporal is not reachable on localhost:7233.${NC}"
  [ -n "$TEMPORAL_PID" ] && kill $TEMPORAL_PID 2>/dev/null || true
  echo "Start it manually in another terminal, then run this script again:"
  echo "  temporal server start-dev"
  exit 1
fi

# 2. FastAPI in background (use same Python as venv)
if command -v lsof &>/dev/null && lsof -i :8000 -t &>/dev/null; then
  echo -e "${YELLOW}Port 8000 is already in use. Free it with: kill \$(lsof -t -i:8000)${NC}"
  exit 1
fi
echo "Starting FastAPI on port 8000..."
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
API_PID=$!
sleep 2
if ! kill -0 $API_PID 2>/dev/null; then
  echo -e "${RED}Failed to start uvicorn. Check .env and dependencies.${NC}"
  exit 1
fi
echo -e "${GREEN}API running (PID $API_PID).${NC}"

cleanup() {
  echo ""
  echo "Stopping API and worker..."
  kill $API_PID 2>/dev/null || true
  kill $WORKER_PID 2>/dev/null || true
  [ -n "$TEMPORAL_PID" ] && kill $TEMPORAL_PID 2>/dev/null || true
  exit 0
}
trap cleanup SIGINT SIGTERM

# 3. Worker in foreground (so we see logs)
echo "Starting Temporal worker (Ctrl+C to stop all)..."
python app/temporal_worker.py &
WORKER_PID=$!
wait $WORKER_PID
