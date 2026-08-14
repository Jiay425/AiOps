#!/usr/bin/env sh
set -eu
: "${REGISTRY:?REGISTRY is required}"
: "${NAMESPACE:?NAMESPACE is required}"
IMAGE_NAME="${IMAGE_NAME:-ops-autoagent-app}"
IMAGE_TAG="${IMAGE_TAG:-2.0.0}"
LOCAL_IMAGE="${LOCAL_IMAGE:-system/${IMAGE_NAME}:${IMAGE_TAG}}"
REMOTE_IMAGE="${REGISTRY}/${NAMESPACE}/${IMAGE_NAME}:${IMAGE_TAG}"
if [ -n "${REGISTRY_USERNAME:-}" ]; then
  : "${REGISTRY_PASSWORD:?REGISTRY_PASSWORD is required when REGISTRY_USERNAME is set}"
  printf '%s' "$REGISTRY_PASSWORD" | docker login --username "$REGISTRY_USERNAME" --password-stdin "$REGISTRY"
fi
docker tag "$LOCAL_IMAGE" "$REMOTE_IMAGE"
docker push "$REMOTE_IMAGE"
printf 'docker pull %s\n' "$REMOTE_IMAGE"
