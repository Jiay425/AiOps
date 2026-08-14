#!/usr/bin/env sh
set -eu
IMAGE="${IMAGE:-system/ops-autoagent-app:2.0.0}"
docker build --tag "$IMAGE" --file Dockerfile .
