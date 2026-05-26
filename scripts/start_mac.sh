#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${FINALLY_IMAGE:-finally:local}"
CONTAINER_NAME="${FINALLY_CONTAINER:-finally}"
VOLUME_NAME="${FINALLY_VOLUME:-finally-data}"
PORT="${FINALLY_PORT:-${APP_PORT:-8000}}"
ENV_FILE="$ROOT_DIR/.env"
BUILD=false
OPEN_BROWSER=false

for arg in "$@"; do
  case "$arg" in
    --build) BUILD=true ;;
    --open) OPEN_BROWSER=true ;;
    -h|--help)
      echo "Usage: scripts/start_mac.sh [--build] [--open]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but was not found on PATH." >&2
  exit 1
fi

if [ ! -f "$ENV_FILE" ] && [ -f "$ROOT_DIR/.env.example" ]; then
  cp "$ROOT_DIR/.env.example" "$ENV_FILE"
  echo "Created .env from .env.example"
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing .env. Create one from .env.example before starting FinAlly." >&2
  exit 1
fi

if [ "$BUILD" = true ] || ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
  docker build -t "$IMAGE_NAME" "$ROOT_DIR"
fi

EXISTING_ID="$(docker ps -aq -f "name=^/${CONTAINER_NAME}$")"
RUNNING_ID="$(docker ps -q -f "name=^/${CONTAINER_NAME}$")"

if [ -n "$RUNNING_ID" ]; then
  if [ "$BUILD" = true ]; then
    docker stop "$CONTAINER_NAME" >/dev/null
    docker rm "$CONTAINER_NAME" >/dev/null
  else
    echo "FinAlly is already running at http://localhost:${PORT}"
    exit 0
  fi
elif [ -n "$EXISTING_ID" ]; then
  if [ "$BUILD" = true ]; then
    docker rm "$CONTAINER_NAME" >/dev/null
  else
    docker start "$CONTAINER_NAME" >/dev/null
    echo "FinAlly started at http://localhost:${PORT}"
    if [ "$OPEN_BROWSER" = true ]; then
      open "http://localhost:${PORT}" >/dev/null 2>&1 || true
    fi
    exit 0
  fi
fi

docker volume create "$VOLUME_NAME" >/dev/null
docker run -d \
  --name "$CONTAINER_NAME" \
  --env-file "$ENV_FILE" \
  -e DB_PATH=/app/db/finally.db \
  -p "${PORT}:8000" \
  -v "${VOLUME_NAME}:/app/db" \
  "$IMAGE_NAME" >/dev/null

echo "FinAlly started at http://localhost:${PORT}"

if [ "$OPEN_BROWSER" = true ]; then
  open "http://localhost:${PORT}" >/dev/null 2>&1 || true
fi
