#!/usr/bin/env sh
set -eu
IMAGE="${IMAGE:-system/ops-autoagent-app:2.0.0}"
OUTPUT="${OUTPUT:---load}"
docker buildx build "$OUTPUT" --platform linux/amd64,linux/arm64 --tag "$IMAGE" --file Dockerfile .
