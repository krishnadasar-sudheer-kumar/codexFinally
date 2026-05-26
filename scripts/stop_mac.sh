#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${FINALLY_CONTAINER:-finally}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but was not found on PATH." >&2
  exit 1
fi

RUNNING_ID="$(docker ps -q -f "name=^/${CONTAINER_NAME}$")"
EXISTING_ID="$(docker ps -aq -f "name=^/${CONTAINER_NAME}$")"

if [ -n "$RUNNING_ID" ]; then
  docker stop "$CONTAINER_NAME" >/dev/null
fi

if [ -n "$EXISTING_ID" ]; then
  docker rm "$CONTAINER_NAME" >/dev/null
  echo "Stopped FinAlly container. Data volume was preserved."
else
  echo "FinAlly container is not running."
fi
